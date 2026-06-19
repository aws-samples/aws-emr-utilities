#!/usr/bin/env python3
"""
Model Trainer — learns optimal parameters from feedback data.

Reads the feedback Iceberg table (predicted vs actual outcomes), trains models
to replace hand-tuned heuristic constants in the recommender:
  - base_efficiency (EC2→Serverless discount)
  - SAFE_SERVING_GBPS (shuffle serving ceiling)
  - PACKING_EFFICIENCY (slot utilization factor)
  - Worker bump thresholds

Also builds workload clusters for cold-start recommendations.

Outputs:
  - Model artifacts (JSON) to S3 for the recommender to load
  - Cluster centroids for nearest-neighbor matching

Usage:
  python3 09_model_trainer.py \
    --feedback-table glue_catalog.db.config_advisor_feedback \
    --recommendations-table glue_catalog.db.serverless_config_advisor_v2 \
    --model-output s3://bucket/config-advisor/models/ \
    --min-samples 20
"""
import argparse
import json
import math
import sys
import time
import logging
from collections import defaultdict

logging.basicConfig(
    format="%(asctime)s UTC %(levelname)-5s [%(name)s]  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("model-trainer")

try:
    import numpy as np
except ImportError:
    np = None
    log.warning("numpy not available — using fallback math")

# ──────────────────────────────────────────────────────────────────────────────
# MODEL CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

# Current hand-tuned defaults (baselines to beat)
CURRENT_DEFAULTS = {
    "base_efficiency_low": 0.47,
    "base_efficiency_high": 0.80,
    "safe_serving_gbps": 0.04,
    "packing_efficiency": 0.70,
    "worker_bump_small_to_medium": 70,
    "worker_bump_medium_to_large": 100,
    "wave_cap_multiplier": 2,
    "spill_memory_headroom": 0.7,
}

# Feature set for workload clustering
CLUSTER_FEATURES = [
    "input_gb",
    "shuffle_ratio",
    "spill_ratio",
    "num_stages",
    "max_stage_tasks",
    "duration_hours",
    "has_window_functions",
    "has_broadcast_joins",
    "num_joins",
]

# ──────────────────────────────────────────────────────────────────────────────


def _safe_div(a, b, default=0.0):
    if b == 0 or b is None:
        return default
    return a / b


def _calibrate_serving_rate(feedback_records):
    """Calibrate SAFE_SERVING_GBPS from feedback.

    For runs that succeeded without fetch-wait collapse (< 50%), compute the
    actual serving rate they achieved. The safe ceiling is the P75 of successful
    runs — above that, we've observed instability.
    """
    rates = []
    for r in feedback_records:
        if not r.get("actual_success"):
            continue
        if r.get("actual_fetch_wait_pct", 0) > 50:
            continue
        shuffle_write = r.get("actual_shuffle_write_gb", 0)
        duration_sec = r.get("actual_duration_hours", 0) * 3600
        max_exec = r.get("predicted_max_executors", 0)
        if shuffle_write > 100 and duration_sec > 0 and max_exec > 0:
            rate = shuffle_write / max_exec / duration_sec
            rates.append(rate)

    if len(rates) < 5:
        return CURRENT_DEFAULTS["safe_serving_gbps"], "insufficient-data"

    rates.sort()
    p75_idx = int(len(rates) * 0.75)
    calibrated = rates[p75_idx]
    return round(calibrated, 4), f"calibrated-from-{len(rates)}-runs"


def _calibrate_efficiency(feedback_records):
    """Calibrate base_efficiency range from EC2→Serverless migration feedback.

    The efficiency factor is: actual_serverless_work / predicted_ec2_work.
    We learn the range [low, high] that covers 90% of observed outcomes.
    """
    efficiencies = []
    for r in feedback_records:
        if not r.get("actual_success"):
            continue
        duration_ratio = r.get("delta_duration_ratio", 0)
        if 0.1 < duration_ratio < 5.0:
            # Inverse of duration ratio approximates efficiency gain
            efficiencies.append(1.0 / duration_ratio)

    if len(efficiencies) < 5:
        return (CURRENT_DEFAULTS["base_efficiency_low"],
                CURRENT_DEFAULTS["base_efficiency_high"],
                "insufficient-data")

    efficiencies.sort()
    p10_idx = int(len(efficiencies) * 0.10)
    p90_idx = int(len(efficiencies) * 0.90)
    return (round(efficiencies[p10_idx], 3),
            round(efficiencies[p90_idx], 3),
            f"calibrated-from-{len(efficiencies)}-runs")


def _calibrate_packing(feedback_records):
    """Calibrate PACKING_EFFICIENCY from actual executor utilization."""
    packings = []
    for r in feedback_records:
        if not r.get("actual_success"):
            continue
        utilization = r.get("delta_executor_utilization", 0)
        if 0.1 < utilization < 1.5:
            packings.append(utilization)

    if len(packings) < 5:
        return CURRENT_DEFAULTS["packing_efficiency"], "insufficient-data"

    packings.sort()
    # Target packing should be the median of actual utilization
    median_idx = len(packings) // 2
    return round(packings[median_idx], 3), f"calibrated-from-{len(packings)}-runs"


def _build_workload_clusters(recommendations, n_clusters=5):
    """Cluster workloads by feature vector for cold-start matching.

    Uses simple k-means-style clustering without sklearn dependency.
    Returns cluster centroids and labels.
    """
    if not recommendations or len(recommendations) < n_clusters:
        return None, "insufficient-data"

    # Extract feature vectors
    vectors = []
    labels = []
    for rec in recommendations:
        vec = [
            rec.get("input_gb", 0),
            _safe_div(rec.get("shuffle_write_gb", 0), max(rec.get("input_gb", 1), 1)),
            _safe_div(rec.get("total_memory_spilled_gb", 0), max(rec.get("input_gb", 1), 1)),
            rec.get("duration_hours", 0),
            rec.get("peak_shuffle_write_per_stage", 0),
        ]
        vectors.append(vec)
        labels.append(rec.get("application_name", ""))

    if np is None:
        # Fallback: just group by input size ranges (simple binning)
        clusters = []
        for v in vectors:
            if v[0] > 5000:
                clusters.append({"tier": "xlarge", "input_gb_range": ">5TB"})
            elif v[0] > 1000:
                clusters.append({"tier": "large", "input_gb_range": "1-5TB"})
            elif v[0] > 100:
                clusters.append({"tier": "medium", "input_gb_range": "100GB-1TB"})
            else:
                clusters.append({"tier": "small", "input_gb_range": "<100GB"})
        return clusters, "simple-binning"

    # Normalize features
    arr = np.array(vectors, dtype=float)
    means = arr.mean(axis=0)
    stds = arr.std(axis=0)
    stds[stds == 0] = 1.0
    normalized = (arr - means) / stds

    # Simple k-means (10 iterations)
    n = len(normalized)
    k = min(n_clusters, n)
    rng = np.random.default_rng(42)
    centroid_idx = rng.choice(n, size=k, replace=False)
    centroids = normalized[centroid_idx].copy()

    for _ in range(10):
        # Assign
        distances = np.linalg.norm(normalized[:, None] - centroids[None, :], axis=2)
        assignments = distances.argmin(axis=1)
        # Update
        for c in range(k):
            mask = assignments == c
            if mask.any():
                centroids[c] = normalized[mask].mean(axis=0)

    # Convert centroids back to original scale
    centroids_orig = centroids * stds + means
    cluster_configs = []
    for i, centroid in enumerate(centroids_orig):
        members = [labels[j] for j in range(n) if assignments[j] == i]
        cluster_configs.append({
            "cluster_id": i,
            "centroid": {
                "input_gb": round(float(centroid[0]), 1),
                "shuffle_ratio": round(float(centroid[1]), 3),
                "spill_ratio": round(float(centroid[2]), 3),
                "duration_hours": round(float(centroid[3]), 2),
                "peak_stage_shuffle_gb": round(float(centroid[4]), 1),
            },
            "member_count": len(members),
            "example_workloads": members[:3],
        })

    return cluster_configs, f"kmeans-{k}-clusters"


def main():
    parser = argparse.ArgumentParser(description="Train parameter models from feedback data")
    parser.add_argument("--feedback-table", default="glue_catalog.data_processing.config_advisor_feedback")
    parser.add_argument("--recommendations-table", default="glue_catalog.data_processing.serverless_config_advisor_v2")
    parser.add_argument("--model-output", required=True, help="S3 prefix for model artifacts")
    parser.add_argument("--min-samples", type=int, default=20, help="Minimum feedback records to train")
    parser.add_argument("--feedback-json", help="Local JSON file with feedback records (bypass Iceberg)")
    parser.add_argument("--recommendations-json", help="Local JSON file with recommendations (bypass Iceberg)")
    args = parser.parse_args()

    import boto3
    s3 = boto3.client("s3", region_name="us-east-1")

    # Load feedback data
    if args.feedback_json:
        with open(args.feedback_json) as f:
            feedback = json.load(f)
    else:
        # TODO: Query Iceberg table via Spark/Athena
        log.warning("Iceberg query not implemented yet; use --feedback-json")
        feedback = []

    # Load recommendations for clustering
    if args.recommendations_json:
        with open(args.recommendations_json) as f:
            recommendations = json.load(f)
    else:
        recommendations = []

    log.info("Loaded %d feedback records, %d recommendations", len(feedback), len(recommendations))

    if len(feedback) < args.min_samples:
        log.warning("Only %d feedback records (need %d). Writing baseline model with current defaults.",
                    len(feedback), args.min_samples)
        model = {
            "version": 1,
            "status": "baseline",
            "parameters": CURRENT_DEFAULTS,
            "calibration_source": "hand-tuned-defaults",
            "sample_count": len(feedback),
            "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    else:
        # Calibrate parameters
        serving_rate, serving_note = _calibrate_serving_rate(feedback)
        eff_low, eff_high, eff_note = _calibrate_efficiency(feedback)
        packing, packing_note = _calibrate_packing(feedback)

        model = {
            "version": 1,
            "status": "trained",
            "parameters": {
                "base_efficiency_low": eff_low,
                "base_efficiency_high": eff_high,
                "safe_serving_gbps": serving_rate,
                "packing_efficiency": packing,
                "worker_bump_small_to_medium": CURRENT_DEFAULTS["worker_bump_small_to_medium"],
                "worker_bump_medium_to_large": CURRENT_DEFAULTS["worker_bump_medium_to_large"],
                "wave_cap_multiplier": CURRENT_DEFAULTS["wave_cap_multiplier"],
                "spill_memory_headroom": CURRENT_DEFAULTS["spill_memory_headroom"],
            },
            "calibration_notes": {
                "serving_rate": serving_note,
                "efficiency": eff_note,
                "packing": packing_note,
            },
            "sample_count": len(feedback),
            "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        log.info("Calibrated parameters:")
        log.info("  base_efficiency: [%.3f, %.3f] (%s)", eff_low, eff_high, eff_note)
        log.info("  safe_serving_gbps: %.4f (%s)", serving_rate, serving_note)
        log.info("  packing_efficiency: %.3f (%s)", packing, packing_note)

    # Build workload clusters
    clusters, cluster_note = _build_workload_clusters(recommendations)
    if clusters:
        model["workload_clusters"] = clusters
        model["cluster_method"] = cluster_note
        log.info("Built %d workload clusters (%s)", len(clusters), cluster_note)

    # Write model artifact
    model_path = f"{args.model_output.rstrip('/')}/model-v{model['version']}-{int(time.time())}.json"
    # Also write as "latest" for easy lookup
    latest_path = f"{args.model_output.rstrip('/')}/model-latest.json"

    bucket, key = model_path.replace("s3://", "").split("/", 1)
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(model, indent=2))
    log.info("Model written to s3://%s/%s", bucket, key)

    _, latest_key = latest_path.replace("s3://", "").split("/", 1)
    s3.put_object(Bucket=bucket, Key=latest_key, Body=json.dumps(model, indent=2))
    log.info("Latest model pointer: s3://%s/%s", bucket, latest_key)

    # Summary
    log.info("Training complete:")
    log.info("  Status: %s", model["status"])
    log.info("  Samples: %d", model["sample_count"])
    log.info("  Clusters: %s", len(clusters) if clusters else "none")


if __name__ == "__main__":
    main()
