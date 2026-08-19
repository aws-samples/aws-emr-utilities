# Lake Formation: FTA and FGAC

EMR Test Drive can compare workloads across three data-access configurations —
plain Glue Data Catalog access, Lake Formation Full Table Access (FTA), and Lake
Formation Fine-Grained Access Control (FGAC). This page explains how the tool
models the difference, what it currently proves for each, and where the current
coverage gap is.

For the exact behaviour of either feature, treat the AWS documentation as
authoritative — this page cites it by title rather than reproducing it, and
does not attempt to be a complete restatement.

## FTA and FGAC are two different runtime architectures, not two settings

It's tempting to think of "turn on Lake Formation" as a single toggle with a
couple of modes. It isn't. FTA and FGAC differ in where enforcement happens and
what enforcement is even possible:

| | Full Table Access (FTA) | Fine-Grained Access Control (FGAC) |
|---|---|---|
| What it enforces | Whole-table allow/deny | Table, row, column, cell, and nested-attribute filtering |
| How | Lake Formation vends short-lived S3 credentials to the job's runtime role | An EMR record server intercepts metadata and data requests and filters them |
| Enablement point | Job-level Spark configuration | Application-level switch |

**FGAC is an application-level switch. FTA is job-level Spark configuration.
The two cannot coexist on one EMR Serverless application (or, correspondingly,
one EMR cluster).** This is the single most important constraint to design
around, and it is documented explicitly by AWS's guidance on running EMR
Serverless jobs without fine-grained access control, which states that a job
cannot simultaneously run Full Table Access and Fine-Grained Access Control on
the same EMR cluster or application.

Because of this, **access mode binds to the variant, not to the run.** Each
access mode a run wants to compare needs its own EMR Serverless application (or
EC2 cluster). EMR Test Drive's variant model reflects this directly: setting
`access_mode: lf_fta` on one variant and `access_mode: lf_fgac` on another
produces two separate applications, never one application switching modes
per job. See [docs/configuration.md](configuration.md) for the config shape and
`examples/configs/lake-formation.yaml` for a worked example comparing all three
modes plus an upgrade under FGAC.

## FGAC runs two Spark drivers per job

Under FGAC, a job runs with **two Spark drivers**: a user-profile driver, which
builds query plans but cannot itself launch tasks or reach S3 or the Glue
catalog, and a system-profile driver, which does the privileged work — talking
to S3 and Glue, compiling and running the stages that read protected data or
apply filters. `maxExecutors` is shared between the two profiles.

