# Getting started

This walks through running EMR Test Drive against your own AWS account: what you
need before you start, what each command does, what it costs, and how to clean
up afterwards. If you just want to see a finished report without touching AWS,
see the "Try it with no AWS account" section in the top-level [README](../README.md)
instead — that path runs the comparison engine and report renderer over fixture
data and creates nothing.

## Prerequisites

- An AWS account you are willing to create EMR Serverless applications in.
- Python 3.9 or later.
- `boto3` and `PyYAML` installed (`pip install -r requirements.txt`), if you plan
  to run any command that talks to AWS. The CLI's `validate --no-aws` and the
  offline example do not need either.
- Credentials for the account, available to boto3 in the usual ways (a named
  profile, environment variables, an assumed role, SSO). EMR Test Drive does not
  manage credentials itself; every command re-checks that the caller's account
  matches `run.account` in your config and refuses to proceed otherwise.
- An S3 bucket in the target region, and an IAM role that EMR Serverless jobs
  will assume. `etd bootstrap` (below) can create both for you if you don't have
  them yet.

### IAM permissions actually needed

Two distinct sets of permissions are involved, and it helps to keep them separate:

**The identity running the `etd` CLI** (your own user or role) needs enough
permission to create and delete the resources the harness manages: EMR
Serverless applications, S3 objects under the run's prefix, a Glue database and
its tables, and — only if any variant uses `lf_fta` or `lf_fgac` — Lake Formation
grants and data-lake settings, plus the IAM permissions to create the Lake
Formation registration role. If you also use `etd bootstrap`, it additionally
needs `iam:CreateRole`, `iam:PutRolePolicy`, `s3:CreateBucket`, and their
delete-side equivalents for teardown. There's no single minimal policy shipped
for this identity because it's whatever you already use to administer the
account; an account administrator role is sufficient.

**The EMR Serverless job execution role** (`run.execution_role_arn` in your
config) is what actually runs the Spark jobs, and its permissions are narrow and
knowable. `etd bootstrap` creates one scoped to exactly this:

| Permission | Scope | Why |
|---|---|---|
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation` | the run's own bucket | read/write assets, test-bed data, and results |
| `glue:Get*` / `Create*` / `Update*` / `Delete*` on databases, tables, partitions | the account's Glue catalog | create the test-bed database and tables, and read/write them per operation |

If any variant uses `lf_fta` or `lf_fgac`, the harness adds one more inline
policy to this same role at setup time, granting `lakeformation:GetDataAccess`
and the temporary-credential actions Lake Formation needs to vend S3 access —
Lake Formation permissions alone are not sufficient; the runtime role also needs
this IAM action. See [docs/lake-formation.md](lake-formation.md) for what else
Lake Formation mode sets up (a separate registration role, data-lake
administrators, and grants).

If you write your own execution role instead of using `bootstrap`, give it the
S3 and Glue permissions above at minimum, scoped to your bucket and account.

## The lifecycle

Every command takes `--config <path>`, pointing at a YAML file shaped like
[config.template.yaml](../config.template.yaml). The commands are meant to be
run in this order, though several are optional or idempotent:

```
etd bootstrap   # optional: create the bucket and execution role
etd validate    # check the config and, unless --no-aws, the account
etd setup       # create applications, stage the job asset, build the test bed
etd run         # run every workload on every variant, then build the report
etd report      # rebuild the report from an existing run, without rerunning anything
etd teardown    # delete everything this run created
```

### `etd bootstrap`

Creates the S3 bucket and the EMR Serverless execution role for a brand-new
account, if you don't already have them. Both are tagged the same way every
other resource the harness creates is tagged, and both are removable with
`etd teardown --delete-iam`. It prints the bucket name and role ARN to paste
back into your config. Safe to run again — it reuses the bucket and role rather
than failing if they already exist.

### `etd validate`

Parses your config, reports what it resolved (variants, workloads, comparisons,
a rough job and cost estimate), and — unless you pass `--no-aws` — checks that
your current credentials are for the account named in `run.account`, that the
bucket is reachable, and that the execution role exists. Creates nothing. Run
this after every config edit; it catches typos in release labels, missing
required fields, and account mismatches before you spend anything.

### `etd setup`

Creates one EMR Serverless application per variant (or reuses a matching one
that already exists under the same name), stages the job asset to S3, and — once
every application is ready — runs a single job on the baseline variant to
generate the test-bed data and register it as a Glue database. If any variant
uses `lf_fta` or `lf_fgac`, this step also creates the Lake Formation
registration role, updates the account's data-lake settings, registers the data
location, and grants the execution role what it needs — see
[docs/lake-formation.md](lake-formation.md).

Before creating anything, it prints an estimate (job count and rough dollar
cost) and, unless you pass `--yes` or set `safety.confirm_before_provision:
false`, asks you to confirm.

### `etd run`

Runs every workload against every variant. Functional workloads run once per
declared table format; performance workloads run as `job_repeats` separate job
submissions, each executing `iterations` in-process repetitions per query — see
[docs/methodology.md](methodology.md) for why both numbers exist. Variants run
in parallel with each other (bounded by `safety.max_parallel_variants`);
iterations and job repeats within a variant run one after another. If `etd run`
is invoked without a prior `etd setup` having been run, it runs setup first.

When every job has finished, `etd run` writes the run's manifest and per-unit
result files under `runs/<name>/<run_id>/`, builds `report.html` and
`report.json`, and prints a one-line summary per declared comparison. Pass
`--open` to open the HTML report immediately, and `--fail-on
new_failure,correctness,regression,timeout` to get a non-zero exit code when any
of those categories appear on a gated comparison — useful in CI.

### `etd report`

Rebuilds `report.html` and `report.json` from an existing run directory without
resubmitting any job. Two things happen on every `report` (and at the end of
every `run`, since `run` calls the same code):

1. **Every ordered pair of variants is compared**, not just the pairs you
   declared under `comparisons:`. This is what powers the three-dropdown picker
   in the report — see [docs/interpreting-the-report.md](interpreting-the-report.md).
2. **Expected-support states are re-resolved from the current matrices.**
   Measurements (what actually happened on a given job run) are immutable once
   written; *expectations* (whether AWS documents an operation as supported for
   a given format, access mode, and release) are metadata, and are recomputed at
   report time rather than frozen at run time. If the bundled support matrices
   are corrected in a later version of the tool, re-running `etd report` against
   an old run directory re-judges the same measurements under the corrected
   expectations — no new jobs run.

Pass `--run-dir runs/<name>/<run_id>` to target a specific run other than the
most recent one, and the same `--open` / `--fail-on` flags as `etd run`.

### `etd status`

Lists the EMR Serverless applications that exist in the account for this run's
name, and whether a local run directory is present. Read-only.

### `etd teardown`

Deletes every resource this run created. It is **tag-scoped**: every resource
the harness provisions is tagged `etd:managed=true` plus `etd:run=<name>`, and
teardown only ever acts on resources carrying both tags. It lists what it's
about to delete and asks for confirmation unless you pass `--yes`.

By default, `etd teardown` deletes only the EMR Serverless applications. Two
flags extend it:

- `--delete-data` also deletes the run's S3 objects (assets, test-bed data,
  results, logs — everything under `s3://<bucket>/<prefix>/<name>/`) and the
  Glue database.
