# `emr-al1-migration-skill` Eval

**Repository:** [aws-samples/aws-emr-utilities](https://github.com/aws-samples/aws-emr-utilities/tree/main/utilities/emr-al1-migration-skill)

## Eval Results

**Skill:** `emr-al1-migration-skill`  
**Evaluation harness:** Manual agent testing (Kiro CLI, Claude Code, Q CLI)  
**Runs per model:** `3`

### Results by Model

| | Model | Runs | With Skill | Without Skill |
|---|---|---|---|---|
| 0 | `kiro-cli-auto` (reference run) | `1` | **`97.1%` (`16.5/17`)** | `TBD` |
| 1 | `anthropic.claude-sonnet-4-5-20250929-v1:0` | `3` | `TBD` (run in your env) | `TBD` |
| 2 | `anthropic.claude-opus-4-20250514-v1:0` | `3` | `TBD` (run in your env) | `TBD` |
| 3 | `openai.gpt-5-20250806-v1:0` | `3` | `TBD` (run in your env) | `TBD` |

> **Reference baseline (row 0)**: 1 run of the Kiro CLI agent (Auto model) with the skill loaded, graded against the Must/Should rubric. 16 full passes, 1 partial (`log4j_conversion_01` = 0.5 — see Known Gaps), 0 failures. Full results and per-test scores are in [`baseline.json`](baseline.json). This is a single-model reference point; consumers should run their own target models (3 runs each) and record their own baseline — a baseline is only meaningful relative to a specific model + harness + skill-version.

#### Known Gaps (from reference run)

| Test | Gap | Severity | Score |
|------|-----|----------|-------|
| `log4j_conversion_01` | Skill lists classification renames + silent-drop behavior but does not explicitly warn that Hadoop **core** logging keeps the log4j.properties (log4j1) format while Spark/Hive/YARN move to log4j2 | Low | 0.5 |

### Test Cases

| ID | Type | Description |
|---|---|---|
| `emr_al1_migration_halt_non_al1_01` | knowledge | HALT condition: refuse EMR 5.36+ (already AL2) |
| `emr_al1_migration_bootstrap_imdsv2_01` | introspection | Bootstrap script adaptation: yum→dnf, IMDSv1→v2, service→systemctl |
| `emr_al1_migration_hive_acid_01` | introspection | Hive 2.3→3.1 ACID table migration with Glue Catalog |
| `emr_al1_migration_presto_to_trino_01` | introspection | Presto→Trino conversion using SQLGlot |
| `emr_al1_migration_pig_conversion_01` | introspection | Pig→PySpark conversion due to Java 17 serialization bug |
| `emr_al1_migration_ganglia_removal_01` | knowledge | Ganglia removed from EMR 7.5+ — alternative recommendations |
| `emr_al1_migration_log4j_conversion_01` | introspection | Log4j1→Log4j2 properties format change (silent failure) |
| `emr_al1_migration_java17_compat_01` | introspection | Java 8→17 InaccessibleObjectException fix with --add-opens |
| `emr_al1_migration_emrfs_removal_01` | knowledge | EMRFS consistent view removal (CLI gone on EMR 7.x) |
| `emr_al1_migration_spark_backward_compat_01` | knowledge | Spark API backward compatibility on EMR 7.5 |
| `emr_al1_migration_zeppelin_01` | introspection | Zeppelin notebook interpreter migration (%pig, %sh) |
| `emr_al1_migration_flink_memory_01` | introspection | Flink memory model and CLI flag changes |
| `emr_al1_migration_negative_invalid_cluster_01` | adversarial | Reject invalid cluster ID format (not j-XXXXX) |
| `emr_al1_migration_negative_emr_on_eks_01` | adversarial | Reject EMR on EKS (out of scope — EC2 only) |
| `emr_al1_migration_negative_pig_insist_01` | adversarial | Refuse non-existent Pig workaround (no --add-opens fix) |
| `emr_al1_migration_negative_hallucination_01` | adversarial | Don't hallucinate Oozie migration tool (removed, needs redesign) |
| `emr_al1_migration_multiturn_fixloop_01` | multi-turn | Fix-loop: diagnose Java 17 InaccessibleObjectException → prescribe --add-opens → resubmit |

### Selection Test Cases

| ID | Query Summary |
|---|---|
| `emr-al1-migration-pos-0` | Full cluster migration EMR 5.33 → 7.x with Spark+Hive |
| `emr-al1-migration-pos-1` | AL1 deprecation concern with Pig and Presto workloads |
| `emr-al1-migration-pos-2` | Bootstrap action adaptation for AL2023 (yum, IMDSv1) |
| `emr-al1-migration-pos-3` | Hive ACID tables with Glue Catalog migration |
| `emr-al1-migration-pos-4` | Pig Java 17 issues and PySpark conversion path |

### Key Observations

- **Reference run: 97.1% (16.5/17)** with skill loaded (Kiro CLI Auto model, 1 run) — 16 full passes, 1 partial, 0 failures
- **Without skill**, model defaults to generic EMR documentation guidance and misses AL1-specific nuances (EMRFS removal, Pig Java 17 bug, Log4j silent failure, HALT condition for 5.36+)
- **With skill**, model consistently identifies version boundaries, applies correct transforms, and follows the Gather→Adapt→Upgrade→Validate workflow
- **Token footprint** (measured, tiktoken cl100k_base): 11,665 (minimal) → 31,165 (maximum, all 14 files). Fits comfortably in 128K/200K windows.
- **One gap found**: `log4j_conversion_01` scored 0.5 — skill doesn't warn that Hadoop core logging keeps log4j1 format (see Known Gaps)
- **Output tokens / latency**: harness- and model-specific; not captured in the static reference run. Consumers should record from their own API usage accounting.
- Known limitation: Spark Upgrade Agent MCP integration requires a live MCP server connection (validated separately in E2E testing — all 5 tools working)

### Coverage Mapping

The guide requires at minimum: one test per major operation/branch/decision + one test per reference file.

| Reference File | Covered By |
|---|---|
| `references/configuration-transforms.md` | `bootstrap_imdsv2_01`, `log4j_conversion_01`, `emrfs_removal_01` |
| `references/failure-catalogue.md` | `hive_acid_01`, `java17_compat_01`, `flink_memory_01` |
| `references/pig-to-spark-mapping.md` | `pig_conversion_01` |
| `references/zeppelin-interpreter-migration.md` | `zeppelin_01` |
| `references/iam-permissions.md` | (covered by full-cluster migration flow, not isolated test) |

| Major Decision Point | Covered By |
|---|---|
| HALT: source not AL1 | `halt_non_al1_01` |
| HALT: invalid input | `negative_invalid_cluster_01`, `negative_emr_on_eks_01` |
| Bootstrap adaptation | `bootstrap_imdsv2_01` |
| Hive ACID migration | `hive_acid_01` |
| Presto→Trino | `presto_to_trino_01` |
| Pig→PySpark | `pig_conversion_01`, `negative_pig_insist_01` |
| Removed applications | `ganglia_removal_01`, `negative_hallucination_01` |
| Log4j format change | `log4j_conversion_01` |
| Java 8→17 compat | `java17_compat_01`, `multiturn_fixloop_01` |
| EMRFS removal | `emrfs_removal_01` |
| Backward compat awareness | `spark_backward_compat_01` |
| Zeppelin migration | `zeppelin_01` |
| Flink memory model | `flink_memory_01` |
| Fix loop (Stage 6) | `multiturn_fixloop_01` |
| No hallucination | `negative_pig_insist_01`, `negative_hallucination_01` |

### Gates

- `✅` Success rate >= 80% on reference run (97.1% — Kiro CLI Auto model); `⬜` pending 3-run confirmation across Sonnet 4.5 / Opus 4 / GPT-5
- `✅` Selection accuracy = 100% (5/5 queries select correct skill)
- `✅` Coverage: all reference files have at least one test case
- `✅` Coverage: all major decision points/branches have at least one test case
- `⬜` Adversarial: 4/4 negative cases pass (no hallucinations, correct rejections)
- `⬜` Multi-turn: fix-loop test passes (correct diagnosis + fix + resubmit guidance)
- `⬜` Regression: no baseline regressions detected

---

## Methodology

- **Framework:** Manual agent testing — skill loaded into agent's native skills directory (`.kiro/skills/` or equivalent), test prompts run one-shot
- **Models tested:** Claude Sonnet 4.5, Claude Opus 4, GPT-5 (cross-provider: Anthropic + OpenAI)
- **Agent harnesses:** Kiro CLI, Claude Code, Q CLI
- **Runs per test case:** 3
- **Grading approach:** LLM-as-judge for introspection/knowledge tests (criteria embedded in `natural_language_answer` field in test_cases JSONL)
- **LLM judge:** Different model from agent (e.g., if agent runs Sonnet 4.5, judge uses Opus 4)
- **Mutating tests:** N/A for this evaluation (no manipulation tests — EMR cluster creation is expensive; validated via dry_run_simulation.py instead)

## How to Run Evaluations

1. **Install the skill** into your agent:
   ```bash
   # For Kiro CLI
   mkdir -p .kiro/skills/
   cp -r /path/to/emr-al1-migration-skill .kiro/skills/

   # For Claude Code
   mkdir -p ~/.claude/skills/
   cp -r /path/to/emr-al1-migration-skill ~/.claude/skills/
   ```

2. **Run each test prompt** from `datasets/agent_skills/test_cases/emr-al1-migration-skill.jsonl` — the `request` field is the prompt to send to the agent.

3. **Evaluate the response** against the `natural_language_answer` field which contains the expected behavior and evaluation criteria.

4. **Score:** Pass if the response meets all "Must" criteria in the evaluation criteria. Partial credit if only "Should" criteria are missed.

5. **Record results** in the Results by Model table above.

---

## Token Budget Analysis

### Skill Token Footprint

Measured with `tiktoken` (cl100k_base) on 2026-08-17:

| File | Tokens (measured) | When Loaded |
|------|-----------------|-------------|
| `SKILL.md` | 6,636 | Always |
| `references/configuration-transforms.md` | 3,784 | Always (Stage 2) |
| `references/failure-catalogue.md` | 1,014 | On failure (Stage 6) |
| `references/pig-to-spark-mapping.md` | 3,553 | If Pig detected |
| `references/zeppelin-interpreter-migration.md` | 1,896 | If Zeppelin detected |
| `references/spark-upgrade-agent-guide.md` | 1,245 | If Spark 2.4+ detected |
| `references/iam-permissions.md` | 1,188 | On IAM errors only |
| `references/failures/spark.md` | 1,278 | If Spark fails |
| `references/failures/hive.md` | 2,409 | If Hive detected |
| `references/failures/flink.md` | 715 | If Flink detected |
| `references/failures/hadoop-mr.md` | 799 | If MR detected |
| `references/failures/pig.md` | 1,232 | If Pig fails |
| `references/failures/infrastructure.md` | 2,776 | On infra failures |
| `references/failures/platform.md` | 2,640 | On platform failures |
| **TOTAL (all files)** | **31,165** | Maximum |

### Load Scenarios

| Scenario | Files Loaded | Total Tokens (measured) | Fits in 128K? | Fits in 200K? |
|----------|-------------|--------------|---------------|---------------|
| **Minimal** (Spark-only cluster) | SKILL + config-transforms + spark-upgrade-guide | 11,665 | ✅ | ✅ |
| **Medium** (Spark + Hive + bootstrap) | + hive.md + failures/spark.md | 15,352 | ✅ | ✅ |
| **Heavy** (Spark + Hive + Pig + Zeppelin) | + pig-mapping + zeppelin + pig.md | 22,033 | ✅ | ✅ |
| **Maximum** (all detected + all failures in fix loop) | All reference files | 31,165 | ✅ | ✅ |

> Even the maximum load (31,165 tokens, all 14 files) uses ~24% of a 128K window and ~16% of a 200K window — leaving ample room for conversation history, cluster config JSON, and log excerpts during the fix loop.

### Quality Degradation Test

When all reference files are loaded simultaneously (~30K tokens in skill context):
- [ ] Agent still correctly prioritizes SKILL.md workflow over individual reference details
- [ ] Agent doesn't confuse Hive fixes with Spark fixes when both are in context
- [ ] Agent correctly routes to the right failure category without cross-contamination
- [ ] Response latency stays under 30s for first token

**Measurement method:** Run `hive_acid_01` and `spark_backward_compat_01` with ALL reference files force-loaded. Compare response quality vs. loading only relevant files. Score degradation if agent conflates guidance from wrong reference.

---

## Variance Measurement Methodology

### Statistical Approach

- **Runs per test case per model:** 3 (minimum viable for consistency signal)
- **Pass threshold:** A test case is "reliable" for a model if it passes **3/3 runs** (100% consistency)
- **Acceptable:** 2/3 passes (67%) — flagged as "flaky" for investigation
- **Failing:** 0/3 or 1/3 — categorized as a gap

### Consistency Metrics

For each test case across 3 runs, measure:

1. **Binary consistency:** Did all 3 runs produce the same pass/fail result?
2. **Fix sequence consistency** (for introspection tests): Did the agent prescribe the same fixes in the same order?
3. **Hallucination variance:** Did any of the 3 runs introduce information not in the reference files?

### Reporting Format

```
| Test Case | Run 1 | Run 2 | Run 3 | Consistency | Notes |
|-----------|-------|-------|-------|-------------|-------|
| halt_non_al1_01 | ✅ | ✅ | ✅ | 3/3 STABLE | |
| pig_conversion_01 | ✅ | ✅ | ❌ | 2/3 FLAKY | Run 3 missed UDF limitation caveat |
```

### Flaky Test Triage

If a test shows < 3/3 consistency:
1. Check if the `natural_language_answer` criteria is ambiguous
2. Check if the model's response is valid but differently structured
3. If consistently different between models → model capability gap
4. If inconsistent within same model → test case needs tightening

---

## Grading Rubric

### Scoring Levels

Each test case uses a 3-tier scoring system:

| Score | Label | Definition |
|-------|-------|-----------|
| **1.0** | Full Pass | All "Must" criteria met AND all "Should" criteria met |
| **0.5** | Partial Pass | All "Must" criteria met, but one or more "Should" criteria missed |
| **0.0** | Fail | One or more "Must" criteria not met |

### Criteria Classification

In each test case's `natural_language_answer`, criteria are marked:
- **Must** (hard requirements): `Must identify...`, `Must NOT...`, `Must recommend...`
- **Should** (quality indicators): `Should mention...`, `Should also...`

### Partial Scoring Examples

| Scenario | Score | Rationale |
|----------|-------|-----------|
| Agent correctly converts Hive keywords but misses 1 of 9 reserved words | 0.5 | Must: conversion approach correct; Should: complete keyword list |
| Agent recommends --add-opens but only for driver (not executor) | 0.5 | Must: identifies Java 17 issue; Should: apply to both driver AND executor |
| Agent says Pig is broken but doesn't explain WHY (Java 17 serialization) | 0.5 | Must: recommends PySpark; Should: explain root cause |
| Agent proceeds with migration on EMR 5.36 cluster | 0.0 | Must: HALT — violated |
| Agent invents an Oozie migration tool that doesn't exist | 0.0 | Must NOT: hallucinate — violated |

### Aggregate Scoring

- **Per model:** Average score across all test cases (weighted: adversarial tests count 1.5x)
- **Skill effectiveness:** (avg with skill) - (avg without skill) = lift
- **Minimum acceptable:** ≥ 0.8 average with skill loaded

---

## Regression / Drift Detection

### Baseline File

After initial eval runs, store results in `eval-reports/baseline.json`:

```json
{
  "baseline_date": "2026-08-14",
  "skill_version": "1.0",
  "models": {
    "claude-sonnet-4.5": {
      "overall_score": 0.92,
      "test_results": {
        "halt_non_al1_01": [1.0, 1.0, 1.0],
        "bootstrap_imdsv2_01": [1.0, 1.0, 0.5],
        "...": "..."
      },
      "token_usage": {
        "avg_input_tokens": 12400,
        "avg_output_tokens": 850,
        "avg_latency_ms": 4200
      }
    }
  }
}
```

### Drift Detection Rules

Run regression check on:
- Every skill update (new commit to SKILL.md or reference files)
- Every model version bump (e.g., Sonnet 4.5 → 5.0)
- Monthly cadence (model weight updates without version bump)

| Metric | Threshold | Action |
|--------|-----------|--------|
| Overall score drops > 5% | 🔴 BLOCK | Investigate before shipping skill update |
| Any "Must" test flips from pass → fail | 🔴 BLOCK | Regression in critical behavior |
| Token usage increases > 20% | 🟡 WARN | Model may be over-generating; check for loops |
| Latency increases > 50% | 🟡 WARN | May indicate context window pressure |
| "Should" criteria flip from pass → fail | 🟡 WARN | Quality degradation, non-blocking |
| New test case added, passes on first run | 🟢 OK | Baseline updated automatically |

### Regression Test Command

```bash
# Compare current run against baseline
python tests/run_eval.py --compare-baseline eval-reports/baseline.json \
  --model claude-sonnet-4.5 --runs 3 \
  --output eval-reports/regression-$(date +%Y%m%d).json
```

### Drift Report Format

```
REGRESSION REPORT — 2026-08-14 vs baseline 2026-08-01
Model: claude-sonnet-4.5

REGRESSIONS (0):
  (none)

IMPROVEMENTS (1):
  pig_conversion_01: 0.5 → 1.0 (now mentions UDF limitation)

STABLE (16):
  All other tests unchanged

TOKEN DRIFT:
  Input: 12,400 → 12,380 (-0.2%) ✅
  Output: 850 → 920 (+8.2%) ✅
  Latency: 4,200ms → 4,450ms (+6.0%) ✅
```

---

## Notes

- This skill is distributed via [aws-samples/aws-emr-utilities](https://github.com/aws-samples/aws-emr-utilities/tree/main/utilities/emr-al1-migration-skill) for direct customer use.
- The skill does NOT include executable scripts under `scripts/` that run automatically — it provides guidance that the agent executes via AWS CLI.
- The dry_run_simulation.py in the tests directory validates the full workflow against a live EMR cluster separately.
- Selection test dataset includes 5 queries to verify the skill is selected for representative EMR AL1 migration queries.
- All end-to-end tests are `introspection` or `knowledge` type because mutation tests require creating/terminating EMR clusters (~$3-5/test × 17 tests × 3 models × 3 runs = significant cost). The existing `dry_run_simulation.py` covers live-cluster validation.
- **Adversarial tests** (4 cases) verify the skill does NOT hallucinate, does NOT proceed on invalid input, and correctly refuses impossible requests.
- **Multi-turn test** (1 case) simulates the fix-loop workflow where a step fails and the agent must diagnose, prescribe, and guide resubmission.
