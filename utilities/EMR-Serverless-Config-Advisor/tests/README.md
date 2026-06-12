# Config Advisor Regression Test Suite

**Policy: no change to `emr_recommender.py` merges without passing this suite.**
Enforced by `.github/workflows/config-advisor-regression.yml` on every PR that
touches the recommender.

## What it does

`test_recommender_golden.py` runs the recommender against 19 committed fixture
apps (extracted event-log metrics — no raw logs, no customer data):

- `fixtures/b12/` — 12 B12 production jobs (proven EMR Serverless configs,
  $138.40 total, 13.6% cheaper / 30.7% faster than the EC2 source)
- `fixtures/ump/` — 7 UMP analytics jobs, including the shared-clickstream
  congestion-collapse cases that motivated PR #164 (EC2 source, the 300-exec
  proven-good run, the 82-exec failed run, the cancelled run, and the
  email-pipeline jobs that must NOT regress)

The output is projected onto the **recommendation contract** (worker
type/vcpu/memory, max/min executors, shuffle partitions, disk size+type, and
the high-risk config overrides: advisory, broadcast threshold, excluded rules,
compression codec) and diffed against `golden_baseline.json`. Any unexpected
difference fails with exit 1 and a line-by-line diff.

## Workflow

```bash
cd utilities/EMR-Serverless-Config-Advisor

# Before/after editing the recommender:
python3 tests/test_recommender_golden.py

# Output changed intentionally? Regenerate the baseline IN THE SAME PR
# so reviewers see the recommendation diff alongside the code diff:
python3 tests/test_recommender_golden.py --update-golden --reason "PR #NNN: <why>"
```

The `--reason` is appended to the `history` array inside
`golden_baseline.json` together with the full list of changed lines — this is
the allow-list audit trail: every intentional recommendation change is
recorded, attributable, and reviewed as part of the PR diff.

## What failure means

- **You changed recommendations you didn't intend to change** — fix the code; or
- **You changed them intentionally** — regenerate the baseline with a reason
  and let the reviewer judge the recommendation diff itself.

A green run means: for all 19 reference workloads, the recommender emits
byte-identical contracts to the reviewed baseline.

## Adding fixtures

Drop extractor output (`task_stage_summary/*.json` + `spark_config_extract/*.json`)
into a new directory under `fixtures/`, add the directory to `FIXTURES` in
`test_recommender_golden.py`, and regenerate the baseline. Good candidates:
any job where the recommender ever produced a harmful config — the fixture
pins the fix forever.
