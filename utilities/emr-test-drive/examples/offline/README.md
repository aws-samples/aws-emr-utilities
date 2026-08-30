# EMR Test Drive — worked example (synthetic fixtures)

> For a **real, measured** run see `../runs/bda-upgrade/*/out/report.html`, produced live against
> EMR Serverless 7.11.0 vs 7.13.0. This directory exercises the same compare/report code against
> synthetic fixtures, which is useful for iterating on report design without spending money.

A runnable preview of the **compare + report** layer, so the report and dashboard design can be
reviewed and iterated on before any AWS resources are provisioned.

```bash
cd emr-test-drive/example
python3 make_fixtures.py          # generate synthetic result units
python3 generate.py --open        # build out/report.html + out/report.json and open it
```

No dependencies. Python 3.9+ standard library only. No AWS calls, no credentials, no cost.

---

## What is real and what is not

| | Provenance |
|---|---|
| `../etd/matrices/lf_fgac.json` | **REAL.** The Lake Formation FGAC support matrix transcribed from AWS documentation (retrieved 2026-08-13): four formats, ~20 operations each, three support states, plus the release gates. This is the expected-state matrix the engine diffs against |
| `../etd/compare.py`, `../etd/report.py` | **REAL logic.** A working implementation of the four diffs, variant matching, error clustering, verdicts and rendering |
| `fixtures/units/*.json` | **SYNTHETIC.** Deterministic fixtures from `make_fixtures.py`. Not measurements |
| `fixtures/run_manifest.json` | Synthetic scenario, but the variant/comparison structure is the real proposed schema |

The report itself carries a `SAMPLE DATA` ribbon and a data-provenance banner so it cannot be
mistaken for a measurement.

## The scenario it renders

A customer on `emr-7.11.0` with Lake Formation **FGAC** moving to `emr-7.13.0`. That upgrade crosses
two documented boundaries: 7.12 (DML/DDL switch from runtime-role credentials to LF vended
credentials) and 7.10 (Delta/Hudi Spark configuration changes). They also want the cost of governance
and a patch validated.

Five variants, two workloads, five comparisons:

| Comparison | Intent | Match | Outcome |
|---|---|---|---|
| 7.11 FGAC → 7.13 FGAC | upgrade | MATCHED | **BLOCK** — 1 new failure, 1 silent data loss, 3 regressions, 1 timeout, 6 ops fixed |
| 7.13 FGAC → +patch | patch validation | MATCHED | **PROCEED** — patch resolves both defects |
| 7.13 plain → FTA | governance | MATCHED | **PROCEED** — 19 queries slower, labelled OVERHEAD not regression |
| 7.13 FTA → FGAC | governance | UNMATCHED_BY_DESIGN | **BLOCK** — FGAC-specific failure + correctness findings; sizing deliberately differs |
| 7.11 FGAC → 7.13 plain | upgrade | UNMATCHED | **INDETERMINATE** — 3 dimensions differ, no verdict possible |

## What the example demonstrates

**Expected-state diffing.** 14 operations fail on the candidate *by design* (Hudi `CTAS`, Iceberg
`DataFrame Writer V1`, Delta `CREATE TABLE LIKE`, Hive `LOAD DATA`…). They are reported
`EXPECTED UNSUPPORTED`, not as regressions. A harness without this reports 14 false failures.

**Release-gate awareness.** Delta `CREATE_TABLE` and Iceberg/Delta `DELETE`/`UPDATE`/`MERGE` are
documented as unsupported at or before 7.11, so on the upgrade they show `FIXED BY RELEASE` — the
upgrade's genuine wins appear alongside its costs.

**Correctness before performance.** Ranked above perf: an operation that exits 0 with 3,200 of 5,000
rows readable because the commit log never advanced, and an overwrite that leaves 48 orphaned
objects. Both are invisible to an exit-status gate.

**Resolved-defect detection.** In the patch comparison the baseline is the broken side. Naively
diffing checksums would report the *fixed* candidate as divergent; the engine emits
`CORRECTNESS_FIXED` instead.

**Intent-aware performance.** Enabling FTA makes 19 queries slower. That is the price of governance,
not a regression — labelled `OVERHEAD` and excluded from the verdict. Reporting it as a regression
would tell the customer to stop enforcing access control.

**Statistical honesty.** Best-of-N, ±5% noise band shaded on the chart, per-variant run-to-run spread
published. When observed spread (5.9%) exceeds the configured band (5%), the report says so and tells
you to widen the band or add iterations. Geomean (−0.2%) and total time (+2.4%) disagree here because
the regressions land on the expensive queries — both are shown rather than picking the flattering one.

**Unmatched detection.** Comparing across three dimensions yields `INDETERMINATE`, not a fake verdict.

**Error clustering.** Failures collapse into 2 root causes with a representative stack trace and the
affected operations, instead of a wall of rows.

## Report layout

```
Header            run metadata · scenario · data provenance
Variant dashboard every variant: pass rate, documented-unsupported count,
                  TPC-DS total, timeouts, cost, vs-baseline  +  2 bar charts
Comparison tabs   one per pair, verdict dot in the tab
  Verdict banner  BLOCK / CAUTION / PROCEED / INDETERMINATE + reasons
  Correctness     finding cards: evidence table, note, repro command
  Functional      6 KPIs · format×operation heatmap · error clusters · filterable table
  Performance     6 KPIs · noise-band waterfall · per-query detail with iterations
  Cost            KPIs · bars · vCPU-hr / GB-hr / $ table
  Environments    match status · full variant specs side by side
Footer            methodology and provenance
```

## CI gate

```bash
python3 generate.py --fail-on new_failure,correctness,regression,timeout
# exit 1, and prints:
#   cmp-upgrade-fgac: 1 new functional failure(s)
#   cmp-upgrade-fgac: 1 new correctness finding(s) (SILENT_DATA_LOSS)
#   cmp-upgrade-fgac: 3 query regression(s)
#   cmp-upgrade-fgac: 1 new timeout(s)
```

Only `upgrade_regression` and `patch_validation` comparisons gate; governance overhead never fails a
build.

## Files

```
fixtures/
  run_manifest.json             variants, workloads, comparisons, pricing, thresholds
  units/                        generated result units (10 files: 5 variants x 2 workloads)
make_fixtures.py                deterministic synthetic fixture generator
generate.py                     CLI: build report, --open, --fail-on
out/                            report.html, report.json  (generated)
```

## Not implemented here

Everything upstream of the results: the provider layer (provision / submit / monitor / teardown),
the orchestrator, the CDK stacks, and the web front end. Those are specified in
[../02-DESIGN.md](../02-DESIGN.md) and sequenced in [../04-PLAN.md](../04-PLAN.md). The web UI
renders the same data as these two files.

## Iterating on the design

- **Change the scenario** → edit `fixtures/run_manifest.json` (variants, comparisons, thresholds).
- **Change what defects appear** → edit `DEFECTS` in `make_fixtures.py`.
- **Change verdict rules** → `overall_verdict`, `compare_perf`, `compare_functional` in `../etd/compare.py`.
- **Change the look** → `CSS`, `svg_waterfall`, `svg_hbars`, `heatmap` in `../etd/report.py`.
- **Change the noise band** → `thresholds` in the manifest; the report re-derives all verdicts.
