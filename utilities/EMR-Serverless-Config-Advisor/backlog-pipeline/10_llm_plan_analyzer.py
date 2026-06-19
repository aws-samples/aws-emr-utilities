#!/usr/bin/env python3
"""
LLM Plan Analyzer — uses Claude to diagnose anomalous query plans and suggest fixes.

When the recommender detects anomalies (single stage dominating >60% of wall-clock,
shuffle-to-input ratio >10x, WindowGroupLimit skew), this module invokes Claude
to analyze the physical plan and produce actionable config/hint recommendations.

Results are cached by plan hash to avoid redundant API calls.

Usage:
  python3 10_llm_plan_analyzer.py \
    --extract-path s3://bucket/output/task_stage_summary/ \
    --cache-path s3://bucket/config-advisor/plan-analysis-cache/ \
    --output-path s3://bucket/config-advisor/plan-analysis-results/

  # Or as a library:
  from llm_plan_analyzer import analyze_plan
  result = analyze_plan(physical_plan, stage_metrics, config)
"""
import argparse
import hashlib
import json
import sys
import time
import logging

logging.basicConfig(
    format="%(asctime)s UTC %(levelname)-5s [%(name)s]  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("llm-plan-analyzer")

try:
    import boto3
except ImportError:
    log.error("boto3 required")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# USER CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

REGION = "us-east-1"
MODEL_ID = "anthropic.claude-sonnet-4-6-v1"
MAX_PLAN_CHARS = 30000  # Truncate plans longer than this for API limits
MAX_TOKENS = 2048

# Anomaly detection thresholds
SINGLE_STAGE_DOMINANCE_PCT = 60  # Stage taking >60% of total duration
SHUFFLE_RATIO_THRESHOLD = 10    # Shuffle/input ratio > 10x is anomalous
SPILL_RATIO_THRESHOLD = 5       # Spill > 5x shuffle is anomalous
FETCH_WAIT_THRESHOLD = 50       # >50% fetch wait indicates serving issues

# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Spark performance engineer analyzing query plans for EMR Serverless workloads.

Given a Spark physical plan and stage-level metrics, identify:
1. Root cause of the performance anomaly
2. Whether the join strategy is optimal for the data sizes
3. Specific Spark configurations or hints that would fix the issue

Output JSON with this structure:
{
  "diagnosis": "one-sentence root cause",
  "join_analysis": [
    {"join_type": "SortMergeJoin|BroadcastHashJoin|ShuffledHashJoin",
     "tables": ["left", "right"],
     "optimal": true/false,
     "recommended_type": "...",
     "reason": "..."}
  ],
  "config_recommendations": [
    {"key": "spark.sql.xxx", "value": "...", "reason": "..."}
  ],
  "excluded_rules": ["rule1", "rule2"],
  "severity": "low|medium|high|critical",
  "confidence": 0.0-1.0
}

Be specific and actionable. Reference actual operator names from the plan."""


def _hash_plan(plan_text: str) -> str:
    """Deterministic hash for plan caching."""
    return hashlib.sha256(plan_text.encode()).hexdigest()[:16]


def _check_cache(s3_client, cache_prefix, plan_hash):
    """Check if analysis for this plan hash already exists in cache."""
    bucket, prefix = cache_prefix.replace("s3://", "").split("/", 1)
    key = f"{prefix.rstrip('/')}/{plan_hash}.json"
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        cached = json.loads(resp["Body"].read())
        log.info("Cache hit for plan %s", plan_hash)
        return cached
    except s3_client.exceptions.NoSuchKey:
        return None
    except Exception:
        return None


def _write_cache(s3_client, cache_prefix, plan_hash, result):
    """Write analysis result to cache."""
    bucket, prefix = cache_prefix.replace("s3://", "").split("/", 1)
    key = f"{prefix.rstrip('/')}/{plan_hash}.json"
    s3_client.put_object(Bucket=bucket, Key=key, Body=json.dumps(result, indent=2))


def _detect_anomalies(extract_data):
    """Detect which apps have anomalous patterns worth analyzing."""
    anomalies = []
    stages = extract_data.get("stage_summary", {}).get("stages", [])
    sql_execs = extract_data.get("sql_executions", [])
    duration_sec = extract_data.get("application_info", {}).get("total_run_duration_hours", 0) * 3600
    io = extract_data.get("io_summary", {}).get("application_level", {})

    if not stages or duration_sec <= 0:
        return anomalies

    total_input = io.get("total_input_gb", 0) or 0
    total_shuffle_write = io.get("total_shuffle_write_gb", 0) or 0
    total_spill = extract_data.get("spill_summary", {}).get("total_memory_spilled_gb", 0) or 0

    # Check 1: Single stage dominance
    max_stage = max(stages, key=lambda s: s.get("duration_sec", 0) or 0)
    max_stage_pct = (max_stage.get("duration_sec", 0) or 0) / duration_sec * 100
    if max_stage_pct > SINGLE_STAGE_DOMINANCE_PCT:
        anomalies.append({
            "type": "single-stage-dominance",
            "stage_id": max_stage.get("stage_id"),
            "pct_of_job": round(max_stage_pct, 1),
            "stage_duration_min": round((max_stage.get("duration_sec", 0) or 0) / 60, 1),
        })

    # Check 2: Excessive shuffle ratio
    if total_input > 0:
        shuffle_ratio = total_shuffle_write / total_input
        if shuffle_ratio > SHUFFLE_RATIO_THRESHOLD:
            anomalies.append({
                "type": "excessive-shuffle-ratio",
                "ratio": round(shuffle_ratio, 1),
                "shuffle_write_gb": round(total_shuffle_write, 1),
                "input_gb": round(total_input, 1),
            })

    # Check 3: Excessive spill
    if total_shuffle_write > 0:
        spill_ratio = total_spill / total_shuffle_write
        if spill_ratio > SPILL_RATIO_THRESHOLD:
            anomalies.append({
                "type": "excessive-spill",
                "spill_gb": round(total_spill, 1),
                "shuffle_write_gb": round(total_shuffle_write, 1),
                "ratio": round(spill_ratio, 1),
            })

    # Check 4: Fetch wait indicating serving issues
    fetch_wait = io.get("shuffle_fetch_wait_percent", 0) or 0
    if fetch_wait > FETCH_WAIT_THRESHOLD:
        anomalies.append({
            "type": "serving-bottleneck",
            "fetch_wait_pct": round(fetch_wait, 1),
        })

    return anomalies


def _build_analysis_prompt(extract_data, anomalies):
    """Build the prompt for Claude with plan + metrics context."""
    sql_execs = extract_data.get("sql_executions", [])
    stages = extract_data.get("stage_summary", {}).get("stages", [])
    io = extract_data.get("io_summary", {}).get("application_level", {})
    spark_config = extract_data.get("spark_config", {})

    # Get physical plan (use the longest one as primary)
    plans = [sq.get("physical_plan_description", "") for sq in sql_execs if sq.get("physical_plan_description")]
    primary_plan = max(plans, key=len) if plans else ""

    # Truncate if too long
    if len(primary_plan) > MAX_PLAN_CHARS:
        primary_plan = primary_plan[:MAX_PLAN_CHARS] + "\n... [TRUNCATED]"

    # Top-N heaviest stages
    sorted_stages = sorted(stages, key=lambda s: s.get("duration_sec", 0) or 0, reverse=True)[:10]
    stage_summary = []
    for s in sorted_stages:
        stage_summary.append({
            "stage_id": s.get("stage_id"),
            "duration_min": round((s.get("duration_sec", 0) or 0) / 60, 1),
            "num_tasks": s.get("num_tasks", 0),
            "input_gb": round(s.get("input_gb", 0) or 0, 1),
            "shuffle_read_gb": round(s.get("shuffle_read_gb", 0) or 0, 1),
            "shuffle_write_gb": round(s.get("shuffle_write_gb", 0) or 0, 1),
            "mem_spill_gb": round(s.get("mem_spill_gb", 0) or 0, 1),
            "disk_spill_gb": round(s.get("disk_spill_gb", 0) or 0, 1),
        })

    # Relevant config subset
    relevant_keys = [
        "spark.executor.cores", "spark.executor.memory",
        "spark.sql.shuffle.partitions", "spark.sql.adaptive.enabled",
        "spark.sql.autoBroadcastJoinThreshold",
        "spark.sql.adaptive.advisoryPartitionSizeInBytes",
    ]
    config_subset = {k: v for k, v in spark_config.items() if k in relevant_keys}

    prompt = f"""Analyze this Spark workload that has the following anomalies:
{json.dumps(anomalies, indent=2)}

## Application Metrics
- Input: {io.get('total_input_gb', 0):.1f} GB
- Shuffle Write: {io.get('total_shuffle_write_gb', 0):.1f} GB
- Shuffle Read: {io.get('total_shuffle_read_gb', 0):.1f} GB
- Duration: {extract_data.get('application_info', {}).get('total_run_duration_hours', 0):.2f} hours
- Memory Spill: {extract_data.get('spill_summary', {}).get('total_memory_spilled_gb', 0):.1f} GB

## Current Spark Config
{json.dumps(config_subset, indent=2)}

## Top 10 Heaviest Stages
{json.dumps(stage_summary, indent=2)}

## Physical Plan
```
{primary_plan}
```

Provide your analysis as JSON."""

    return prompt


def analyze_plan(s3_client, bedrock_client, extract_data, cache_prefix=None):
    """Analyze a single workload's plan. Returns analysis dict or None."""
    anomalies = _detect_anomalies(extract_data)
    if not anomalies:
        return None

    app_id = extract_data.get("application_id", "unknown")
    log.info("Anomalies detected for %s: %s", app_id,
             [a["type"] for a in anomalies])

    # Build plan hash for caching
    sql_execs = extract_data.get("sql_executions", [])
    plans = [sq.get("physical_plan_description", "") for sq in sql_execs if sq.get("physical_plan_description")]
    plan_text = max(plans, key=len) if plans else ""
    plan_hash = _hash_plan(plan_text + json.dumps(anomalies))

    # Check cache
    if cache_prefix:
        cached = _check_cache(s3_client, cache_prefix, plan_hash)
        if cached:
            return cached

    # Build prompt and call Claude
    prompt = _build_analysis_prompt(extract_data, anomalies)

    try:
        response = bedrock_client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        resp_body = json.loads(response["body"].read())
        content = resp_body.get("content", [{}])[0].get("text", "")

        # Parse JSON from response
        # Handle case where model wraps in markdown code block
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        analysis = json.loads(content)
    except json.JSONDecodeError:
        analysis = {
            "diagnosis": content[:500] if content else "Failed to parse response",
            "config_recommendations": [],
            "severity": "unknown",
            "confidence": 0.0,
            "raw_response": content[:1000],
        }
    except Exception as e:
        log.error("Bedrock API error for %s: %s", app_id, e)
        return None

    # Enrich with metadata
    analysis["application_id"] = app_id
    analysis["plan_hash"] = plan_hash
    analysis["anomalies_detected"] = anomalies
    analysis["analyzed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Write to cache
    if cache_prefix:
        _write_cache(s3_client, cache_prefix, plan_hash, analysis)

    return analysis


def main():
    parser = argparse.ArgumentParser(description="LLM-based Spark query plan analysis")
    parser.add_argument("--extract-path", required=True,
                        help="S3 prefix with task_stage_summary/*.json extracts")
    parser.add_argument("--cache-path", required=True,
                        help="S3 prefix for plan analysis cache")
    parser.add_argument("--output-path", required=True,
                        help="S3 prefix for analysis results")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max number of apps to analyze per run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect anomalies but don't call LLM")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    # Load extracts
    bucket, prefix = args.extract_path.replace("s3://", "").split("/", 1)
    prefix = prefix.rstrip("/") + "/"

    extracts = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                extracts.append(json.loads(body))
                if len(extracts) >= args.limit:
                    break
        if len(extracts) >= args.limit:
            break

    log.info("Loaded %d extracts from %s", len(extracts), args.extract_path)

    # Analyze each
    results = []
    analyzed = 0
    cached = 0
    skipped = 0

    for extract in extracts:
        anomalies = _detect_anomalies(extract)
        if not anomalies:
            skipped += 1
            continue

        app_id = extract.get("application_id", "unknown")

        if args.dry_run:
            log.info("[DRY-RUN] %s: anomalies=%s", app_id, [a["type"] for a in anomalies])
            results.append({"application_id": app_id, "anomalies": anomalies, "dry_run": True})
            continue

        analysis = analyze_plan(s3, bedrock, extract, cache_prefix=args.cache_path)
        if analysis:
            results.append(analysis)
            if analysis.get("plan_hash") and _check_cache(s3, args.cache_path, analysis["plan_hash"]):
                cached += 1
            else:
                analyzed += 1

    # Write results
    if results:
        output_key = f"{args.output_path.rstrip('/')}/analysis-{int(time.time())}.json"
        out_bucket, out_key = output_key.replace("s3://", "").split("/", 1)
        s3.put_object(Bucket=out_bucket, Key=out_key, Body=json.dumps(results, indent=2))
        log.info("Results written to %s", output_key)

    log.info("Plan analysis complete: %d analyzed, %d cached, %d skipped (no anomalies)",
             analyzed, cached, skipped)

    # Print high-severity findings
    high_sev = [r for r in results if r.get("severity") in ("high", "critical")]
    if high_sev:
        log.warning("HIGH/CRITICAL findings:")
        for r in high_sev:
            log.warning("  %s: %s", r.get("application_id"), r.get("diagnosis", "")[:100])


if __name__ == "__main__":
    main()
