"""cli.py — preregister check | seal | verify | score | selftest.

Exit codes follow the portfolio dialect throughout: 0 checked and holds · 1 checked and fails ·
2 NOT checked. `2` never means "mild failure"; it means no conclusion was reached.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .falsify import Support, analyse
from .score import score_run
from .seal import Spec, SpecError, load_spec, seal_spec, verify_seal

PROG = "preregister"


def _die(msg: str, code: int = 2) -> "int":
    print(msg, file=sys.stderr)
    return code


def _load(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        print(f"no such file: {path}", file=sys.stderr)
        return None
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except json.JSONDecodeError as e:
        print(f"{path} is not valid JSON: {e}", file=sys.stderr)
        return None


def cmd_check(args: argparse.Namespace) -> int:
    """Analyse a spec's rule WITHOUT sealing it — the loop you want while drafting."""
    try:
        spec = load_spec(args.spec)
        report = analyse(spec.decision_rule, spec.metrics)
    except (SpecError, ValueError, OSError) as e:
        return _die(str(e))

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return report.exit_code

    print(f"{report.verdict} — {spec.name}")
    print(f"  rule            {report.rule}")
    print(f"  analysis        {'exhaustive' if report.exact else 'witness + interval proof'}")
    for label, br in (("finding fires", report.positive), ("null fires", report.negative)):
        mark = {"REACHABLE": "yes", "UNREACHABLE": "NEVER", "UNKNOWN": "unknown"}[br.status]
        print(f"  {label:<15} {mark}")
        if br.witness:
            pretty = ", ".join(f"{k}={v:g}" for k, v in sorted(br.witness.items()))
            print(f"    witness       {pretty}")
        elif br.reason:
            print(f"    why           {br.reason}")
    if report.constant_metrics:
        print(f"  CONSTANT        {', '.join(report.constant_metrics)} "
              f"(declared support has exactly one value)")
    for n in report.notes:
        print(f"  note            {n}")
    print(f"\n{report.explain()}")
    return report.exit_code


