#!/usr/bin/env python3
"""Write recommendations + metrics to an Iceberg table via Spark.

Usage:
  spark-submit write_to_iceberg.py \
    --rec-path /path/to/cost_recs.json \
    --extract-path s3://bucket/prefix/  (contains task_stage_summary/*.json) \
    --table glue_catalog.db.table \
    --warehouse s3://bucket/iceberg/
"""
import argparse, json, os, sys
from datetime import datetime
from pyspark.sql import SparkSession, Row
from pyspark.sql.types import *


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rec-path", required=True, help="Path to recommendation JSON file (list of recs)")
    parser.add_argument("--extract-path", required=True, help="Path containing task_stage_summary/ dir")
    parser.add_argument("--table", required=True, help="Iceberg table: catalog.database.table")
    parser.add_argument("--warehouse", default="s3://suthan-event-logs/iceberg/")
    args = parser.parse_args()

    catalog, database, table = args.table.split(".")
    full_table = f"{catalog}.{database}.{table}"

    spark = (SparkSession.builder
        .appName("write-to-iceberg")
        .config(f"spark.sql.catalog.{catalog}", "org.apache.iceberg.spark.SparkCatalog")
        .config(f"spark.sql.catalog.{catalog}.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
        .config(f"spark.sql.catalog.{catalog}.warehouse", args.warehouse)
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .getOrCreate()
    )

    # Load recommendations
    if args.rec_path.startswith("s3://"):
        import boto3
        p = args.rec_path.replace("s3://", "").split("/", 1)
        body = boto3.client("s3").get_object(Bucket=p[0], Key=p[1])["Body"].read()
        recs = json.loads(body)
    else:
        recs = json.load(open(args.rec_path))

    # Load task_stage_summary extracts keyed by application_id
    extract_dir = args.extract_path.rstrip("/") + "/task_stage_summary/"
    extracts = {}
    if extract_dir.startswith("s3://"):
        import boto3
        p = extract_dir.replace("s3://", "").split("/", 1)
        s3 = boto3.client("s3")
        resp = s3.list_objects_v2(Bucket=p[0], Prefix=p[1])
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".json"):
                body = s3.get_object(Bucket=p[0], Key=obj["Key"])["Body"].read()
                d = json.loads(body)
                extracts[d.get("application_id", "")] = d
    else:
        import glob as g
        for f in g.glob(os.path.join(extract_dir, "*.json")):
            d = json.load(open(f))
            extracts[d.get("application_id", "")] = d

    # Build rows
    now = datetime.utcnow().isoformat()
    rows = []
    for rec in recs:
        app_id = rec.get("application_id", rec.get("job_id", "unknown"))
        ext = extracts.get(app_id, {})
        m = rec.get("metrics", {})
        es = ext.get("executor_summary", {})
        sd = ext.get("shuffle_data_summary", {})
        io = ext.get("io_summary", {}).get("application_level", {})
        ai = ext.get("application_info", {})

        rows.append(Row(
            job_id=str(ai.get("job_id", rec.get("job_id", ""))),
            application_name=str(rec.get("application_name", "")),
            app_id=str(ai.get("app_id", app_id)),
            optimization_mode=str(rec.get("optimization_mode", "")),
            input_gb=float(io.get("total_input_gb", 0) or 0),
            shuffle_read_gb=float(io.get("total_shuffle_read_gb", 0) or 0),
            shuffle_write_gb=float(io.get("total_shuffle_write_gb", 0) or 0),
            peak_shuffle_write_per_stage=float(sd.get("max_stage_shuffle_write_gb", 0) or 0),
            peak_disk_spill_per_stage=float(sd.get("max_stage_disk_spill_gb", 0) or 0),
            duration_hours=float(ext.get("total_run_duration_hours", 0) or 0),
            duration_minutes=float(ext.get("total_run_duration_minutes", 0) or 0),
            avg_memory_utilization_percent=float(es.get("avg_memory_utilization_percent", 0) or 0),
            avg_cpu_utilization_percent=float(es.get("avg_cpu_utilization_percent", 0) or 0),
            max_memory_utilization_percent=float(es.get("max_memory_utilization_percent", 0) or 0),
            idle_core_percentage=float(es.get("idle_core_percentage", 0) or 0),
            total_memory_spilled_gb=float(ext.get("spill_summary", {}).get("total_memory_spilled_gb", 0) or 0),
            cost_factor=float(ext.get("total_cost_factor", 0) or 0),
            src_event_log_location=str(ext.get("src_event_log_location", app_id)),
            recommendation=json.dumps(rec),
            created_at=now,
        ))

    df = spark.createDataFrame(rows)

    # Create table if not exists, then append
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {catalog}.{database}")
    try:
        df.writeTo(full_table).using("iceberg").append()
    except Exception:
        df.writeTo(full_table).using("iceberg").create()

    print(f"✅ Wrote {len(rows)} rows to {full_table}")
    spark.stop()


if __name__ == "__main__":
    main()
