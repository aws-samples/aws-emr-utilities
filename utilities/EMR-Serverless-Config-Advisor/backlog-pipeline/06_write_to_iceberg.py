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


def write_to_iceberg(rec_path, extract_path, table_name, warehouse, spark=None, perf_rec_path=None):
    """Write recommendation records joined with extracted metrics into an Iceberg table.

    Args:
        rec_path: Path to cost recommendations JSON file
        perf_rec_path: Path to performance recommendations JSON file (optional)
        extract_path: Path to extracted metrics
        table_name: Iceberg table name
        warehouse: Iceberg warehouse path
        spark: SparkSession
    """
    print(f"Cost recommendations path: {rec_path}")
    if perf_rec_path:
        print(f"Perf recommendations path: {perf_rec_path}")

    # Create SparkSession if not provided
    should_stop_spark = False
    if spark is None:
        should_stop_spark = True
        print("Creating SparkSession with Iceberg support...")
        spark = (SparkSession.builder
                 .appName("WriteToIceberg")
                 .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
                 .config("spark.sql.catalog.glue_catalog", "org.apache.iceberg.spark.SparkCatalog")
                 .config("spark.sql.catalog.glue_catalog.warehouse", warehouse)
                 .config("spark.sql.catalog.glue_catalog.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
                 .config("spark.sql.catalog.glue_catalog.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
                 .getOrCreate())

    # Parse table name early for error messages
    # Handle both "database.table" and "catalog.database.table" formats
    parts = table_name.split(".")
    if len(parts) == 2:
        catalog = "iceberg"
        database, table = parts
    elif len(parts) == 3:
        catalog, database, table = parts
    else:
        raise ValueError(f"Invalid table name format: {table_name}. Expected 'database.table' or 'catalog.database.table'")
    full_table = f"{catalog}.{database}.{table}"

    # Load cost recommendations
    if rec_path.startswith("s3://"):
        import boto3
        p = rec_path.replace("s3://", "").split("/", 1)
        body = boto3.client("s3").get_object(Bucket=p[0], Key=p[1])["Body"].read()
        cost_recs = json.loads(body)
    else:
        with open(rec_path) as f:
            cost_recs = json.load(f)

    print(f"Loaded {len(cost_recs)} cost recommendations")

    # Load perf recommendations if provided
    perf_recs_dict = {}
    if perf_rec_path:
        if perf_rec_path.startswith("s3://"):
            import boto3
            p = perf_rec_path.replace("s3://", "").split("/", 1)
            body = boto3.client("s3").get_object(Bucket=p[0], Key=p[1])["Body"].read()
            perf_recs = json.loads(body)
        else:
            with open(perf_rec_path) as f:
                perf_recs = json.load(f)

        # Convert to dict keyed by application_id for easy lookup
        perf_recs_dict = {rec.get("application_id", rec.get("job_id")): rec for rec in perf_recs}
        print(f"Loaded {len(perf_recs_dict)} perf recommendations")

    # Rename for clarity
    recs = cost_recs

    # Track records statistics
    total_recs_loaded = len(recs)
    recs_without_extract = []  # List of app_ids without extracts

    # Load task_stage_summary extracts keyed by application_id
    extract_dir = extract_path.rstrip("/") + "/task_stage_summary/"
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
        for fpath in g.glob(os.path.join(extract_dir, "*.json")):
            with open(fpath) as f:
                d = json.load(f)
            extracts[d.get("application_id", "")] = d

    # Build rows
    now = datetime.utcnow().isoformat()
    rows = []
    for rec in recs:
        app_id = rec.get("application_id", rec.get("job_id", "unknown"))
        ext = extracts.get(app_id, {})

        # Track if extract is missing
        if not ext or not ext.get("application_id"):
            recs_without_extract.append(app_id)

        es = ext.get("executor_summary", {})
        sd = ext.get("shuffle_data_summary", {})
        io = ext.get("io_summary", {}).get("application_level", {})
        ai = ext.get("application_info", {})

        # Get perf recommendation for this app if available
        perf_rec = perf_recs_dict.get(app_id, {})

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
            cost_config=json.dumps(rec),  # Cost recommendations JSON
            perf_config=json.dumps(perf_rec) if perf_rec else None,  # Performance recommendations JSON (optional)
            created_at=now,
        ))

    if not rows:
        print(f"No recommendation rows to write for {full_table}")
        print(f"\n{'='*60}")
        print(f"ICEBERG WRITE SUMMARY")
        print(f"{'='*60}")
        print(f"Total recommendations loaded:    {total_recs_loaded}")
        print(f"Records without extracts:        {len(recs_without_extract)}")
        if recs_without_extract:
            print(f"  Apps without extracts:")
            for app_id in recs_without_extract:
                print(f"    - {app_id}")
        print(f"Records written to Iceberg:      0")
        print(f"{'='*60}")
        if should_stop_spark and spark:
            spark.stop()
        return 0

    df = spark.createDataFrame(rows)

    # Debug: Show schema of dataframe being written
    print(f"\n{'='*60}")
    print(f"DataFrame Schema (columns being written):")
    print(f"{'='*60}")
    df.printSchema()

    print(f"\nDataFrame sample (first 5 rows):")
    df.show(5, truncate=False)

    # Debug: Show target table schema
    print(f"\n{'='*60}")
    print(f"Target Iceberg Table Schema:")
    print(f"{'='*60}")
    try:
        table_schema_df = spark.sql(f"DESCRIBE TABLE {full_table}")
        table_schema_df.show(100, truncate=False)
    except Exception as e:
        print(f"Could not describe table: {e}")

    # Create table if not exists, then append
    #spark.sql(f"CREATE DATABASE IF NOT EXISTS {catalog}.{database}")
    try:
        df.writeTo(full_table).using("iceberg").append()
        print(f"✓ Successfully appended {len(rows)} rows to {full_table}")
    except Exception as e:
        print(f"\n✗ Error writing to Iceberg table {full_table}")
        print(f"  Error: {str(e)}")
        print(f"  Table may not exist or schema may not match")
        print(f"  To create table, uncomment the line below and re-run")
        print(f"  #df.writeTo(full_table).using('iceberg').create()")

    # Print comprehensive summary
    print(f"\n{'='*60}")
    print(f"ICEBERG WRITE SUMMARY")
    print(f"{'='*60}")
    print(f"Total recommendations loaded:    {total_recs_loaded}")
    print(f"Extract directory:               {extract_dir}")
    print(f"Total task_stage_summary extracts found: {len(extracts)}")
    print(f"Records without extracts:        {len(recs_without_extract)}")
    if recs_without_extract and len(recs_without_extract) <= 10:
        print(f"  Apps without extracts:")
        for app_id in recs_without_extract:
            print(f"    - {app_id}")
    elif recs_without_extract:
        print(f"  First 10 apps without extracts:")
        for app_id in recs_without_extract[:10]:
            print(f"    - {app_id}")
    print(f"Records written to Iceberg:      {len(rows)}")
    print(f"{'='*60}")

    print(f"✅ Wrote {len(rows)} rows to {full_table}")
    if should_stop_spark and spark:
        spark.stop()
    return len(rows)
