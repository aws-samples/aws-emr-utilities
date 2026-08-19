# Methodology: how performance is measured, and why

A performance comparison is easy to get wrong in a way that looks confident and
is false. This page explains the measurement design choices in EMR Test Drive
and, where useful, the specific failure each choice is defending against.

## Best-of-N per query

For each query, the harness records every timing it observes and reports the
**minimum** as the headline number, with the median and maximum retained
alongside it. Minimum, not average, because almost all of the variance in a
repeated query's timing is added latency — a slow network call, a cold cache, a
noisy neighbour, a JIT warm-up — and essentially none of it is the query running
*faster* than its true best case. The fastest observed run is therefore the
measurement least contaminated by transient overhead, and averaging in the slow
runs would only dilute that signal with noise you don't want to model.

This applies at two levels, and conflating them is the single most common way
a performance comparison here would go wrong.

## `job_repeats` vs `per_job_iterations` — two different sources of variance

A performance workload has two repetition knobs:

- **`iterations`** (called `per_job_iterations` in the config template's
  comments) — how many times each query runs **inside one job submission**,
  reusing the same driver and the same executors for every repetition.
- **`job_repeats`** — how many **separate job submissions** are made, each one
  running the entire query set through its own `iterations` repetitions on a
  fresh driver and fresh executors.

Iterations inside a single job run measure *query* variance only: JIT warm-up,
buffer cache state, adaptive query execution replanning. They are structurally
blind to anything that varies at the level of the job itself — which host
Kubernetes or the underlying fleet placed the driver and executors on, whether
those executors were already warm from a prior job, S3 throughput at that
moment, or any other environmental effect that resets between job submissions
but not between iterations of one job.

That blindness is not theoretical. Running this harness on EMR Serverless
across two AWS accounts, a single query at `job_repeats=1` appeared **3.4x
faster** on the candidate variant than on the baseline. Running the same query
at `job_repeats=2` — two independent job submissions per variant instead of
one — the same query came out at **roughly -4%**, i.e. a small regression, not
an enormous improvement. Run-to-run environmental variance in that measurement
was on the order of 10-20%. The 3.4x number was not wrong data; it was a real
timing, from a real job, that happened to land on an unusually favourable
combination of warm executors and idle infrastructure on one side and an
unfavourable one on the other, and a single job run had no way to tell you
that.

The practical conclusion, and the reason the config template defaults to
`job_repeats: 3`: **treat 2 job repeats as the floor**, since below that you
cannot see whole-job variance at all, and use 3 to 5 when you need to resolve a
delta smaller than the environmental noise you're observing. If a delta does
not reproduce across independent job runs, it is not a delta — it's the
environment.

## The noise band: three sources, kept separate

Every per-query comparison computes a **noise band** — the size of delta that
is not distinguishable from measurement noise, and is therefore reported
`NEUTRAL` (or `WITHIN_NOISE` if the band had to widen to accommodate observed
spread) rather than as a regression or improvement.

The band is the **maximum** of three numbers, not their average and not a
pooled calculation over all of them at once:

1. **The configured band** (`thresholds.perf_noise_band_pct`, default 5%) — a
   floor you set explicitly.
2. **Within-job spread** — the variance between `iterations` inside one job
   run, taken as the largest such spread seen across all of that unit's job
   repeats.
3. **Between-job spread** — the variance of the best time across the
   independent job repeats themselves.

These are computed and kept as **separate** measures on purpose, and only
combined by taking their maximum. Pooling every iteration from every job run
into one flat list and computing the spread of that pooled set would produce a
number that looks more precise than it is: within-job jitter and between-job
environmental variance have different causes and different implications, and
averaging them together systematically inflates the reported band (because a
few volatile individual iterations can dominate a pooled spread even when the
job-to-job story is stable) — which then hides a real, reproducible delta that
a correctly separated calculation would have surfaced. Taking the max of the
two keeps each source honest: if either one is wide, the band widens to match
it, but a wide within-job spread on an unrelated query never bleeds into the
between-job number for a different query, and vice versa.