This has a direct, unavoidable consequence for measurement: **an FGAC variant
can never be executor-matched against a plain (or FTA) variant.** Even if you
configure identical `driver_cores` / `executor_count` values in the YAML, the
FGAC variant is running twice the driver capacity underneath that
configuration. EMR Test Drive's variant manifest reflects this by recording
`drivers_per_job: 2` for any variant using `lf_fgac`, and the comparison engine
treats a plain-vs-FGAC pair as a shape mismatch unless the comparison declares
a `sizing_caveat` explaining the deliberate difference — see
[docs/interpreting-the-report.md](interpreting-the-report.md#match-statuses)
for how `UNMATCHED_BY_DESIGN` is reported for exactly this case, and why the
resulting performance number is presented as the cost of governance rather than
as a matched benchmark.

## FGAC forbids disabling Spark dynamic allocation

FGAC requires Spark's dynamic resource allocation to remain enabled — attempting
to pin a fixed executor count under FGAC (the way a plain or FTA variant
typically would, for a stable, reproducible shape) is rejected by the service.
EMR Test Drive's job submission logic reflects this: for any variant with
`access_mode: lf_fgac`, dynamic allocation is left enabled and only
`maxExecutors` / `initialExecutors` / `minExecutors` are configured, rather than
pinning a fixed count the way other access modes do. This is a second, separate
reason (beyond the two-driver architecture above) why an FGAC variant's actual
resource footprint during a run isn't fully under your control the way a
plain variant's is.

## A no-op write sink is rejected under FGAC

EMR Test Drive's performance workloads measure query execution cost by writing
results to a sink rather than materializing them, by default using Spark's
`noop` write format — this forces full execution without the cost of writing
real output. FGAC's record server rejects a `noop` sink. Consequently, **if any
variant in a run uses `access_mode: lf_fgac`, the whole run falls back to a
counting sink (`count()`) for every variant**, not only the FGAC one — mixing
sinks across variants in the same run would mean the timing difference reflects
the sink, not the variant, so the fallback is applied uniformly rather than per
variant. See [docs/methodology.md](methodology.md#the-same-performance-sink-for-every-variant-in-a-run)
for the full explanation.

## Per-format support differs and is release-gated

Not every table format supports the same operations under FTA or FGAC, and some
of that support is gated by EMR release. EMR Test Drive encodes AWS's documented
support state — three states, not a simple pass/fail — per `(access_mode,
table_format, operation, release_label)` combination, and diffs observed
results against that *expected* state rather than against plain pass/fail. This
is what lets the report say `EXPECTED_UNSUPPORTED` for an operation AWS
documents as unsupported, instead of reporting it as a regression. See
[docs/interpreting-the-report.md](interpreting-the-report.md#functional-verdicts)
for the full set of functional verdicts this produces.

The three documented support states are:

| State | Meaning |
|---|---|
| Supported | Uses Lake Formation vended credentials exclusively; there is no fallback to runtime-role credentials if the Lake Formation permissions granted are insufficient. |
| Supported with IAM permissions on the Amazon S3 location | Does *not* use Lake Formation credentials; the job's runtime role needs direct S3 IAM access to the table's location regardless of Lake Formation registration. |
| Not supported | Documented as unsupported for this combination. A failure here is expected behaviour, not a regression. |

Release boundaries matter too. AWS documentation describes several
release-gated changes to FGAC behaviour across EMR versions — for example, when
DML and DDL operations that modify table data began using Lake Formation vended
credentials rather than runtime-role credentials, and when certain operations
(such as `DELETE`, `UPDATE`, and `MERGE`) gained support under FGAC that did not
exist in earlier releases. A functional result that changes from failing to
passing purely because the candidate's release crosses one of these documented
gates is reported as `FIXED_BY_RELEASE`, distinct from an unexplained `FIXED`
— see [docs/interpreting-the-report.md](interpreting-the-report.md#functional-verdicts).

Consult the current AWS documentation for the authoritative, up-to-date support
matrix and version gates for your target releases; the relevant EMR Management
Guide and EMR Serverless User Guide pages covering Lake Formation fine-grained
access control, open-table-format support under fine-grained access control,
and running jobs without fine-grained access control (Full Table Access) are
the primary sources this tool's expected-support data is transcribed from.

## The current gap: whole-table grants only

**The FGAC path in this tool is exercised with whole-table grants.** Row,
column, cell, and nested-attribute data filters — the actual differentiating
capability of fine-grained access control, as distinct from FTA — are **not
yet asserted** by the functional or correctness workloads. A run today proves
that FGAC's plumbing works (application-level enablement, two-driver execution,
the release-gated operation support described above, the sizing and sink
constraints) and measures its performance overhead relative to plain and FTA
access. It does not yet prove that a row filter actually excludes the rows it's
supposed to, that a column filter actually hides the column it's supposed to,
or that a cell-level or nested-attribute filter behaves as configured.

This is called out because it is the difference between "FGAC is wired up
correctly" and "FGAC is enforcing the specific policy you configured" — and
right now this tool only demonstrates the former. If your use case depends on
verifying row-, column-, cell-, or nested-level filtering specifically, treat
that as unverified by this tool until data-filter assertions are added, and
verify it directly against your own Lake Formation data filters and grants in
the meantime.