def cmd_seal(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec)
        stamp = args.timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        sealed = seal_spec(spec, repo=args.repo, timestamp=stamp,
                           allow_unfalsifiable=args.allow_unfalsifiable)
    except (SpecError, ValueError, OSError) as e:
        return _die(str(e), 1 if "REFUSING TO SEAL" in str(e) else 2)

    out = json.dumps(sealed.to_dict(), indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"sealed {spec.name}")
        print(f"  digest      {sealed.digest}")
        print(f"  commit      {sealed.git_commit or 'not a git repository'}")
        if sealed.git_dirty:
            print("  WARNING     the working tree is DIRTY, so `git_commit` does not describe "
                  "the code that will run")
        print(f"  written     {args.out}")
    else:
        print(out)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    seal = _load(args.seal)
    if seal is None:
        return 2
    try:
        digest = verify_seal(seal)
    except SpecError as e:
        return _die(str(e), 1)
    print(f"SEAL INTACT — {digest}")
    fal = seal.get("falsifiability", {})
    print(f"  rule        {fal.get('rule', '?')}")
    print(f"  verdict     {fal.get('verdict', '?')}")
    print(f"  commit      {seal.get('git_commit') or 'unrecorded'}")
    for lim in seal.get("limits", []):
        print(f"  limit       {lim}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    seal = _load(args.seal)
    if seal is None:
        return 2
    observed: Dict[str, Any]
    if args.results:
        loaded = _load(args.results)
        if loaded is None:
            return 2
        observed = loaded
    else:
        observed = {}
        for kv in args.set or []:
            if "=" not in kv:
                return _die(f"--set expects name=value, got {kv!r}")
            k, v = kv.split("=", 1)
            observed[k.strip()] = v.strip()
    if not observed:
        return _die("no observed values: pass a results JSON or one or more --set name=value")

    outcome = score_run(seal, observed)
    print(json.dumps(outcome.to_dict(), indent=2) if args.json else outcome.render())
    return outcome.exit_code


def cmd_selftest(args: argparse.Namespace) -> int:
    """The tool pointed at its own scar: the rule that could never fire."""
    checks: List[tuple] = []

    unreachable = Spec.from_dict({
        "name": "the real one", "hypothesis": "batch composition changes greedy output",
        "decision_rule": "argmax_flips > 0",
        "metrics": {"argmax_flips": {"type": "integer", "lo": 0, "hi": 0}},
    })
    r = analyse(unreachable.decision_rule, unreachable.metrics)
    checks.append((r.verdict == "UNFALSIFIABLE" and r.positive.status == "UNREACHABLE",
                   "a rule whose metric is pinned to 0 is caught: `argmax_flips > 0` can never "
                   "fire"))
    try:
        seal_spec(unreachable, repo=".")
        checks.append((False, "sealing an unfalsifiable plan was REFUSED"))
    except SpecError:
        checks.append((True, "sealing an unfalsifiable plan was REFUSED"))

    ok = Spec.from_dict({
        "name": "the fixed one", "hypothesis": "same, with logprobs actually requested",
        "decision_rule": "argmax_flips > 0",
        "metrics": {"argmax_flips": {"type": "integer", "lo": 0, "hi": 40}},
    })
    r2 = analyse(ok.decision_rule, ok.metrics)
    checks.append((r2.falsifiable and r2.exact,
                   "the same rule over a support that can actually vary is FALSIFIABLE, "
                   "exhaustively"))

    always = Spec.from_dict({
        "name": "guaranteed", "hypothesis": "auc beats chance",
        "decision_rule": "auc >= 0.0", "metrics": {"auc": {"lo": 0.0, "hi": 1.0}},
    })
    r3 = analyse(always.decision_rule, always.metrics)
    checks.append((r3.verdict == "UNFALSIFIABLE" and r3.negative.status == "UNREACHABLE",
                   "a rule that is true over its whole support is caught too — the NULL can "
                   "never fire"))

    s = seal_spec(ok, repo=".", timestamp="1970-01-01T00:00:00+00:00")
    tampered = json.loads(json.dumps(s.to_dict()))
    tampered["spec"]["decision_rule"] = "argmax_flips >= 0"
    out = score_run(tampered, {"argmax_flips": 0})
    checks.append((out.verdict == "REFUSED" and out.exit_code == 2,
                   "editing the plan after sealing is caught at scoring time"))

    out2 = score_run(s.to_dict(), {"argmax_flips": 999})
    checks.append((out2.verdict == "REFUSED",
                   "an observed value outside the declared support is REFUSED, not scored"))

    out3 = score_run(s.to_dict(), {"unrelated": 1})
    checks.append((out3.verdict == "REFUSED",
                   "a run that never reported the metric the rule reads is REFUSED"))

    for good, label in checks:
        print(f"  [{'ok  ' if good else 'FAIL'}] {label}")
    bad = sum(1 for g, _ in checks if not g)
    print(f"\nRESULT: {'preregister refuses where it must' if not bad else f'{bad} FAILED'}")
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Seal an experiment plan — and refuse to seal one whose conclusion is "
                    "already fixed.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="analyse a spec's decision rule without sealing it")
    c.add_argument("spec")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("seal", help="analyse, then bind the plan to a digest and a commit")
    s.add_argument("spec")
    s.add_argument("-o", "--out", help="write the sealed record here")
    s.add_argument("--repo", default=".", help="repository to read the commit from")
    s.add_argument("--timestamp", help="override the seal timestamp (for reproducible tests)")
    s.add_argument("--allow-unfalsifiable", action="store_true",
                   help="seal anyway, and STAMP the record with the reason it should not have "
                        "been sealed")
    s.set_defaults(func=cmd_seal)

    v = sub.add_parser("verify", help="recompute a seal's digest")
    v.add_argument("seal")
    v.set_defaults(func=cmd_verify)

    sc = sub.add_parser("score", help="apply a sealed rule to observed results")
    sc.add_argument("seal")
    sc.add_argument("-r", "--results", help="JSON object of observed metric values")
    sc.add_argument("--set", action="append", metavar="NAME=VALUE")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_score)

    t = sub.add_parser("selftest", help="point the tool at its own scar")
    t.set_defaults(func=cmd_selftest)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
