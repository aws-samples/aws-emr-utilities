# Configuration reference

This is a reference for every key in the run configuration YAML, derived from
[config.template.yaml](../config.template.yaml) and the loader/validator in
`etd/spec.py`. Copy `config.template.yaml` as a starting point rather than
writing one from scratch — it has working defaults for everything not marked
required below.

The file has six top-level sections: `run`, `variants`, `comparisons`,
`testbed`, `workloads`, `thresholds`, plus `safety`. This reference follows that
order, with `lake_formation` covered alongside `testbed`/`workloads` where it
applies, since Lake Formation setup is driven by the variants' `access_mode`
rather than by a separate top-level switch in the current schema (see
[docs/lake-formation.md](lake-formation.md) for the distinction and the config
example that touches it, `examples/configs/lake-formation.yaml`).

## `run`

Identity, location, and safety metadata for the whole run.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `name` | yes | — | Short name for this test drive. Used in resource names, S3 prefixes, and tags. Must match `^[a-z0-9][a-z0-9-]{1,30}$`. |
| `region` | yes | — | AWS region. |
| `account` | yes | — | Account ID the run must execute in. Every command verifies the caller's account matches this before doing anything, and refuses to proceed on a mismatch. |
| `bucket` | yes | — | S3 bucket for assets, test-bed data, logs, and results. Must already exist and be writable, unless created by `etd bootstrap`. |
| `execution_role_arn` | yes | — | IAM role EMR Serverless jobs assume. See [docs/getting-started.md](getting-started.md#iam-permissions-actually-needed) for what it needs. |
| `prefix` | no | `etd` | Everything lands under `s3://<bucket>/<prefix>/<name>/`. |
| `credential_refresh_command` | no | *(none)* | Shell command to refresh credentials mid-run. Leave unset if your profile already refreshes itself (SSO, `credential_process`) — boto3 handles that automatically. Set this if your profile holds static keys that expire during a long run; the harness runs the command and retries when a call fails as expired. |
| `tags` | no | `{}` | Extra tags applied to every resource this run creates, merged with the harness's own `etd:run` / `etd:managed` tags. |

The four values marked required have no usable default — the config loader
specifically rejects the placeholder values shipped in the template (a
repeated-digit placeholder account id, and a `bucket` of `my-etd-bucket`) so
that copying the template without editing it fails fast with a clear message
rather than quietly trying to run against a placeholder account.

## `variants`

A list of environments to compare. Each variant is one EMR Serverless
application. **Access mode binds to the variant, not to the run** — see
[docs/lake-formation.md](lake-formation.md) for why FTA and FGAC each need their
own variant and cannot share an application.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `id` | yes | — | Short identifier, referenced by `comparisons`. |
| `label` | no | `id` | Human-readable name shown in the report. |
| `release_label` | yes | — | EMR release label, e.g. `emr-7.13.0`. |
| `architecture` | no | `X86_64` | `X86_64` or `ARM64`. |
| `access_mode` | no | `plain` | One of `plain`, `lf_fta`, `lf_fgac` — see below. |
| `baseline` | no | `false` on all but the first variant | Marks the variant that other variants are compared against by default. If no variant sets this, the first one declared becomes the baseline. |
| `shape` | no | see below | Driver/executor sizing for this variant's jobs. |
| `spark_conf` | no | `{}` | Extra Spark configuration keys, applied on top of everything the harness sets automatically for the variant's access mode and table format. Variant-level values win over the harness's own defaults. |
| `image_uri` | no | *(none)* | A custom EMR Serverless image URI, for testing a patch. Setting this gives the variant a `patch_hash`, so "same release label, with and without patch" becomes a clean, matched, one-dimension comparison. |
| `notes` | no | `""` | Free-text, carried into the manifest for your own reference. |

`shape` fields, all with a default of a small 2-core / 8 GB driver and two
2-core / 8 GB executors:

| Key | Default |
|---|---|
| `driver_cores` | `2` |
| `driver_memory` | `8g` |
| `executor_cores` | `2` |
| `executor_memory` | `8g` |
| `executor_count` | `2` |

YAML anchors (`&shape` / `*shape` in the template) are a convenient way to keep
two variants' shapes identical, which matters because of the one-dimension
rule described below.

### `access_mode` values

| Value | Meaning |
|---|---|
| `plain` | Glue (or Hive) catalog, execution role has direct S3 IAM. No Lake Formation involvement. |
| `lf_fta` | Lake Formation Full Table Access: whole-table credential vending. Job-level Spark configuration, not an application setting. |
| `lf_fgac` | Lake Formation Fine-Grained Access Control: table/row/column/cell/nested filtering, enforced through the EMR record server. Application-level switch. |

`lf_fta` and `lf_fgac` cannot coexist on one EMR Serverless application, so if
you want to compare them, declare two separate variants, each with its own
`access_mode`, and they will get two separate applications. See
[docs/lake-formation.md](lake-formation.md) for the mechanics and the
comparison implications (an FGAC variant cannot be shape-matched against a
`plain` one, because FGAC runs a second Spark driver per job).

## `comparisons`

Which variant pairs to compare, and how to interpret the result. This list
gates CI (`--fail-on`); it is not the only thing the report shows — every
ordered pair of variants is compared and made available in the report's
dropdown picker (see [docs/interpreting-the-report.md](interpreting-the-report.md)),
but only the pairs declared here appear as "declared" comparisons in the
CLI summary and are eligible for `--fail-on` gating.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `id` | yes | — | Comparison identifier. |
| `title` | no | `id` | Shown in the report and CLI output. |
| `baseline` | yes | — | Variant id to use as the baseline side. |
| `candidate` | yes | — | Variant id to use as the candidate side. |
| `intent` | yes | — | One of `upgrade_regression`, `patch_validation`, `governance_overhead`. |
| `primary` | no | `false` | Marks the headline comparison for a run with several. |
| `sizing_caveat` | no | *(none)* | Free text shown when the pair's shapes deliberately differ — for example an FGAC-vs-plain comparison. See `examples/configs/lake-formation.yaml` for the wording used there. |

If you declare no `comparisons` at all and have two or more variants, the
loader auto-generates one comparison per non-baseline variant against the
baseline, with `intent` set to `patch_validation` if that variant carries an
`image_uri`, otherwise `upgrade_regression`.

**Only `upgrade_regression` and `patch_validation` intents gate CI.** A
`governance_overhead` comparison never fails a build on functional or
performance grounds by design — see
[docs/interpreting-the-report.md](interpreting-the-report.md) for why turning on
access control is never scored as a regression.

### The one-dimension rule

If a comparison's two variants differ in more than one of
`deployment_model`, `release_label`, `architecture`, `access_mode`, `shape`, or
`patch`, the report labels that pair `UNMATCHED`: the functional verdict still
stands, but the performance verdict is suppressed, because a delta cannot be
attributed to any one cause. Keep every variant identical except the one thing
you're testing, or expect the pair to fall into `UNMATCHED_BY_DESIGN` /
`UNMATCHED` — see [docs/interpreting-the-report.md](interpreting-the-report.md)
for the full set of match statuses.

## `testbed`

The data the workloads run against.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `mode` | no | `generate` | `generate` creates a small star-schema dataset in your bucket. `existing` points at your own Glue tables instead. |
| `database` | no | `etd_<run.name>` | Glue database name. |
| `scale.fact_rows` | no | `2000000` in code (`40000000` in the template) | Row count for the fact table. Size this so your queries take at least roughly 10 seconds — sub-second queries are dominated by fixed job-startup overhead, and the harness will correctly refuse to call any delta at that scale a regression. |
| `scale.dim_rows` | no | `20000` in code (`200000` in the template) | Row count for the dimension table. |
| `tables` | only with `mode: existing` | — | List of existing table names to use, when not generating data. |

The test bed is built once, on the baseline variant, and every other variant
reads the same physical data through the same Glue database — so a functional
or performance difference between variants is never explained by different
input data.

## `workloads`

A list of things to run. Each entry has `kind: functional` or
`kind: performance`, and the fields that apply differ by kind.

### Functional workloads

| Key | Required | Default | Meaning |
|---|---|---|---|
| `id` | yes | — | Workload identifier. |
| `kind` | yes | — | `functional`. |
| `formats` | no | `[parquet]` | Table formats to exercise: any of `parquet`, `csv`, `orc`, `avro`, `iceberg`, `delta`. |
| `iterations` | no | `1` | Repetitions per operation (functional workloads default to 1; correctness and pass/fail status don't generally benefit from more). |
| `operations` | no | `DEFAULT` | `DEFAULT` runs the harness's built-in operation matrix; an explicit list restricts it. |

Every functional result is diffed against the *expected* support state for its
`(access_mode, table_format, operation, release_label)` combination, not
against a plain pass/fail — see [docs/interpreting-the-report.md](interpreting-the-report.md)
for what `EXPECTED_UNSUPPORTED` and the other functional verdicts mean.

### Performance workloads

| Key | Required | Default | Meaning |
|---|---|---|---|
| `id` | yes | — | Workload identifier. |
| `kind` | yes | — | `performance`. |
| `queries` | yes | — | Mapping of query name to SQL text. `{db}` is substituted with the test-bed database name. |
| `iterations` | no | `1` (template recommends `3`) | Repetitions of each query **inside one job run** (`per_job_iterations` in the template's comments — same field, `iterations` in the schema). |
| `job_repeats` | no | `1` (template recommends `3`) | Number of **separate job submissions**, each running the full query set once through `iterations` repetitions. |

`iterations` and `job_repeats` are not interchangeable, and the difference is
the single most important thing to get right when configuring a performance
workload — see [docs/methodology.md](methodology.md) for the full explanation
and a worked example of why conflating them produces a fake result.

Write your own queries here to replace the template's synthetic star-schema
ones — the whole point of the tool is comparing behaviour on a workload that
looks like yours.

## `thresholds`

Controls when a performance delta becomes a verdict.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `perf_noise_band_pct` | no | `5.0` | Deltas within this band are `NEUTRAL`, never a regression. |
| `perf_regression_alert_pct` | no | `10.0` | Regressions at or beyond this threshold are flagged `high` severity and can gate CI; below it they're still `REGRESSION` but `medium` severity. |
| `min_iterations_for_perf_verdict` | no | `2` | Minimum iterations required on both sides before a delta is scored at all; fewer produces `INSUFFICIENT_DATA`. |

The effective band used for any one query is never tighter than the observed
spread on either variant — see [docs/methodology.md](methodology.md) for how
that spread is computed and why it can widen the configured band but never
narrow it.

## `safety`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `auto_stop_minutes` | no | `15` | EMR Serverless application idle timeout. |
| `job_timeout_minutes` | no | `30` | Per-job execution timeout. |
| `max_parallel_variants` | no | `4` | Upper bound on how many variants run concurrently. |
| `confirm_before_provision` | no | `true` | Prompt with a cost estimate before `etd setup` creates anything. Pass `--yes` on the command line to skip the prompt without changing the config. |

## Required values, summarized

Only five values across the whole config have no usable default:
`run.name`, `run.region`, `run.account`, `run.bucket`, `run.execution_role_arn`,
plus at least one entry in `variants` (with a `release_label`) and at least one
entry in `workloads` (with a `queries` mapping if it's a performance workload).
`etd validate` checks all of these and prints a clear error if any are missing
or still hold a template placeholder value.
