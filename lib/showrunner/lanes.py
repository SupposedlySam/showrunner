"""Lane routing — which lane a ready leaf lands in, and why. Issue #8.

Two properties matter more than the mechanism.

**Conservative by default.** Wrongly routing a serialized leaf into the headless lane
collides on a single-consumer resource — the exact failure the one hard rule exists to
prevent — and it surfaces as a wedged device hours in with nobody watching. Wrongly
serializing a headless leaf makes it slower. Those costs are not comparable, so
unclassified work serializes.

**The classifier is not the enforcement.** Routing is an optimisation; the lock is the
guarantee, and it must be taken by the *consumer* (`showrunner lock run`), not merely
checked by the router. Anything that makes routing look like the safety mechanism is a
regression, because it invites tuning the regex instead of holding the lock.

Every decision is logged with the rule that produced it, so a wrong route is diagnosable
after the fact rather than re-derived from behaviour.
"""

import json
import os
import re

from .util import die, now

HEADLESS = "headless"
SERIALIZED = "serialized"
ORCHESTRATION = "orchestration"


class Decision(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def line(self):
        res = " resource=%s" % self["resource"] if self.get("resource") else ""
        return "%-14s → %-11s%s  [%s]" % (self["leaf"], self["lane"], res, self["why"])


def _field_values(leaf, field):
    if field == "labels":
        return leaf.labels_list if hasattr(leaf, "labels_list") else []
    if field == "paths":
        return leaf.paths_list if hasattr(leaf, "paths_list") else []
    return [leaf.get(field) or ""]


def _rule_matches(rule, leaf):
    """A rule matches when EVERY declared field matches (AND), each field matching when
    ANY of its patterns hits (OR). Returns the matching evidence, or None."""
    match = rule.get("match") or {}
    if not match:
        return None
    evidence = []
    for field, patterns in match.items():
        if isinstance(patterns, str):
            patterns = [patterns]
        values = _field_values(leaf, field)
        hit = None
        for pat in patterns:
            for val in values:
                try:
                    if re.search(pat, str(val), re.IGNORECASE):
                        hit = "%s~/%s/" % (field, pat)
                        break
                except re.error as exc:
                    die("lane rule %r has an invalid pattern %r for field %r: %s"
                        % (rule.get("name"), pat, field, exc), code=2)
            if hit:
                break
        if not hit:
            return None
        evidence.append(hit)
    return " & ".join(evidence)


def route(cfg, leaf):
    """Resolve a leaf to a lane. Always returns a Decision — never silently nothing."""
    for rule in cfg.get("lanes") or []:
        evidence = _rule_matches(rule, leaf)
        if evidence:
            return Decision({
                "leaf": leaf["id"],
                "title": leaf.get("title", ""),
                "lane": rule.get("lane") or SERIALIZED,
                "resource": rule.get("resource"),
                "rule": rule.get("name") or evidence,
                "why": "rule %r matched %s" % (rule.get("name") or "<unnamed>", evidence),
                "matched": True,
                "ts": now(),
            })

    default = cfg.get("default_lane") or SERIALIZED
    resource = None
    if default == SERIALIZED:
        names = [r["name"] for r in (cfg.get("resources") or []) if r.get("name")]
        resource = names[0] if len(names) == 1 else None
    return Decision({
        "leaf": leaf["id"],
        "title": leaf.get("title", ""),
        "lane": default,
        "resource": resource,
        "rule": None,
        # An unmatched leaf is a missing rule. Silence lets the config rot while the run
        # gets slower for reasons nobody can name.
        "why": "NO RULE MATCHED — defaulted to %s. Add a lane rule for this leaf; an "
               "unmatched leaf is a missing rule, not a neutral outcome." % default,
        "matched": False,
        "ts": now(),
    })


def log(cfg, decisions):
    path = os.path.join(cfg.state_dir, "routing.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for d in decisions:
            fh.write(json.dumps(d, sort_keys=True) + "\n")
    return path