- `--delete-iam` also deletes the execution role and (if present) the Lake
  Formation registration role that `bootstrap` or `setup` created.

If any variant used `lf_fta` or `lf_fgac`, teardown also deregisters the Lake
Formation data location and removes the registration role's policies before
attempting to delete it.

Because teardown only touches resources it tagged itself, pointing it at an
account that already has other, unrelated EMR Serverless applications — even
ones with a similar name — will not affect them.

## Expected cost and runtime

The default configuration (a two-variant upgrade comparison, one functional
workload across a few table formats, one performance workload of a handful of
queries, 40 million fact rows) costs on the order of **$0.20 per run** and takes
somewhere in the range of several minutes to tens of minutes of wall clock,
dominated by EMR Serverless application startup and the number of separate job
submissions. Every job's actual billed vCPU-hours and GB-hours come from
`billedResourceUtilization` on the job run, which is what the cost comparison in
the report uses — this is not an estimate.

Cost scales roughly linearly with:

- the number of variants (each variant is its own EMR Serverless application
  and its own set of job submissions),
- the number of table formats in your functional workload,
- `job_repeats` on the performance workload (each repeat is a full separate job
  submission — see [docs/methodology.md](methodology.md) for why this matters
  for the numbers, not just the cost), and
- the scale of the generated test bed (`testbed.scale.fact_rows` /
  `dim_rows` in your config).

`etd setup` and `etd run` both print a job-count and dollar estimate before
asking you to confirm (unless you pass `--yes` or disable the confirmation
gate). Treat it as a rough planning number, not a quote — it is a simple
per-job multiplier, not a model of your actual query cost.

## Re-rendering a report offline from a finished run

Every run's manifest and per-unit results are plain JSON under
`runs/<name>/<run_id>/`. Nothing about rebuilding the report requires AWS
credentials or network access — `etd report` reads only that local directory.

```bash
./etd-cli.py --config my-upgrade.yaml report --run-dir runs/my-upgrade/<run_id> --open
```

This is the same code path that runs automatically at the end of `etd run`. Use
it when you've edited thresholds in your config, when a bundled support matrix
has been corrected, or when you just want to regenerate the HTML without
touching the account again. It writes `report.html` and `report.json` under
`runs/<name>/<run_id>/out/` and does not require the EMR Serverless applications
that produced the results to still exist — you can tear everything down and
still regenerate the report from the saved unit files as many times as you
like.
