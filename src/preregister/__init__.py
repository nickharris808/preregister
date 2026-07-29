"""preregister — seal an experiment plan, and refuse to seal one that cannot fail.

    from preregister import Spec, analyse, seal_spec, score_run

    spec = Spec.from_dict({
        "name": "batch-invariance",
        "hypothesis": "batch composition changes greedy output",
        "decision_rule": "argmax_flips > 0",
        "metrics": {"argmax_flips": {"type": "integer", "lo": 0, "hi": 0}},
    })
    seal_spec(spec)      # SpecError: REFUSING TO SEAL -- the finding can never be reported
"""
from .expr import ParseError, parse
from .falsify import (REACHABLE, UNKNOWN, UNREACHABLE, FalsifiabilityReport, Support, analyse)
from .score import FINDING, NULL, REFUSED, Outcome, score_run
from .seal import SCHEMA, Seal, Spec, SpecError, load_spec, seal_spec, verify_seal

__version__ = "0.1.0"
__all__ = ["Spec", "Support", "SpecError", "ParseError", "parse", "analyse",
           "FalsifiabilityReport", "seal_spec", "verify_seal", "load_spec", "Seal", "SCHEMA",
           "score_run", "Outcome", "FINDING", "NULL", "REFUSED",
           "REACHABLE", "UNREACHABLE", "UNKNOWN", "__version__"]
