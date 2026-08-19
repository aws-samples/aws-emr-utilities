# Design and extension points

This is for anyone modifying or extending EMR Test Drive rather than just
running it: the mental model the whole tool is built on, the provider contract,
the comparison engine's structure, how the support-matrix layer stays correct
over time, the on-disk layout a run produces, and what's involved in adding a
new deployment model.

## The mental model: variants × workloads

Everything in this tool is one idea: take two or more **variants** (fully
specified places to run a job) and one or more **workloads** (fully specified
things to run), run every workload on every variant, and diff the results.

A variant is defined by: `deployment_model`, `release_label`, `architecture`,
`shape` (driver/executor sizing), `access_mode` (`plain` / `lf_fta` /
`lf_fgac`), any Spark configuration overlay, and an optional patch (a custom
image). A workload is defined by: `kind` (`functional` or `performance`), the
operations or queries to run, the table formats or dataset to run them against,
and the iteration counts.

The comparison engine's central rule follows directly from this model: if two
variants being compared differ in more than one of those defining fields, the
delta between them can't be attributed to any single cause, so the pair is
marked `UNMATCHED` and the performance verdict is suppressed. See
[docs/interpreting-the-report.md](interpreting-the-report.md#match-statuses)
for the resulting verdicts, and [docs/configuration.md](configuration.md#the-one-dimension-rule)
for how to construct a config that avoids it.

## The provider interface

One provider per deployment model, behind a single contract. Today only EMR
Serverless is implemented (`etd/providers/emr_serverless.py`); adding EMR on EC2
or EMR on EKS means writing a sibling module against the same shape, not
modifying the orchestrator, the comparison engine, or the report.

The contract, as implemented by `EmrServerlessProvider`, is:

```python
class Provider:
    def provision(self, variant: Variant) -> str:
        """Create (or reuse) the environment for this variant. Returns an env id."""

    def wait_ready(self, env_id: str, timeout_s: int) -> None:
        """Block until the environment can accept job submissions."""

    def submit(self, variant: Variant, name: str, entry_point: str,
               args: list[str], extra_conf: dict | None) -> str:
        """Submit one job. Returns a job id."""

    def monitor(self, variant: Variant, job_id: str,
                poll_s: int, on_state) -> JobResult:
        """Poll until the job reaches a terminal state. Returns timing, billed
        resource facts, and state details."""

    def teardown(self, dry_run: bool = False) -> list[str]:
        """Delete every environment tagged for this run. Tag-scoped: never
        touches anything the harness did not create."""
```

Two properties of the existing implementation matter for any new provider:

- **`provision` is idempotent and reuse-aware.** `EmrServerlessProvider.provision`
  looks for an existing application with the run's naming convention before
  creating one, and if it finds one whose release label or architecture doesn't
  match the config, it raises rather than silently reusing a mismatched
  environment. A new provider should do the same — reuse when the existing
  resource genuinely matches, fail loudly when it doesn't.
- **`teardown` is tag-scoped, not name-scoped.** The EMR Serverless provider
  checks both the resource's name prefix *and* its `etd:managed` / `etd:run`
  tags before deleting anything, and skips (with a printed reason) any resource
  that matches the name pattern but not the tags. This is what makes it safe to
  point the tool at an account that already has unrelated resources with
  similar names.

`JobResult` (the return type of `monitor`) carries `state`, `state_details`,
`wall_clock_s`, and a `billed` dict of resource-consumption facts (vCPU-hours,
memory GB-hours, and — for EMR Serverless — a note on which field the billed
figures came from, since the service populates them a few seconds after a job
reaches its terminal state and the client falls back to
`totalResourceUtilization` if `billedResourceUtilization` hasn't landed yet).
A new provider's cost facts don't need to match this shape exactly, but the
comparison engine's cost diff (`etd/compare.py::compare_cost`) expects
`vcpu_hour` and `memory_gb_hour` at minimum.

## The comparison engine

`etd/compare.py` implements four independent diffs, deliberately kept separate
rather than merged into one score, because they answer different questions and
a customer reading the report needs to be able to reason about each on its own:

1. **Correctness** (`compare_correctness`) — row counts, result-set checksums,
   and post-condition facts (table version advancement, orphaned objects).
   Diffed first and weighted highest, because a faster wrong answer is not an
   improvement.
2. **Functional** (`compare_functional`) — per-operation status, diffed against
   the *expected* support state rather than plain pass/fail (see "the
   support-matrix layer" below), plus error clustering
   (`cluster_errors`/`normalise_error`) to collapse many failures into a few
   root causes.
3. **Performance** (`compare_perf`) — best-of-N per query, an effective noise
   band per query, and a geometric-mean aggregate. See
   [docs/methodology.md](methodology.md) for the reasoning behind every choice
   here; this module is the implementation of that reasoning.
4. **Cost** (`compare_cost`) — normalized dollars per run, per variant, from
   the provider's billed resource facts and a pinned pricing table.

Each diff function takes plain dataclass-free dicts loaded from the run's unit
files (see "run artifact layout" below) and returns rows plus aggregate
figures; none of them talk to AWS or hold any state beyond what's passed in.
`build_comparison` assembles one comparison's full result (match status plus
all four diffs plus the overall verdict); `build_matrix` generates every
*ordered pair* of variants in the run (not just the declared `comparisons:`
entries), which is what lets the report's three-dropdown picker resolve any
combination the reader selects — see
[docs/interpreting-the-report.md](interpreting-the-report.md#navigating-the-report-the-three-dropdown-picker).

Adding a fifth diff (for example, a resource-utilization-shape comparison)
means writing another function with the same signature convention and wiring
its output into `build_comparison`; it does not require changing the other
three.

## The support-matrix layer: expectations are metadata, not frozen measurements

`etd/support.py` and the JSON files under `etd/matrices/` encode, per access
mode, a lookup from `(table_format, operation)` to a documented support state
(`Supported`, `Supported with S3 IAM`, or `Not supported`), with release-label
version gates where AWS's documentation specifies one.

The important design point is **when this lookup happens**. A measurement — did
this operation succeed, what was its row count, what did it time — is recorded
once, at run time, and is immutable afterward: it's a fact about what happened
on a specific job run. An *expectation* — whether that operation is documented
as supported for this format, access mode, and release — is not a fact about
the run; it's a fact about the documentation, and the documentation is
externally maintained, which means the tool's transcription of it can be wrong
or incomplete and get corrected later.

Because of this, `expected_state` is **recomputed at report time**, not stored
once and frozen. `etd report` (and the equivalent code path inside `etd run`)
re-resolves every functional unit's expected state against the current
matrices before building the HTML and JSON output — see the docstring on
`_reresolve_expectations` in `etd/cli.py`. If a matrix entry is corrected in a
newer version of the tool, re-running `etd report` against an *old* run
directory re-judges the same, unchanged measurements under the corrected
expectations. No new jobs run, and no measurement is altered — only which
verdict a given measurement resolves to.

This is also why the matrices carry a `sources` list and a `retrieved` date:
they're transcriptions of an external, versioned source of truth, not derived
data, and a correction to them should be traceable back to what AWS document
changed. See [docs/lake-formation.md](lake-formation.md) for what the FTA and
FGAC matrices currently cover and the known gap in what they assert.

## Run artifact layout

Every run's on-disk output lives under `runs/<run.name>/<run_id>/`:

```
runs/<name>/<run_id>/
  setup.json              # applications created, asset uri, job log from setup
  run_manifest.json       # full resolved RunSpec, variant manifests, workload
                          # descriptions, thresholds, pricing table, matrix
                          # provenance, and the complete job log
  units/
    <variant_id>__<workload_id>.json   # one file per (variant, workload) pair
  out/
    report.html           # self-contained, no external assets
    report.json           # same content, machine-readable
```

`run_manifest.json` is what `etd/compare.py::load_run` reads to reconstruct the
`Run` object the comparison engine operates on; the `units/` files are the
per-unit results (`RESULT_UNIT`-shaped records: operation or query name,
status, duration, and format-specific facts like row counts and checksums for
functional units, or iteration lists and per-job breakdowns for performance
units). Both `etd run` and `etd report` operate on exactly this directory
structure, which is why `etd report --run-dir` can rebuild a report entirely
offline, without AWS credentials, from a run whose EMR Serverless applications
have already been torn down.

## Adding a provider for EMR on EC2 or EMR on EKS

Concretely, this means:

1. Write `etd/providers/emr_ec2.py` (or `emr_eks.py`) implementing the five
   methods above. `submit` and `monitor` are the most mechanical — EMR on EC2's
   `run_job_flow` / `add_job_flow_steps` and EMR on EKS's
   `start_job_run`/`describe_job_run` on `emr-containers` map fairly directly
   onto the same shape the EMR Serverless provider already uses.
   `provision` and `teardown` are the ones that need the most care, since they
   define what "the environment for this variant" even means for that
   deployment model (a cluster and its instance groups, for EC2; a virtual
   cluster and a managed node group, for EKS) and must implement the same
   tag-scoped, reuse-aware safety properties described above.
2. Extend `Variant.deployment_model` handling wherever the orchestrator
   currently assumes EMR Serverless — chiefly in `etd/live.py`'s job submission
   helpers, which build EMR-Serverless-specific Spark configuration for access
   modes and table formats. The access-mode and format configuration logic
   itself (`access_mode_conf`, `format_conf`) is largely deployment-model
   agnostic already; what differs by model is how that configuration reaches
   the job (EMR Serverless's `sparkSubmitParameters` versus EC2's cluster
   `Configurations` versus EKS's pod template overlays).
3. Nothing in `etd/compare.py` or `etd/report.py` needs to change. Both operate
   on the `unit_kind`-tagged result records and the variant manifest fields
   (`deployment_model`, `release_label`, `architecture`, `access_mode`,
   `shape_hash`, `patch_hash`) that already generalize across deployment
   models — that's the point of keeping the comparison engine's inputs to a
   flat, provider-agnostic schema.

The same pattern extends to other extension points beyond a new deployment
model: a new workload kind is a new runner that writes result-unit records in
the existing schema; a new access mode is a new entry in `access_mode_conf` /
`format_conf` plus a new matrix file under `etd/matrices/`; a new comparison
dimension is another function alongside `compare_correctness` /
`compare_functional` / `compare_perf` / `compare_cost`.
