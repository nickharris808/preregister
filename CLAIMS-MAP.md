# CLAIMS-MAP — `preregister`

**Tag: CLEAN.** This package does not practise any claim in the associated patent family.

## The boundary

Every method independent in the family terminates in a **physical actuation step** — admitting a
request, provisioning at a floor, binding a key, refusing a settlement. A tool that computes
something and prints it does not practise those claims. A tool that acts on the result does.

`preregister` computes and prints. Specifically:

| it does | it does not |
|---|---|
| parse a decision rule and the declared support of each metric | control any system under test |
| decide reachability of both branches, by witness or interval proof | admit, refuse, provision or actuate anything |
| hash a canonical serialisation of the plan | sign anything, or bind an identity to it |
| compare observed values against the sealed rule | gate a deploy, a merge, or a payment on the result |

Its exit code is advisory. Nothing downstream is required to honour it, and the package ships no
mechanism that would enforce it.

## The nearest claim family, and the step not taken

The family includes claims about **refusing an outcome on the basis of a recorded comparison** —
for example refusing to settle a dispute when a claimed quantity exceeds a recomputed one. The
structural resemblance is real: `preregister score` performs a recorded comparison and reports
`REFUSED`.

The step not taken is the actuation. `preregister` **prints** `REFUSED` and exits 2. It does not
withhold a payment, block a pipeline, revoke an artifact, or bind the refusal into a record that
any other system consults. The refusal is a statement, not an action, and the claims recite the
action.

A downstream user who wires `preregister score` into a gate that blocks a release is performing
that step themselves. That is their decision and their exposure; this package neither ships nor
documents that wiring.

## Provenance

Written for this release. The defect it exists to catch — a sealed pre-registration whose positive
branch was structurally unreachable — is our own, is documented in `oss/AUDIT.md`, and is shipped
as `examples/unfalsifiable.json`.
