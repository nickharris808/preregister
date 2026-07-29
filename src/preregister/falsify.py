"""falsify.py — can this rule's positive branch actually fire? Can its negative?

THE SCAR THIS EXISTS FOR. This portfolio once shipped a pre-registration whose positive branch was
structurally unreachable. The run was configured with `prompt_logprobs=0`, which makes the metric
`argmax_flips` identically zero; the sealed rule was `argmax_flips > 0`. The pre-registration was
honest, public, sealed before data — and could only ever report the null. It measured nothing, and
the seal made it look rigorous.

A pre-registration whose conclusion is fixed before the data is not a pre-registration. So this
module answers two questions about a rule, over the supports its own spec declares:

    is there an assignment where the rule FIRES?      (else the finding can never be reached)
    is there an assignment where it does NOT fire?    (else the finding is guaranteed)

HOW EACH ANSWER IS ESTABLISHED, AND WHY THAT MATTERS.

    reachable   — proven by EXHIBITING a witness. A concrete assignment that evaluates to the
                  branch is a proof, not evidence.
    unreachable — proven by INTERVAL EVALUATION over the whole declared domain. The interval
                  semantics over-approximates, so if it says a branch is empty, it is empty.
    unknown     — neither. No witness found and no emptiness proof. This is NOT "probably fine":
                  it is reported as UNDETERMINED and the seal is refused, because the one thing
                  this package must never do is bless a rule it could not analyse.

The asymmetry is deliberate. Both `reachable` and `unreachable` are sound; the uncertainty is
pushed entirely into `unknown`, where it is visible.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .expr import (FALSE, MAYBE, TRUE, Interval, Node, eval_exact, eval_interval, metric_names,
                   parse)

__all__ = ["Support", "BranchResult", "FalsifiabilityReport", "analyse",
           "REACHABLE", "UNREACHABLE", "UNKNOWN"]

REACHABLE = "REACHABLE"
UNREACHABLE = "UNREACHABLE"
UNKNOWN = "UNKNOWN"

# A cap on exhaustive enumeration. Above it the analysis switches to witness-search plus interval
# proof, and SAYS SO -- a silent downgrade from exact to approximate would be the same defect this
# package exists to catch.
ENUMERATION_CAP = 200_000


@dataclass(frozen=True)
class Support:
    """The set of values a metric can take. Declaring this is the author's real work.

    `finite`   — an explicit set. Enables exact analysis.
    `integer`  — an inclusive integer range. Exact if small enough to enumerate.
    `real`     — an inclusive real interval. Witness search plus interval proof; never exhaustive.
    """
    kind: str
    values: Tuple[float, ...] = ()
    lo: float = 0.0
    hi: float = 0.0

    @staticmethod
    def from_dict(name: str, d: object) -> "Support":
        if isinstance(d, (list, tuple)):
            vals = tuple(float(v) for v in d)
            if not vals:
                raise ValueError(f"metric {name!r} declares an EMPTY support: no assignment "
                                 f"exists, so no rule mentioning it can ever be evaluated")
            return Support("finite", values=vals)
        if not isinstance(d, dict):
            raise ValueError(f"metric {name!r}: support must be a list of values or an object "
                             f"with lo/hi, got {type(d).__name__}")
        if "values" in d:
            return Support.from_dict(name, d["values"])
        if "lo" not in d or "hi" not in d:
            raise ValueError(f"metric {name!r}: an interval support needs both `lo` and `hi`. "
                             f"A metric with no declared range cannot be analysed, and an "
                             f"unanalysable rule is what this package refuses to seal.")
        lo, hi = float(d["lo"]), float(d["hi"])
        if math.isnan(lo) or math.isnan(hi):
            raise ValueError(f"metric {name!r}: NaN bound")
        if lo > hi:
            raise ValueError(f"metric {name!r}: declared support is empty (lo {lo} > hi {hi})")
        kind = "integer" if str(d.get("type", "real")).lower() in ("int", "integer") else "real"
        if kind == "integer" and (lo != int(lo) or hi != int(hi)):
            raise ValueError(f"metric {name!r}: integer support with non-integer bounds")
        return Support(kind, lo=lo, hi=hi)

    @property
    def constant(self) -> bool:
        if self.kind == "finite":
            return len(set(self.values)) == 1
        return self.lo == self.hi

    @property
    def enumerable(self) -> Optional[int]:
        """How many points, if it can be enumerated exactly; None if it cannot."""
        if self.kind == "finite":
            return len(set(self.values))
        if self.kind == "integer" and math.isfinite(self.lo) and math.isfinite(self.hi):
            return int(self.hi) - int(self.lo) + 1
        return None

    def points(self) -> Sequence[float]:
        if self.kind == "finite":
            return sorted(set(self.values))
        if self.kind == "integer":
            return [float(v) for v in range(int(self.lo), int(self.hi) + 1)]
        raise TypeError("a real support cannot be enumerated")

    def interval(self) -> Interval:
        if self.kind == "finite":
            vs = sorted(set(self.values))
            return Interval(vs[0], vs[-1])
        return Interval(self.lo, self.hi)

    def probes(self, extra: Sequence[float] = ()) -> List[float]:
        """Candidate witness values: the ends, the middle, and any threshold the rule mentions."""
        if self.kind == "finite":
            return sorted(set(self.values))
        if self.kind == "integer":
            n = self.enumerable or 0
            if n <= 64:
                return list(self.points())
            base = {self.lo, self.hi, float(math.floor((self.lo + self.hi) / 2))}
        else:
            base = {self.lo, self.hi, (self.lo + self.hi) / 2.0}
        for t in extra:
            # A comparison against a threshold is decided at that threshold and just either side
            # of it; a grid that steps over `t` will miss `x > t` on a narrow support.
            for cand in (t, t - 1e-9, t + 1e-9, t - 1.0, t + 1.0):
                if self.lo <= cand <= self.hi:
                    base.add(float(math.floor(cand)) if self.kind == "integer" else cand)
        return sorted(base)

    def to_dict(self) -> dict:
        if self.kind == "finite":
            return {"values": list(self.values)}
        return {"type": self.kind, "lo": self.lo, "hi": self.hi}


@dataclass
class BranchResult:
    status: str
    witness: Optional[Dict[str, float]] = None
    reason: str = ""


@dataclass
class FalsifiabilityReport:
    rule: str
    positive: BranchResult
    negative: BranchResult
    exact: bool
    metrics: List[str] = field(default_factory=list)
    constant_metrics: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def falsifiable(self) -> bool:
        """Both branches proven reachable. Anything else is not a yes."""
        return self.positive.status == REACHABLE and self.negative.status == REACHABLE

    @property
    def verdict(self) -> str:
        if self.falsifiable:
            return "FALSIFIABLE"
        if UNREACHABLE in (self.positive.status, self.negative.status):
            return "UNFALSIFIABLE"
        return "UNDETERMINED"

    @property
    def exit_code(self) -> int:
        """0 falsifiable · 1 proven unfalsifiable · 2 could not be established."""
        return {"FALSIFIABLE": 0, "UNFALSIFIABLE": 1, "UNDETERMINED": 2}[self.verdict]

    def explain(self) -> str:
        if self.verdict == "FALSIFIABLE":
            return ("both branches are reachable: a witness exists for the finding and for its "
                    "absence, so the data can decide")
        if self.positive.status == UNREACHABLE:
            return (f"THE FINDING CAN NEVER BE REPORTED. {self.positive.reason} The run is "
                    f"guaranteed to return the null before any data is collected.")
        if self.negative.status == UNREACHABLE:
            return (f"THE NULL CAN NEVER BE REPORTED. {self.negative.reason} The run is "
                    f"guaranteed to confirm the finding before any data is collected.")
        which = "the finding" if self.positive.status == UNKNOWN else "the null"
        return (f"could not establish whether {which} is reachable, and this package does not "
                f"bless a rule it was unable to analyse")

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "verdict": self.verdict,
            "falsifiable": self.falsifiable,
            "analysis": "exhaustive" if self.exact else "witness-search + interval proof",
            "metrics": self.metrics,
            "constant_metrics": self.constant_metrics,
            "positive_branch": {"status": self.positive.status,
                                "witness": self.positive.witness,
                                "reason": self.positive.reason},
            "negative_branch": {"status": self.negative.status,
                                "witness": self.negative.witness,
                                "reason": self.negative.reason},
            "explanation": self.explain(),
            "notes": self.notes,
        }


def _thresholds(node: Node) -> List[float]:
    """Constants the rule compares against — the values most likely to separate the branches."""
    from .expr import Binary, Num, Unary
    out: List[float] = []
    if isinstance(node, Num):
        out.append(node.value)
    elif isinstance(node, Unary):
        out.extend(-t for t in _thresholds(node.operand))
    elif isinstance(node, Binary):
        out.extend(_thresholds(node.left))
        out.extend(_thresholds(node.right))
    return out


def analyse(rule: str, supports: Dict[str, Support], *,
            cap: int = ENUMERATION_CAP) -> FalsifiabilityReport:
    """Decide reachability of both branches of `rule` over `supports`."""
    node = parse(rule)
    names = sorted(metric_names(node))
    missing = [n for n in names if n not in supports]
    if missing:
        raise ValueError(
            f"the rule mentions {missing} but the spec declares no support for "
            f"{'them' if len(missing) > 1 else 'it'}. Every metric a rule reads must declare the "
            f"values it can take, or the rule cannot be checked for falsifiability at all.")

    consts = [n for n in names if supports[n].constant]
    notes: List[str] = []
    if not names:
        notes.append("the rule mentions no metric at all, so its value is fixed by construction")

    # --- can it be enumerated exactly? --------------------------------------------------------
    sizes = [supports[n].enumerable for n in names]
    total = 1
    exact = True
    for s in sizes:
        if s is None:
            exact = False
            break
        total *= s
        if total > cap:
            exact = False
            break

    thresholds = _thresholds(node)
    pos = BranchResult(UNKNOWN)
    neg = BranchResult(UNKNOWN)

    if exact and names:
        grids = [supports[n].points() for n in names]
        for combo in itertools.product(*grids):
            env = dict(zip(names, combo))
            try:
                v = eval_exact(node, env)
            except ZeroDivisionError:
                continue
            if v != 0 and pos.status != REACHABLE:
                pos = BranchResult(REACHABLE, dict(env), "witness found by exhaustive enumeration")
            elif v == 0 and neg.status != REACHABLE:
                neg = BranchResult(REACHABLE, dict(env), "witness found by exhaustive enumeration")
            if pos.status == REACHABLE and neg.status == REACHABLE:
                break
        n_pts = total
        notes.append(f"exhaustive over {n_pts} assignment(s) — every point in the declared support "
                     f"was evaluated, so an absent branch is absent, not merely unfound")
        for br, label in ((pos, "the finding"), (neg, "the null")):
            if br.status != REACHABLE:
                br.status = UNREACHABLE
                br.reason = (f"no assignment in the declared support makes {label} fire; "
                             f"all {n_pts} were checked.")
        return FalsifiabilityReport(rule, pos, neg, True, names, consts, notes)

    # --- witness search, then interval proof of emptiness --------------------------------------
    if names:
        probe_sets = [supports[n].probes(thresholds) for n in names]
        budget = 1
        for ps in probe_sets:
            budget *= max(1, len(ps))
        if budget > cap:
            probe_sets = [ps[:8] for ps in probe_sets]
            notes.append("probe grid truncated to 8 values per metric to stay inside the budget; "
                         "an UNKNOWN below may reflect that truncation, not the rule")
        for combo in itertools.product(*probe_sets):
            env = dict(zip(names, combo))
            try:
                v = eval_exact(node, env)
            except ZeroDivisionError:
                continue
            if v != 0 and pos.status != REACHABLE:
                pos = BranchResult(REACHABLE, dict(env), "witness found by probe search")
            elif v == 0 and neg.status != REACHABLE:
                neg = BranchResult(REACHABLE, dict(env), "witness found by probe search")
            if pos.status == REACHABLE and neg.status == REACHABLE:
                break

    if pos.status != REACHABLE or neg.status != REACHABLE:
        env_iv = {n: supports[n].interval() for n in names}
        try:
            whole = eval_interval(node, env_iv)
        except (KeyError, ZeroDivisionError):
            whole = MAYBE
        if whole == FALSE and pos.status != REACHABLE:
            pos = BranchResult(UNREACHABLE, None,
                               "interval evaluation over the entire declared support proves the "
                               "rule is false everywhere.")
        elif whole == TRUE and neg.status != REACHABLE:
            neg = BranchResult(UNREACHABLE, None,
                               "interval evaluation over the entire declared support proves the "
                               "rule is true everywhere.")
        for br in (pos, neg):
            if br.status == UNKNOWN:
                br.reason = ("no witness was found and emptiness could not be proven; the "
                             "analysis is inconclusive, which is not the same as safe")
    if not exact:
        notes.append("NOT exhaustive: at least one metric has a continuous or very large support, "
                     "so a REACHABLE verdict rests on an exhibited witness and an UNREACHABLE one "
                     "on an interval proof over the whole domain")
    return FalsifiabilityReport(rule, pos, neg, False, names, consts, notes)
