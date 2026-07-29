"""score.py — apply a sealed rule to observed results, and refuse when the seal does not fit.

Three ways scoring must refuse rather than answer, all of them things that have actually happened
to real pre-registrations:

  1. the plan was edited after sealing        -> digest mismatch
  2. a metric the rule reads was not reported -> the rule cannot be evaluated
  3. an observed value is OUTSIDE the support the plan declared

(3) is the subtle one and it is the most important. If a plan declared `auc` in [0,1] and the run
reports 1.4, then either the metric is not what the plan thought it was or the measurement is
broken. Either way the falsifiability analysis was carried out over the wrong domain, so the seal
does not certify this run at all. Silently scoring it would launder a broken measurement through a
credential.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .expr import eval_exact, metric_names, parse
from .falsify import Support
from .seal import SCHEMA, SpecError, verify_seal

__all__ = ["Outcome", "score_run"]

FINDING, NULL, REFUSED = "FINDING", "NULL", "REFUSED"


@dataclass
class Outcome:
    verdict: str
    rule: str
    observed: Dict[str, float]
    refusals: List[str] = field(default_factory=list)
    digest: Optional[str] = None
    description: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 the null · 1 the pre-registered finding · 2 refused to score."""
        return {NULL: 0, FINDING: 1, REFUSED: 2}[self.verdict]

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": "prereg-outcome/v1", "verdict": self.verdict, "rule": self.rule,
                "observed": self.observed, "seal_digest": self.digest,
                "description": self.description, "refusals": self.refusals, "notes": self.notes}

    def render(self) -> str:
        lines = [f"{self.verdict} — {self.description}"]
        if self.refusals:
            lines += [f"  {r}" for r in self.refusals]
        else:
            for k in sorted(self.observed):
                lines.append(f"  {k:<28} {self.observed[k]}")
        lines += [f"  {n}" for n in self.notes]
        return "\n".join(lines)


def _in_support(value: float, s: Support) -> bool:
    if s.kind == "finite":
        return any(math.isclose(value, v, rel_tol=1e-12, abs_tol=1e-12) for v in s.values)
    if s.kind == "integer":
        return s.lo <= value <= s.hi and float(value).is_integer()
    return s.lo <= value <= s.hi


def score_run(seal: Dict[str, Any], observed: Dict[str, float]) -> Outcome:
    """Evaluate the sealed rule against observed metric values."""
    refusals: List[str] = []
    try:
        digest = verify_seal(seal)
    except SpecError as e:
        return Outcome(REFUSED, "", dict(observed), [str(e)], None,
                       "the sealed plan could not be verified")

    spec = seal["spec"]
    rule = str(spec.get("decision_rule", ""))
    try:
        node = parse(rule)
    except Exception as e:
        return Outcome(REFUSED, rule, dict(observed), [f"the sealed rule does not parse: {e}"],
                       digest, "the sealed plan is not evaluable")

    supports = {k: Support.from_dict(k, v) for k, v in spec.get("metrics", {}).items()}
    needed = sorted(metric_names(node))

    clean: Dict[str, float] = {}
    for k, v in observed.items():
        try:
            clean[k] = float(v)
        except (TypeError, ValueError):
            refusals.append(f"metric {k!r} is {v!r}, which is not a number")

    for n in needed:
        if n not in clean:
            refusals.append(
                f"the rule reads {n!r} and the run did not report it. A missing metric cannot be "
                f"defaulted -- a default would decide the outcome by fiat.")

    for k, v in clean.items():
        if k in supports and not _in_support(v, supports[k]):
            s = supports[k]
            rng = (f"one of {sorted(set(s.values))}" if s.kind == "finite"
                   else f"[{s.lo}, {s.hi}]")
            refusals.append(
                f"{k}={v} is OUTSIDE the support this plan declared ({rng}). The falsifiability "
                f"analysis was carried out over that domain, so the seal does not certify this "
                f"run.")

    if refusals:
        return Outcome(REFUSED, rule, clean, refusals, digest,
                       "the sealed plan does not fit this run")

    unread = sorted(set(clean) - set(needed))
    notes = []
    if unread:
        notes.append(f"reported but not read by the rule: {', '.join(unread)}")

    try:
        value = eval_exact(node, clean)
    except ZeroDivisionError as e:
        return Outcome(REFUSED, rule, clean, [f"the rule divided by zero on this run: {e}"],
                       digest, "the sealed rule is undefined at the observed values")

    if value != 0:
        return Outcome(FINDING, rule, clean, [], digest,
                       spec.get("finding") or "the pre-registered finding fired", notes)
    return Outcome(NULL, rule, clean, [], digest,
                   spec.get("null") or "the pre-registered null", notes)
