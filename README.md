# preregister

**Seal an experiment plan — and refuse to seal one whose conclusion is already fixed.**

[![tests](https://github.com/nickharris808/preregister/actions/workflows/tests.yml/badge.svg)](https://github.com/nickharris808/preregister/actions/workflows/tests.yml)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](LICENSE)

A pre-registration is supposed to stop you from choosing your analysis after seeing the data. It
does nothing at all if the analysis you chose *cannot come out both ways*.

We shipped one of those. The plan was public, sealed before data, and honest. The metric was
`argmax_flips`; the sealed rule was `argmax_flips > 0`. The run was configured with
`prompt_logprobs=0`, which makes `argmax_flips` **identically zero**. The positive branch was
structurally unreachable. The experiment was guaranteed to report the null before a single GPU
started, and the seal made that look like rigour.

`preregister` reads the rule and the declared range of every metric it mentions, and **refuses to
seal a plan whose finding — or whose null — can never fire.**

## Install

```bash
pip install preregister                # zero runtime dependencies
pip install "preregister[yaml]"        # + PyYAML, only if your specs are YAML
```

## 30-second quickstart

```bash
preregister check   plan.json          # analyse the rule; don't seal yet
preregister seal    plan.json -o sealed.json
preregister verify  sealed.json        # recompute the digest
preregister score   sealed.json --set auc=0.91
preregister selftest                   # the tool pointed at its own scar
```

Exit codes are the portfolio dialect: **0** checked and holds · **1** checked and fails ·
**2** NOT checked.

## Worked example — the real defect, verbatim

`examples/unfalsifiable.json` is the plan we actually shipped:

```console
$ preregister check examples/unfalsifiable.json
UNFALSIFIABLE — batch-invariance (as actually shipped, and wrong)
  rule            argmax_flips > 0
  analysis        exhaustive
  finding fires   NEVER
    why           no assignment in the declared support makes the finding fire; all 1 were checked.
  null fires      yes
    witness       argmax_flips=0
  CONSTANT        argmax_flips (declared support has exactly one value)

THE FINDING CAN NEVER BE REPORTED. no assignment in the declared support makes the finding fire;
all 1 were checked. The run is guaranteed to return the null before any data is collected.
$ echo $?
1
```

And a plan that can actually go either way:

```console
$ preregister check examples/leak_probe.json
FALSIFIABLE — cross-tenant KV reuse
  rule            leaked_tokens > 0 and positive_control_tokens > 0
  analysis        witness + interval proof
  finding fires   yes
    witness       leaked_tokens=1, positive_control_tokens=1
  null fires      yes
    witness       leaked_tokens=0, positive_control_tokens=0

both branches are reachable: a witness exists for the finding and for its absence, so the data
can decide
$ echo $?
0
```

Run both yourself: `pip install preregister`, then `preregister selftest`.

## How each answer is established

This is the part that decides whether the tool is worth trusting.

| verdict | how it is established | sound? |
|---|---|---|
| **REACHABLE** | by **exhibiting a witness** — a concrete assignment that makes the branch fire | yes; a witness is a proof |
| **UNREACHABLE** | by **interval evaluation over the entire declared support**, or by exhaustive enumeration when the domain is finite | yes; the interval semantics over-approximates, so an empty result really is empty |
| **UNKNOWN** | neither a witness nor an emptiness proof | — this is reported as `UNDETERMINED`, exit 2 |

`UNDETERMINED` is not "probably fine". The tool will not seal a plan it could not analyse, because
blessing an unanalysed rule is the same failure as blessing an unfalsifiable one, with an extra
step.

## The spec

```json
{
  "name": "cross-tenant KV reuse",
  "hypothesis": "a second tenant can observe cache state created by the first",
  "decision_rule": "leaked_tokens > 0 and positive_control_tokens > 0",
  "metrics": {
    "leaked_tokens":           { "type": "integer", "lo": 0, "hi": 4096 },
    "positive_control_tokens": { "type": "integer", "lo": 0, "hi": 4096 }
  }
}
```

**`metrics` is the real work, and it is not bureaucracy.** Declaring what a number *can* be is
what makes the rule checkable at all — and writing it down is the step at which you notice that
your config pinned it to a constant. That is precisely the moment our own defect would have been
caught.

Supports come in three shapes: an explicit list of `values`, an `integer` range, or a real
interval. Finite and small-integer domains are analysed **exhaustively**; anything else uses
witness search plus an interval proof, and the report says which.

The rule language is deliberately tiny: `< <= > >= == !=`, `and or not`, `+ - * /`, parentheses,
metric names and numbers. `0 < x < 1` is **rejected**, not silently regrouped — it parses as
`(0 < x) < 1` in most languages and would quietly mean something you did not write.

## Scoring refuses in three situations

```console
$ preregister score sealed.json --set leaked_tokens=99999 --set positive_control_tokens=1
REFUSED — the sealed plan does not fit this run
  leaked_tokens=99999.0 is OUTSIDE the support this plan declared ([0.0, 4096.0]). The
  falsifiability analysis was carried out over that domain, so the seal does not certify this run.
```

1. **the plan was edited after sealing** — digest mismatch;
2. **a metric the rule reads was not reported** — a default would decide the outcome by fiat;
3. **an observed value falls outside the declared support** — then either the metric is not what
   the plan thought it was, or the measurement is broken. Scoring it anyway would launder a broken
   measurement through a credential.

## Library use

```python
from preregister import Spec, analyse, seal_spec, score_run, SpecError

spec = Spec.from_dict({
    "name": "batch-invariance", "hypothesis": "batch composition changes greedy output",
    "decision_rule": "argmax_flips > 0",
    "metrics": {"argmax_flips": {"type": "integer", "lo": 0, "hi": 0}},
})

analyse(spec.decision_rule, spec.metrics).verdict     # 'UNFALSIFIABLE'
seal_spec(spec)                                        # SpecError: REFUSING TO SEAL
```

## Honest scope — what a FALSIFIABLE verdict does and does not mean

It means: *both outcomes are reachable over the supports this spec declares, so the data is
capable of deciding between them.* It does **not** mean:

- **that the experiment has the power to tell them apart.** Reachability is not sensitivity.
  `auc > 0.5` over `[0,1]` is falsifiable at n=3 and useless at n=3. This tool does not do a power
  calculation and does not pretend to. See `examples/underpowered.json`.
- **that the declared supports are correct.** If you declare `argmax_flips ∈ [0, 40]` while your
  config pins it to zero, the analysis runs over the wrong domain and reports FALSIFIABLE. The
  error moves one level up, where this tool cannot see it. Declaring supports honestly is the
  discipline; the tool only enforces that you declared them.
- **that the metric measures what you think.** No tool can check that.
- **that the seal proves authorship.** A digest proves the plan did not change between sealing and
  scoring. It is not a signature, and the sealed record says so in its own `limits` field.

## What this does not do

`preregister` **measures and refuses**. It analyses a plan and either seals it or does not. It
never admits, provisions, gates or actuates anything, and it makes no claim about any system under
test. See [CLAIMS-MAP.md](CLAIMS-MAP.md).

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q          # 52 tests
preregister selftest
```

The suite tests both failure directions on purpose. Missing an unfalsifiable rule gives a foregone
conclusion a credential; **wrongly accusing a good rule** trains people to pass
`--allow-unfalsifiable` by reflex, which destroys the tool. Eight ordinary, genuinely-decidable
rules are asserted **not** to be accused, and the interval semantics is checked against brute force
so an `UNREACHABLE` verdict can never be wrong on a finite domain.

## License

Apache-2.0. See [LICENSE](LICENSE) and [CONTRIBUTING.md](CONTRIBUTING.md).

**Citing this?** Metadata is in [CITATION.cff](CITATION.cff) — GitHub's "Cite this repository" button reads it directly.

<!-- PORTFOLIO -->
---

## The rest of the portfolio

25 artifacts, one idea: **a measurement you cannot check is a press release.** Every tool
here reports; none of them gates.

**Tools**

| | |
|---|---|
| [`abstain-bench`](https://github.com/nickharris808/abstain-bench) | how often does a verifier pass input it could not check? |
| [`evidence`](https://github.com/nickharris808/evidence) | run the whole portfolio over your repo — the weakest leg, never the mean |
| [`floorgen`](https://github.com/nickharris808/floorgen) | what must your system remember? an exact lower bound |
| [`formal-proof-mcp`](https://github.com/nickharris808/formal-proof-mcp) | a proof kernel for your coding agent |
| [`gatecount`](https://github.com/nickharris808/gatecount) | exactly how many states does removing this check admit? |
| [`gridlock`](https://github.com/nickharris808/gridlock) | certify a wait-for relation cannot wedge |
| [`honestbench`](https://github.com/nickharris808/honestbench) | measure your CI's escape rate |
| [`kvleak`](https://github.com/nickharris808/kvleak) | cross-tenant leak scanner |
| [`kvprobe`](https://github.com/nickharris808/kvprobe) | model-substitution detector with a measured FPR |
| [`preregister`](https://github.com/nickharris808/preregister) | refuses to seal a plan whose conclusion is already fixed ← you are here |
| [`proof-carrying-ci`](https://github.com/nickharris808/proof-carrying-ci) | the whole portfolio as one CI check, with SARIF |
| [`proof-to-code-drift`](https://github.com/nickharris808/proof-to-code-drift) | fail the build when the proof stops matching |
| [`sf-verify`](https://github.com/nickharris808/sf-verify) | re-derive admission decisions offline |
| [`signoff-cert`](https://github.com/nickharris808/signoff-cert) | certificates that carry their own false-pass bound |
| [`tokencount`](https://github.com/nickharris808/tokencount) | a token count both parties can recompute |

**Benchmarks** — each recomputes one of our own published numbers from its certificate

| | |
|---|---|
| [`illusion-bench`](https://github.com/nickharris808/illusion-bench) | how many broken kernels does your oracle admit? |
| [`kv-reuse-econ-bench`](https://github.com/nickharris808/kv-reuse-econ-bench) | recompute our economics headline |
| [`llm-tenant-isolation-bench`](https://github.com/nickharris808/llm-tenant-isolation-bench) | recompute our isolation figures |

**Datasets**

| | |
|---|---|
| [`abstain-corpus`](https://huggingface.co/datasets/nickh007/abstain-corpus) | 32 inputs a verifier must NOT pass |
| [`kv-reuse-econ-traces`](https://huggingface.co/datasets/nickh007/kv-reuse-econ-traces) | per-workload reuse accounting + the closed form |
| [`kv-tenant-isolation-bench`](https://huggingface.co/datasets/nickh007/kv-tenant-isolation-bench) | isolation observations, uninterpretable rows included |
| [`llm-precision-fingerprints`](https://huggingface.co/datasets/nickh007/llm-precision-fingerprints) | precision-labelled logprobs with a negative control |

**Try it in a browser** — no install, no GPU

| | |
|---|---|
| [`negative-results-atlas`](https://huggingface.co/spaces/nickh007/negative-results-atlas) | ten claims we took back |
| [`tenant-leak-demo`](https://huggingface.co/spaces/nickh007/tenant-leak-demo) | the residency calculator |
| [`wait-for-visualiser`](https://huggingface.co/spaces/nickh007/wait-for-visualiser) | paste a wait-for graph, see the cycle |

### Documentation

Everything above, explained in one place: **<https://nickharris808.github.io/evidence-docs/>** —
the [tutorial](https://nickharris808.github.io/evidence-docs/start/tutorial/),
[what this proves and what it does not](https://nickharris808.github.io/evidence-docs/concepts/what-this-proves/),
and a [CLI reference](https://nickharris808.github.io/evidence-docs/reference/cli/) generated by
running `--help` on every published command.

### The commercial edition

Everything above is **measure-only** and Apache-2.0: it tells you what is true and never acts on
it. The **enforcement** side — binding a partition key at the admission decision, the compiled gate
corpus, and the certificate-*issuing* faucet — is covered by filed patents and licensed separately.

**Reading is free. Enforcing is licensed.**
<!-- /PORTFOLIO -->