A query whose own iterations vary by 40% cannot be used as evidence of a 20%
regression, and reporting it as one is exactly the kind of thing that makes a
benchmark report lose credibility the first time someone tries to reproduce it.

## Geometric mean for the aggregate

The aggregate performance delta across a query set is reported as the
**geometric mean of the per-query ratios** (candidate time / baseline time for
each query), not the arithmetic mean and not simply the ratio of summed times.
p50 and p95 of the same ratio distribution are reported alongside it.

The geometric mean is used because it is resistant to one dominant query
carrying the whole verdict. A query that takes ten minutes and one that takes
one second contribute equally to the geometric mean of ratios (each is one data
point on the same log scale), whereas an arithmetic mean of the ratios, or worse
a ratio of totals, lets whichever query happens to be the slowest absolute time
determine the reported delta for the entire workload — even if every other
query moved in the opposite direction. Total wall-clock time is still reported
separately, because "the whole workload got faster or slower in absolute terms"
is also a real and useful number; it's just not the number used to decide
whether an individual regression exists.

## Variants run in parallel; iterations run serially

Within one run, **variants execute concurrently** with each other (bounded by
`safety.max_parallel_variants`), while **iterations and job repeats within a
single variant execute one after another**. This is deliberate and the two
halves of the rule exist for different reasons:

- Running variants in parallel keeps wall clock down — testing three variants
  serially would triple the time for no benefit to the measurement.
- Running iterations and job repeats *serially within* a variant avoids
  contaminating the very variance the noise band is trying to measure: if two
  iterations of the same query ran concurrently against the same test-bed data,
  they would compete for shared S3 throughput and (where relevant) shared Lake
  Formation request quota, and the resulting timings would reflect that
  contention rather than the query's own behaviour on that variant.

## The same performance sink for every variant in a run

The Spark job that runs performance queries writes its result to a "sink" — by
default `.write.format("noop")`, which forces full query execution without the
cost of materializing output. Lake Formation's fine-grained access control
(FGAC) rejects a no-op sink outright, so if **any** variant in the run uses
`access_mode: lf_fgac`, the harness falls back to a counting sink (`count()`)
for **every** variant in that run, not just the FGAC one.

This matters because mixing sinks across variants — even ones that both
"finish successfully" — would invalidate the comparison: a `noop` write and a
`count()` aggregation are not the same amount of work, so a delta measured
between a variant using one sink and a variant using the other would be
measuring the sink, not the variant. Falling back for the whole run, rather
than per variant, is what keeps every comparison in that run apples-to-apples.

## Why deterministic test-data selection matters

Any query used for a repeatable performance comparison must select the same
logical rows every time it runs, on every variant, on every iteration. Two
common ways to violate this without noticing:

- `LIMIT` **without** `ORDER BY` — the set of rows a query returns under a
  `LIMIT` with no defined order is whatever the engine's physical scan and
  shuffle happen to produce, which can (and does) change between Spark
  versions, between file layouts, and sometimes between runs of the identical
  query on the identical data. A performance number and a correctness checksum
  both become meaningless if the two variants aren't even looking at the same
  rows.
- Filtering on a non-deterministic or environment-dependent value instead of a
  stable key — for example filtering on "now" or an auto-incrementing id
  assigned at write time rather than a fixed value in the data.

The template's example queries filter with `WHERE` predicates on stable columns
(a category value, a fixed row-count threshold, a specific key range) rather
than an unordered `LIMIT`, and any query you substitute should follow the same
rule: select rows by a deterministic condition on the data itself, so that the
same physical result set is guaranteed on the baseline, the candidate, every
iteration, and every job repeat. Without that guarantee, the correctness diff
(row counts, result-set checksums — see
[docs/interpreting-the-report.md](interpreting-the-report.md)) will report
divergence that has nothing to do with the variant you're testing, and the
performance diff will be comparing two different amounts of work under the
same query name.
