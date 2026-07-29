"""seal.py — bind a plan to a commit, and refuse to seal a plan that cannot fail.

A seal is a hash over a CANONICAL serialisation of the spec, so the same plan always seals to the
same digest and any edit changes it. It is not a signature: it proves the plan did not change
between sealing and scoring, not who wrote it. That distinction is stated in the record itself,
because a digest presented as an authenticity guarantee is exactly the "self-consistency is not
authenticity" defect this portfolio measures elsewhere.

THE ORDER MATTERS. Falsifiability is checked BEFORE the digest is computed. A sealed unfalsifiable
plan is worse than an unsealed one: it carries a credential that makes a foregone conclusion look
rigorous.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .falsify import FalsifiabilityReport, Support, analyse

__all__ = ["Spec", "Seal", "SpecError", "seal_spec", "verify_seal", "canonical_bytes", "load_spec"]

SCHEMA = "prereg-seal/v1"


class SpecError(ValueError):
    """The spec is not usable. Always fatal — a partially understood plan is not a plan."""


REQUIRED = ("name", "hypothesis", "metrics", "decision_rule")


@dataclass
class Spec:
    name: str
    hypothesis: str
    decision_rule: str
    metrics: Dict[str, Support]
    finding: str = ""
    null: str = ""
    grid: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Spec":
        if not isinstance(d, dict):
            raise SpecError(f"a spec must be an object, got {type(d).__name__}")
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            raise SpecError(
                f"missing required field(s) {missing}. Each one is load-bearing: `metrics` "
                f"declares what the numbers CAN be, which is the only thing that makes "
                f"`decision_rule` checkable.")
        raw = d["metrics"]
        if not isinstance(raw, dict) or not raw:
            raise SpecError("`metrics` must be a non-empty object mapping each metric to its "
                            "support")
        metrics = {k: Support.from_dict(k, v) for k, v in raw.items()}
        return Spec(name=str(d["name"]), hypothesis=str(d["hypothesis"]),
                    decision_rule=str(d["decision_rule"]), metrics=metrics,
                    finding=str(d.get("finding", "")), null=str(d.get("null", "")),
                    grid=dict(d.get("grid", {})), notes=list(d.get("notes", [])))

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "hypothesis": self.hypothesis,
                "decision_rule": self.decision_rule,
                "metrics": {k: v.to_dict() for k, v in sorted(self.metrics.items())},
                "finding": self.finding, "null": self.null,
                "grid": self.grid, "notes": self.notes}


def load_spec(path: str) -> Spec:
    text = open(path, encoding="utf-8").read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as e:
            raise SpecError(
                "this spec is YAML and PyYAML is not installed: `pip install preregister[yaml]`. "
                "Refusing to guess at the contents of a plan that is about to be sealed.") from e
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SpecError(f"{path} is not valid JSON: {e}") from e
    return Spec.from_dict(data)


def canonical_bytes(obj: Any) -> bytes:
    """Sorted keys, no insignificant whitespace, UTF-8. Reordering a spec must not change its
    digest; changing a single character must."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _git_commit(repo: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_dirty(repo: str) -> Optional[bool]:
    try:
        r = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=15)
        return bool(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class Seal:
    schema: str
    spec: Dict[str, Any]
    digest: str
    falsifiability: Dict[str, Any]
    git_commit: Optional[str] = None
    git_dirty: Optional[bool] = None
    sealed_at: Optional[str] = None
    limits: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}


LIMITS = [
    "This digest proves the plan did not change between sealing and scoring. It does NOT prove "
    "who wrote it, and it is not a signature.",
    "Falsifiability is checked against the SUPPORTS THE SPEC DECLARES. A support that is wrong "
    "about the experiment -- claiming a metric can vary when the configuration pins it -- moves "
    "the error one level up, where this tool cannot see it.",
    "A FALSIFIABLE verdict means both outcomes are reachable. It says nothing about whether the "
    "experiment has the power to distinguish them.",
]


def seal_spec(spec: Spec, *, repo: str = ".", timestamp: Optional[str] = None,
              allow_unfalsifiable: bool = False) -> Seal:
    """Analyse, then seal. Raises SpecError if the rule is not proven falsifiable.

    `allow_unfalsifiable` exists for tests and for the deliberate case of registering a plan you
    have already established cannot fail. It stamps the record, so the escape hatch cannot be
    used quietly.
    """
    report = analyse(spec.decision_rule, spec.metrics)
    limits = list(LIMITS)
    if not report.falsifiable:
        if not allow_unfalsifiable:
            raise SpecError(
                f"REFUSING TO SEAL — {report.verdict}.\n  {report.explain()}\n"
                f"  rule: {spec.decision_rule}\n"
                f"  A seal on this plan would make a foregone conclusion look pre-registered, "
                f"which is worse than no seal at all.")
        limits.insert(0, f"SEALED WITH --allow-unfalsifiable: the rule is {report.verdict}. "
                         f"{report.explain()}")

    body = {"schema": SCHEMA, "spec": spec.to_dict()}
    digest = hashlib.sha256(canonical_bytes(body)).hexdigest()
    return Seal(schema=SCHEMA, spec=spec.to_dict(), digest=digest,
                falsifiability=report.to_dict(), git_commit=_git_commit(repo),
                git_dirty=_git_dirty(repo), sealed_at=timestamp, limits=limits)


def verify_seal(seal: Dict[str, Any]) -> str:
    """Recompute the digest. Returns the recomputed value; raises if it disagrees."""
    if not isinstance(seal, dict) or seal.get("schema") != SCHEMA:
        raise SpecError(f"not a {SCHEMA} record")
    if "spec" not in seal or "digest" not in seal:
        raise SpecError("record is missing `spec` or `digest`")
    got = hashlib.sha256(canonical_bytes({"schema": SCHEMA, "spec": seal["spec"]})).hexdigest()
    if got != seal["digest"]:
        raise SpecError(
            f"DIGEST MISMATCH — the plan changed after it was sealed.\n"
            f"  sealed:     {seal['digest']}\n  recomputed: {got}\n"
            f"  Scoring a run against an edited plan is exactly what a pre-registration exists "
            f"to prevent.")
    return got
