# Benchmark Agent

End-to-end benchmark orchestrator for the EMR Serverless Config Advisor. Given a workload name or event log, it runs the full cycle: extract → recommend → submit → poll → compare.

## Capabilities

1. **Extract** metrics from a Spark event log (zip on S3 or local)
2. **Recommend** configs using the Config Advisor recommender
3. **Submit** benchmark jobs to EMR Serverless with recommended configs
4. **Poll** job status until completion
5. **Compare** actual results against production baseline (duration, spill, plan)
6. **Report** pass/fail with detailed metrics comparison

## Usage

Invoke with `/agents benchmark` or ask: "run the benchmark for search-health"

### Parameters (pass in natural language)

- **workload**: Name from the registry (search-health, sup-trvlr-bml, vrbo-new-property, lodging-sort-be)
- **event-log**: S3 path or local path to the production event log zip (optional if workload has a registered default)
- **mode**: cost, performance, or both (default: both)
- **application-id**: EMR Serverless app ID (default: 00g6iqbn4ke2l109)
- **compare-with**: Additional configs to A/B test (e.g., "DRA tuned: backlogTimeout=15s, allocationRatio=0.5")

## Workload Registry

| Workload | Event Log | Synthetic Data | Query Script |
|----------|-----------|---------------|--------------|
| search-health | `s3://suthan-event-logs/config-advisor-test/eventLogs-search-health-impressions-success-00g62e96m053ng0b.zip` | `s3://suthan-event-logs/synthetic/search-health/data-v2/` | `s3://suthan-event-logs/synthetic/search-health/scripts/query.py` |
| sup-trvlr-bml | (in regression-suite) | `s3://suthan-event-logs/synthetic/regression-suite/sup_trvlr_bml/data-fullscale/` | `s3://suthan-event-logs/synthetic/regression-suite/sup_trvlr_bml/scripts/query.py` |
| vrbo-new-property | (in regression-suite) | `s3://suthan-event-logs/synthetic/regression-suite/vrbo_new_property/data-fullscale/` | `s3://suthan-event-logs/synthetic/regression-suite/vrbo_new_property/scripts/query.py` |

## Execution Flow

```
1. Extract event log → /tmp/{workload}_extract/task_stage_summary/*.json
2. Run recommender → cost + perf configs
3. Submit jobs (one per mode, plus any A/B variants)
4. Poll every 60s until all complete
5. For successful jobs: get dashboard URL, extract event log from run
6. Compare: duration ratio, spill, plan (SortMergeJoin vs ShuffledHashJoin), executor utilization
7. Report results table + pass/fail verdict
```

## Tools Used

- `Bash`: aws emr-serverless CLI, python3 for extraction/recommendation
- `Read`: Event logs, extracted JSONs, recommender output
- Working directory: `/Users/suthan/aws-emr-utilities/utilities/EMR-Serverless-Config-Advisor`

## Key Paths

- Recommender: `emr_recommender.py` (import `generate_dual_recommendations`)
- Extractor: `python_extractor.py` (import `extract_from_zip`, `parse_events`, etc.)
- EMR Serverless app: `00g6iqbn4ke2l109` (dedicated benchmark app, us-east-1)
- Execution role: `arn:aws:iam::633458367150:role/EMRServerlessJobExecutionRole`
- Results prefix: `s3://suthan-event-logs/config-advisor/benchmark-results/`

## Comparison Criteria

A benchmark **passes** if:
- Job completes successfully (no OOM, no timeout)
- Duration is within 1.5x of production baseline (for same-scale data)
- Memory spill < 10% of shuffle write (spill elimination target)
- Query plan matches production (same join strategies)
- No fetch-wait > 50% (no serving collapse)

## Security

- Never include customer workload names, account IDs, or S3 bucket names in git commits
- Anonymize all identifiers in reports that could be committed
