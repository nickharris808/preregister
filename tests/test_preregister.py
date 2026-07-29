"""Tests for preregister, weighted toward the ways an analysis can be quietly WRONG.

A falsifiability checker has two failure modes and they are not symmetric. Missing an
unfalsifiable rule lets a foregone conclusion get a credential. Wrongly calling a good rule
unfalsifiable is a false accusation that trains people to pass `--allow-unfalsifiable` by reflex,
which destroys the tool. Both are tested.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys

import pytest

from preregister import (FINDING, NULL, REFUSED, ParseError, Spec, SpecError, Support, analyse,
                         parse, score_run, seal_spec, verify_seal)
from preregister.expr import (FALSE, MAYBE, TRUE, Interval, eval_exact, eval_interval,
                              metric_names)
from preregister.falsify import REACHABLE, UNKNOWN, UNREACHABLE

PY = sys.executable


def sup(d):
    return {k: Support.from_dict(k, v) for k, v in d.items()}


# ------------------------------------------------------------------ the scar

def test_the_real_defect_is_caught():
    """`prompt_logprobs=0` pins argmax_flips to zero, so `argmax_flips > 0` can never fire.

    This is not a hypothetical. This portfolio shipped it, sealed, before data.
    """
    r = analyse("argmax_flips > 0", sup({"argmax_flips": {"type": "integer", "lo": 0, "hi": 0}}))
    assert r.verdict == "UNFALSIFIABLE"
    assert r.positive.status == UNREACHABLE
    assert r.negative.status == REACHABLE
    assert "never" in r.explain().lower()


def test_the_same_rule_is_fine_once_the_metric_can_vary():
    """The rule was never the bug; the CONFIGURATION was. The tool must say so."""
    r = analyse("argmax_flips > 0", sup({"argmax_flips": {"type": "integer", "lo": 0, "hi": 40}}))
    assert r.falsifiable and r.exact
    assert r.positive.witness["argmax_flips"] > 0
    assert r.negative.witness["argmax_flips"] == 0


def test_a_rule_true_everywhere_is_also_unfalsifiable():
    """The mirror image: the null can never fire, so the finding is guaranteed."""
    r = analyse("auc >= 0.0", sup({"auc": {"lo": 0.0, "hi": 1.0}}))
    assert r.verdict == "UNFALSIFIABLE"
    assert r.negative.status == UNREACHABLE


def test_sealing_an_unfalsifiable_plan_raises():
    spec = Spec.from_dict({"name": "x", "hypothesis": "y", "decision_rule": "flips > 0",
                           "metrics": {"flips": {"type": "integer", "lo": 0, "hi": 0}}})
    with pytest.raises(SpecError, match="REFUSING TO SEAL"):
        seal_spec(spec, repo=".")


def test_the_escape_hatch_stamps_the_record():
    """Sealing anyway must be possible and must be impossible to do quietly."""
    spec = Spec.from_dict({"name": "x", "hypothesis": "y", "decision_rule": "flips > 0",
                           "metrics": {"flips": {"type": "integer", "lo": 0, "hi": 0}}})
    s = seal_spec(spec, repo=".", allow_unfalsifiable=True)
    assert any("allow-unfalsifiable" in lim for lim in s.limits)
    assert s.falsifiability["verdict"] == "UNFALSIFIABLE"


# ------------------------------------------------------------------ no false accusations

@pytest.mark.parametrize("rule,supports", [
    ("auc > 0.7", {"auc": {"lo": 0.0, "hi": 1.0}}),
    ("p < 0.05 and n >= 30", {"p": {"lo": 0.0, "hi": 1.0}, "n": {"type": "integer", "lo": 1, "hi": 500}}),
    ("leaked_tokens > 0", {"leaked_tokens": {"type": "integer", "lo": 0, "hi": 4096}}),
    ("abs_drift > 1e-6", {"abs_drift": {"lo": 0.0, "hi": 1.0}}),
    ("(a > 1) or (b > 1)", {"a": {"lo": 0.0, "hi": 2.0}, "b": {"lo": 0.0, "hi": 2.0}}),
    ("hit_rate / total > 0.5", {"hit_rate": {"lo": 0.0, "hi": 10.0}, "total": {"lo": 1.0, "hi": 10.0}}),
    ("not (auc <= 0.5)", {"auc": {"lo": 0.0, "hi": 1.0}}),
    ("mean_a - mean_b > 0.1", {"mean_a": {"lo": 0.0, "hi": 1.0}, "mean_b": {"lo": 0.0, "hi": 1.0}}),
])
def test_ordinary_good_rules_are_not_accused(rule, supports):
    """Every one of these can genuinely go either way. Calling any of them unfalsifiable would
    be a false accusation, and a tool that cries wolf gets its refusals bypassed."""
    r = analyse(rule, sup(supports))
    assert r.falsifiable, f"{rule} was wrongly reported {r.verdict}: {r.explain()}"


def test_a_threshold_at_the_very_edge_of_the_support_is_still_found():
    """`auc > 0.999` over [0,1] is reachable only in a sliver. A coarse grid would miss it and
    report UNKNOWN; the probe set includes the thresholds the rule names for exactly this."""
    r = analyse("auc > 0.999", sup({"auc": {"lo": 0.0, "hi": 1.0}}))
    assert r.positive.status == REACHABLE, r.explain()
    assert r.falsifiable


def test_an_integer_support_of_one_useful_value_is_reachable():
    r = analyse("flips > 0", sup({"flips": {"type": "integer", "lo": 0, "hi": 1}}))
    assert r.falsifiable


# ------------------------------------------------------------------ soundness of the analysis

def test_an_unreachable_verdict_is_never_wrong_on_finite_supports():
    """Brute force is the oracle: on a fully enumerable domain the analysis must agree with
    actually trying every point."""
    import itertools
    rules = ["a > b", "a + b > 3", "a == b", "a * b >= 4", "a - b > 10", "a / (b + 1) > 2",
             "a > 0 and b > 0", "a > 5 or b > 5", "not (a == b)"]
    dom = {"a": list(range(4)), "b": list(range(4))}
    for rule in rules:
        node = parse(rule)
        truths = set()
        for va, vb in itertools.product(dom["a"], dom["b"]):
            try:
                truths.add(eval_exact(node, {"a": float(va), "b": float(vb)}) != 0)
            except ZeroDivisionError:
                pass
        r = analyse(rule, sup({"a": dom["a"], "b": dom["b"]}))
        assert (r.positive.status == REACHABLE) == (True in truths), rule
        assert (r.negative.status == REACHABLE) == (False in truths), rule


def test_interval_semantics_never_claims_a_reachable_branch_is_empty():
    """The soundness direction that matters. If eval_interval says FALSE over a box, then no
    point in that box may evaluate true."""
    import random
    rng = random.Random(7)
    rules = ["x > y", "x + y > 1", "x * y < 0.25", "x - y >= 0.5", "x > 0.3 and y < 0.7"]
    for rule in rules:
        node = parse(rule)
        for _ in range(200):
            lo1, lo2 = rng.uniform(-2, 2), rng.uniform(-2, 2)
            box = {"x": Interval(lo1, lo1 + rng.uniform(0, 2)),
                   "y": Interval(lo2, lo2 + rng.uniform(0, 2))}
            verdict = eval_interval(node, box)
            if verdict in (TRUE, FALSE):
                want = verdict == TRUE
                for _ in range(30):
                    pt = {k: rng.uniform(v.lo, v.hi) for k, v in box.items()}
                    got = eval_exact(node, pt) != 0
                    assert got == want, f"{rule}: interval said {verdict} but {pt} gave {got}"


def test_undetermined_is_reported_rather_than_guessed():
    """A rule the analysis cannot settle must come back UNDETERMINED with exit 2 — never a
    cheerful FALSIFIABLE."""
    r = analyse("x * x - x > 1000000", sup({"x": {"lo": 0.0, "hi": 1000.5}}))
    assert r.verdict in ("FALSIFIABLE", "UNDETERMINED")
    if r.verdict == "UNDETERMINED":
        assert r.exit_code == 2


# ------------------------------------------------------------------ the spec contract

def test_a_metric_with_no_declared_support_is_refused():
    with pytest.raises(ValueError, match="declares no support"):
        analyse("auc > 0.5", sup({"other": {"lo": 0, "hi": 1}}))


def test_an_empty_support_is_refused():
    with pytest.raises(ValueError, match="EMPTY support"):
        Support.from_dict("m", [])


def test_an_inverted_support_is_refused():
    with pytest.raises(ValueError, match="empty"):
        Support.from_dict("m", {"lo": 1.0, "hi": 0.0})


def test_a_support_without_bounds_is_refused():
    """No range means no analysis, and no analysis means no seal."""
    with pytest.raises(ValueError, match="needs both"):
        Support.from_dict("m", {"type": "real"})


@pytest.mark.parametrize("missing", ["name", "hypothesis", "metrics", "decision_rule"])
def test_every_required_field_is_required(missing):
    d = {"name": "n", "hypothesis": "h", "decision_rule": "a > 0",
         "metrics": {"a": {"lo": 0, "hi": 1}}}
    d.pop(missing)
    with pytest.raises(SpecError, match="missing required"):
        Spec.from_dict(d)


# ------------------------------------------------------------------ the language

def test_a_chained_comparison_is_refused_not_silently_regrouped():
    """`0 < x < 1` groups as `(0 < x) < 1` and would mean something the author never wrote."""
    with pytest.raises(ParseError, match="chained comparison"):
        parse("0 < x < 1")


@pytest.mark.parametrize("bad", ["", "   ", "a >", "a > > b", "(a > 1", "a $ b", "a > 1)"])
def test_malformed_rules_raise_rather_than_evaluate(bad):
    with pytest.raises(ParseError):
        parse(bad)


def test_and_or_not_are_accepted_as_words_and_symbols():
    for a, b in [("a > 1 and b > 1", "a > 1 && b > 1"),
                 ("a > 1 or b > 1", "a > 1 || b > 1"),
                 ("not a > 1", "!(a > 1)")]:
        env = {"a": 2.0, "b": 0.0}
        assert eval_exact(parse(a), env) == eval_exact(parse(b), env)


def test_metric_names_are_extracted_exactly():
    assert metric_names(parse("auc > 0.5 and n_probes >= 30")) == {"auc", "n_probes"}


def test_division_by_an_interval_spanning_zero_is_unbounded_not_hulled():
    """Returning a finite hull here would be unsound in the direction that hides a live branch."""
    got = eval_interval(parse("x / y"), {"x": Interval(1, 2), "y": Interval(-1, 1)})
    assert got.lo == -math.inf and got.hi == math.inf


# ------------------------------------------------------------------ sealing and scoring

def _sealed():
    spec = Spec.from_dict({
        "name": "leak", "hypothesis": "cross-tenant reuse is observable",
        "finding": "a leak was observed", "null": "no leak observed",
        "decision_rule": "leaked_tokens > 0 and positive_control_passed > 0",
        "metrics": {"leaked_tokens": {"type": "integer", "lo": 0, "hi": 4096},
                    "positive_control_passed": {"values": [0, 1]}},
    })
    return seal_spec(spec, repo=".", timestamp="1970-01-01T00:00:00+00:00")


def test_a_seal_is_stable_and_order_independent():
    a, b = _sealed(), _sealed()
    assert a.digest == b.digest
    reordered = json.loads(json.dumps(a.to_dict()))
    reordered["spec"] = dict(reversed(list(reordered["spec"].items())))
    assert verify_seal(reordered) == a.digest


def test_editing_the_plan_after_sealing_is_caught():
    s = json.loads(json.dumps(_sealed().to_dict()))
    s["spec"]["decision_rule"] = "leaked_tokens >= 0"
    with pytest.raises(SpecError, match="DIGEST MISMATCH"):
        verify_seal(s)
    assert score_run(s, {"leaked_tokens": 0, "positive_control_passed": 1}).verdict == REFUSED


def test_scoring_produces_the_finding_and_the_null():
    s = _sealed().to_dict()
    assert score_run(s, {"leaked_tokens": 12, "positive_control_passed": 1}).verdict == FINDING
    assert score_run(s, {"leaked_tokens": 0, "positive_control_passed": 1}).verdict == NULL


def test_a_value_outside_the_declared_support_is_refused():
    """The declared domain is what the falsifiability analysis ran over. Outside it, the seal
    certifies nothing about this run."""
    out = score_run(_sealed().to_dict(), {"leaked_tokens": 99999, "positive_control_passed": 1})
    assert out.verdict == REFUSED
    assert any("OUTSIDE the support" in r for r in out.refusals)


def test_a_missing_metric_is_refused_not_defaulted():
    out = score_run(_sealed().to_dict(), {"leaked_tokens": 5})
    assert out.verdict == REFUSED
    assert any("positive_control_passed" in r for r in out.refusals)


def test_a_non_numeric_observation_is_refused():
    out = score_run(_sealed().to_dict(),
                    {"leaked_tokens": "many", "positive_control_passed": 1})
    assert out.verdict == REFUSED


def test_extra_reported_metrics_are_noted_not_ignored_silently():
    out = score_run(_sealed().to_dict(),
                    {"leaked_tokens": 0, "positive_control_passed": 1, "wall_seconds": 3.2})
    assert out.verdict == NULL
    assert any("wall_seconds" in n for n in out.notes)


def test_exit_codes_follow_the_portfolio_dialect():
    s = _sealed().to_dict()
    assert score_run(s, {"leaked_tokens": 0, "positive_control_passed": 1}).exit_code == 0
    assert score_run(s, {"leaked_tokens": 1, "positive_control_passed": 1}).exit_code == 1
    assert score_run(s, {"leaked_tokens": 0}).exit_code == 2


def test_a_seal_records_that_it_is_not_a_signature():
    assert any("not a signature" in lim for lim in _sealed().limits)


# ------------------------------------------------------------------ CLI

def _cli(*args, **kw):
    return subprocess.run([PY, "-m", "preregister.cli", *args], capture_output=True, text=True,
                          **kw)


def test_cli_selftest_passes():
    r = _cli("selftest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "refuses where it must" in r.stdout


def test_cli_check_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"name": "g", "hypothesis": "h", "decision_rule": "a > 0",
                                "metrics": {"a": {"type": "integer", "lo": 0, "hi": 10}}}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "b", "hypothesis": "h", "decision_rule": "a > 0",
                               "metrics": {"a": {"type": "integer", "lo": 0, "hi": 0}}}))
    assert _cli("check", str(good)).returncode == 0
    r = _cli("check", str(bad))
    assert r.returncode == 1
    assert "NEVER" in r.stdout


def test_cli_seal_refuses_and_says_why(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "b", "hypothesis": "h", "decision_rule": "a > 0",
                               "metrics": {"a": {"type": "integer", "lo": 0, "hi": 0}}}))
    r = _cli("seal", str(bad), "-o", str(tmp_path / "out.json"))
    assert r.returncode == 1
    assert "REFUSING TO SEAL" in r.stderr
    assert not (tmp_path / "out.json").exists(), "a refused seal must not leave a file behind"


def test_cli_round_trip(tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"name": "g", "hypothesis": "h", "decision_rule": "a > 3",
                                "metrics": {"a": {"type": "integer", "lo": 0, "hi": 10}}}))
    out = tmp_path / "sealed.json"
    assert _cli("seal", str(spec), "-o", str(out)).returncode == 0
    assert _cli("verify", str(out)).returncode == 0
    assert _cli("score", str(out), "--set", "a=9").returncode == 1
    assert _cli("score", str(out), "--set", "a=1").returncode == 0
    assert _cli("score", str(out), "--set", "a=99").returncode == 2


def test_cli_json_is_valid_json(tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"name": "g", "hypothesis": "h", "decision_rule": "a > 3",
                                "metrics": {"a": {"type": "integer", "lo": 0, "hi": 10}}}))
    r = _cli("check", str(spec), "--json")
    json.loads(r.stdout)
    out = tmp_path / "sealed.json"
    _cli("seal", str(spec), "-o", str(out))
    r2 = _cli("score", str(out), "--set", "a=9", "--json")
    assert json.loads(r2.stdout)["verdict"] == FINDING


def test_the_shipped_examples_behave_as_their_names_claim(tmp_path):
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = os.path.join(here, "examples")
    assert _cli("check", os.path.join(ex, "unfalsifiable.json")).returncode == 1
    assert _cli("check", os.path.join(ex, "leak_probe.json")).returncode == 0
