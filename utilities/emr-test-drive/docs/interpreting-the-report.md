# Interpreting the report

The report answers four questions independently — did the data come out right,
did the operation work, did it get faster or slower, and what did it cost — and
rolls them up into one overall verdict per comparison. This page explains every
verdict value the report can show and what to do about it, and how to navigate
between the variant pairs a run produced.

## Overall verdicts

Each comparison gets exactly one of these, shown as the verdict banner:

| Verdict | Meaning | What to do |
|---|---|---|
| `PROCEED` | No correctness, functional, or performance regressions were detected. | Safe to move forward on the evidence gathered — this is not a guarantee that nothing else could ever break, only that this workload, on this data, showed no issue. |
| `CAUTION` | Something needs a human look before proceeding: a pre-existing failure on both sides, or a performance regression that crossed the alert threshold but nothing more severe. | Read the reasons listed under the banner; decide whether the flagged items are acceptable for your workload. |
| `BLOCK` | A new correctness finding, a new functional failure, or a new timeout was detected. | Do not proceed on this evidence without investigating. These are the categories the tool treats as unambiguously bad: something that used to work, or used to be correct, no longer is. |
| `INDETERMINATE` | The variant pair differs in more than one dimension (`UNMATCHED`), so no clean upgrade verdict can be drawn — only observations. | Use the comparison for qualitative information, not as a gate. If you need a real verdict, construct a pair that differs in exactly one dimension (see [docs/configuration.md](configuration.md#the-one-dimension-rule)). |

The banner also lists the specific reasons behind the level — new correctness
findings, new failures, regressions past the alert threshold, pre-existing
failures carried by both sides, and (separately, as good news) counts of
correctness findings resolved on the candidate and operations that went from
failing to passing.

## Correctness verdicts

Correctness is checked first and given the highest priority, because a faster
wrong answer is not an improvement. These are per-unit findings, not
pass/fail states like the functional ones — a unit can be functionally
`SUCCESS` and still carry a correctness finding, which is exactly the failure
mode this category exists to catch.

| Verdict | Meaning |
|---|---|
| `SILENT_DATA_LOSS` | The operation reported success (exit 0), but the table's commit log did not advance. The job told you it worked; the data says otherwise. This is the most severe correctness finding the tool produces. |
| `DIVERGENT_RESULT` | The same operation, on the same data, produced a different result-set checksum on the candidate than on the baseline, and neither side's difference is explained by a legitimate status change (see below). |
| `ORPHANED_DATA` | An overwrite left prior-generation files behind at the table's storage prefix. Left unresolved, reads against that table can return duplicate or stale rows. |
| `CORRECTNESS_FIXED` | A defect that was present on the baseline (silent data loss or orphaned data) is no longer reproducible on the candidate. Reported as good news, not as a divergence. |

Two details worth knowing about how these are computed, because they explain
why the tool doesn't produce a flood of false positives on every upgrade that
also fixes something:

- If an operation's pass/fail status **changed** between baseline and candidate
  for a given table format (for example, an operation that failed on the
  baseline now succeeds on the candidate), every later operation against that
  same table legitimately sees different underlying state. A checksum
  difference downstream of that status change is a cascade of the fix, not a
  new divergence, and is not reported as `DIVERGENT_RESULT`.
- If the baseline itself exhibited a defect that the candidate no longer does,
  the comparison reports that as `CORRECTNESS_FIXED` rather than as
  `DIVERGENT_RESULT` against a broken baseline — which would otherwise be
  exactly backwards, flagging an improvement as if it were new corruption.

A finding can also be marked `pre_existing: true` when the same defect is
present on both variants — carried forward rather than introduced by the
candidate. Pre-existing findings are still shown, but they don't drive the
overall verdict to `BLOCK` on their own; a brand-new critical finding does.

## Functional verdicts

These come from comparing each `(operation, table_format)` unit's observed
status against the *expected* support state for that combination, not from a
plain pass/fail. The expected state comes from the bundled support matrices,
transcribed from AWS documentation — see [docs/lake-formation.md](lake-formation.md)
for how that works under Lake Formation modes specifically.

| Verdict | Meaning |
|---|---|
| `NEW_FAILURE` | Passed on the baseline, fails on the candidate. The headline number for an upgrade or patch comparison — this is what "did the upgrade break something" looks like. |
| `STABLE_FAIL` | Fails on both sides, despite being documented as supported. A pre-existing problem, not something the candidate introduced — but still worth knowing about. |
| `FLAKY` | Inconsistent across iterations on the same variant. Treated separately from a clean pass or fail because a flaky result can't be trusted as evidence either way. |
| `FIXED` | Failed on the baseline, passes on the candidate, with no version-gate change in expectation. |
| `FIXED_BY_RELEASE` | Failed on the baseline because the operation was documented as unsupported there, and now passes because the candidate's release crosses the documented version gate that added support. |
| `EXPECTED_UNSUPPORTED` | Both sides fail, and both sides are documented as unsupported for this format/operation/access-mode/release combination. This is correct behaviour, not a regression — the whole point of diffing against expectation instead of pass/fail. |
| `EXPECTED_REMOVED` | The baseline was documented as supported and the candidate is documented as unsupported. A real change, flagged for visibility, but distinguished from an unplanned regression because it matches a documented change rather than contradicting one. |
| `STABLE_PASS` | Passes on both sides. The common case, and not called out further. |
| `NOT_COMPARABLE` | One side reports a status of "not applicable" — for example, an operation that doesn't apply to the current table type. No verdict is drawn. |

`MISSING` also appears if a unit exists in one variant's results but not the
other's — for example a workload that only ran on one side.

Alongside these, failures that carry an error message are grouped into
**clusters**: the error text is normalized (hex ids, request ids, S3 paths,
ARNs, timestamps, and bare numbers are replaced with placeholders) and grouped
by the resulting signature, so that many individual failures collapse into a
small number of root causes, each with a representative example and a member
list. This is what keeps a report on a large operation matrix readable instead
of listing forty failures that are really three problems.

## Performance verdicts

| Verdict | Meaning |
|---|---|
| `REGRESSION` | The candidate is slower than the baseline by more than the effective noise band. Severity is `high` if the delta is at or beyond `perf_regression_alert_pct`, otherwise `medium`. |
| `IMPROVEMENT` | The candidate is faster than the baseline by more than the effective noise band. |
| `NEUTRAL` | The delta is within the configured noise band. |
| `WITHIN_NOISE` | The delta is within the *effective* band, but only because the effective band had to widen beyond the configured value to accommodate observed run-to-run spread on one side or the other. Distinguished from plain `NEUTRAL` so you can see when the band itself, not just the delta, is doing the work. |
| `OVERHEAD` | The candidate is slower, and the comparison's `intent` is `governance_overhead` — see "Intent matters" below. |
| `NEW_TIMEOUT` | The query completed on the baseline but timed out on the candidate. Always treated as critical, regardless of intent. |
| `RESOLVED_TIMEOUT` | The reverse: timed out on the baseline, completed on the candidate. |
| `INSUFFICIENT_DATA` | Fewer than `thresholds.min_iterations_for_perf_verdict` iterations were available on one or both sides, so no verdict is drawn — reported with the raw delta anyway, for reference, but not scored. |
| `NO_DATA` | No usable timing on one or both sides at all. |

Every performance row also carries the **effective band** actually applied
(the max of the configured band and the observed within-job and between-job
spread on each side — see [docs/methodology.md](methodology.md)) and the raw
iteration lists, so a reader can see exactly why a given delta was or wasn't
called a regression, rather than having to trust the verdict label alone.

Performance verdicts only drive the overall verdict when the pair is
`MATCHED` (see match statuses below) — an `UNMATCHED` pair suppresses the
performance verdict entirely, because a timing delta between two things that
differ in more than one way can't be attributed to any one of them.

### Intent matters: why the same slowdown is `OVERHEAD`, not `REGRESSION`

Every comparison declares an `intent`: `upgrade_regression`,
`patch_validation`, or `governance_overhead`. Intent changes how an identical
measured slowdown is classified. Under `upgrade_regression` or
`patch_validation`, a candidate that's meaningfully slower than the baseline is
a `REGRESSION` — something got worse and that's a problem to investigate. Under
`governance_overhead`, the exact same measured slowdown is reported as
`OVERHEAD` instead, and is explicitly excluded from the pass/fail verdict.

This isn't a cosmetic relabeling. A `governance_overhead` comparison is, by
construction, comparing a variant with some access control enabled (Lake
Formation FTA or FGAC) against one without it. Slower is the expected, and
often unavoidable, price of enforcing that access control — filtering every
row, column, or cell through an additional access-control layer costs
something. Calling that cost a "regression" would tell whoever reads the report
to treat their own access-control enforcement as a bug to be fixed, which is
exactly the wrong message to send. The report still tells you the size of that
cost — the aggregate section reports a geomean overhead percentage — it just
doesn't let that cost fail a build or block a decision the way a real
regression does.

`NEW_TIMEOUT` is the one performance verdict that stays critical regardless of
intent: a query that used to finish and now never does is a problem worth
knowing about even if you were expecting some overhead.

## Match statuses

Every variant pair, not just the ones you declared under `comparisons:`, is
diffed against the one-dimension rule (see
[docs/configuration.md](configuration.md#the-one-dimension-rule)) and given a
match status:

| Status | Meaning |
|---|---|
| `MATCHED` | The two variants differ in exactly one of the primary dimensions (deployment model, release label, architecture, access mode, shape, patch). The performance verdict is valid and drives the overall verdict. |
| `UNMATCHED_BY_DESIGN` | The variants differ in more than one dimension, but the comparison declared a `sizing_caveat` explaining why — for example, an access-mode change that necessarily also changes the shape (FGAC's second driver). The performance numbers are still shown, explicitly labelled as overhead rather than a matched benchmark, and don't drive the pass/fail verdict. |
| `UNMATCHED` | The variants differ in more than one dimension with no declared explanation. The functional verdict still stands, but the performance verdict is suppressed — a delta observed here can't be attributed to any single cause, so reporting one as a regression or improvement would be asserting more than the data supports. |

A `config_hash` difference between two variants is tracked separately as an
**advisory** note, not counted against the match — a Spark configuration hash
is expected to change as a *consequence* of a release-label change, so treating
it as an independent dimension would produce false `UNMATCHED` verdicts on
ordinary upgrade comparisons.

## Navigating the report: the three-dropdown picker

The report doesn't show one fixed comparison — it precomputes **every ordered
pair** of variants in the run and lets you pick which one to view through three
controls: a source release, a candidate release, and an access mode selector
(which carries both sides at once, so switching "PLAIN → LF-FGAC" is one
choice rather than two separate ones). Selecting a combination resolves to a
specific precomputed variant pair and reveals its section of the report; every
other pair stays hidden until selected.

Not every combination the three dropdowns can express necessarily has data
behind it — a run typically doesn't include every release crossed with every
access mode. When you select a combination that has no corresponding variant in
the run, **the report says so explicitly**, naming which side (source or
candidate) has no variant for the selected access mode, rather than showing a
blank or stale section. If both dropdowns resolve to the same variant, it says
that too, and prompts you to change one of the selections. This is deliberate:
a report that silently shows the last-viewed comparison when you pick an
unavailable one would be indistinguishable from a report that has data for
every combination, which is not true of any real run.
