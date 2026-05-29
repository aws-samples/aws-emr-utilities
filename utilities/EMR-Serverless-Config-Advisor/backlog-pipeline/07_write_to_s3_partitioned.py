#!/usr/bin/env python3
"""Write recommendations + metrics to S3 with date-hour partitioning.

Usage:
  python write_to_s3_partitioned.py \
    --rec-path /path/to/cost_recs.json \
    --extract-path s3://bucket/prefix/  (contains task_stage_summary/*.json) \
    --s3-output-path s3://bucket/emr-serverless-config-advisor/

Writes data partitioned by datehour (format: yyyymmddHH as integer)
All jobs running in the same date+hour write to the same partition.
"""
import argparse, json, os, sys
import hashlib
from datetime import datetime
from decimal import Decimal
import boto3
from pyspark.sql import SparkSession
from pyspark.sql.functions import lit

# AWS Region
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Default S3 output path
DEFAULT_S3_OUTPUT_PATH = "s3://${S3_BUCKET}/emr-serverless-config-advisor/"


def generate_app_id_hash(application_id):
    """
    Generate hash from application_id using SHA256.
    This hash is used to join backlog and advisor tables.
    MUST match the hash generated in 01_discovery_job.py.

    Args:
        application_id: Spark application ID (e.g., application_1234567890123_0001)

    Returns:
        str: 32-character hash (first 32 chars of SHA256)
    """
    if not application_id:
        return ""
    return hashlib.sha256(application_id.encode()).hexdigest()[:32]


def safe_str(val):
    """Convert value to string, handling None and other types."""
    if val is None:
        return ""
    return str(val)


def safe_float(val):
    """Convert value to float, handling None and empty values."""
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def get_current_datehour():
    """
    Get current date-hour as integer in format yyyymmddHH.
    Example: 2026051914 for May 19, 2026 at 14:00 (2 PM)

    Returns:
        int: datehour as integer
    """
    now = datetime.utcnow()
    datehour_str = now.strftime("%Y%m%d%H")  # yyyymmddHH format
    return int(datehour_str)


def write_to_s3_partitioned(rec_path, extract_path, s3_output_path=None, perf_rec_path=None, spark=None, **kwargs):
    """Write recommendation records joined with extracted metrics to S3 with partitioning.

    IMPORTANT: This function writes to S3 with partitioning by datehour.
    Each job writes independently - all jobs in same hour write to same partition!

    Args:
        rec_path: Path to cost recommendations JSON file
        perf_rec_path: Path to performance recommendations JSON file (optional)
        extract_path: Path to extracted metrics
        s3_output_path: S3 output path (defaults to DEFAULT_S3_OUTPUT_PATH)
        spark: SparkSession (required for writing to S3)
        **kwargs: Additional args for compatibility (ignored)
    """
    # Use default S3 output path if not provided
    if not s3_output_path:
        s3_output_path = DEFAULT_S3_OUTPUT_PATH

    # Ensure path ends with /
    s3_output_path = s3_output_path.rstrip('/') + '/'

    print(f"Cost recommendations path: {rec_path}")
    if perf_rec_path:
        print(f"Perf recommendations path: {perf_rec_path}")

    print(f"\n{'='*80}")
    print(f"TARGET S3 LOCATION")
    print(f"{'='*80}")
    print(f"S3 Path: {s3_output_path}")
    print(f"Format: JSON")
    print(f"Partition By: datehour (yyyymmddHH)")
    print(f"{'='*80}")

    # Get current datehour for partitioning
    datehour = get_current_datehour()
    print(f"\nCurrent datehour: {datehour}")
    print(f"All jobs running in this hour will write to partition: datehour={datehour}")

    # Validate Spark session
    if not spark:
        raise ValueError("SparkSession is required for writing to S3")

    # Initialize S3 client for reading input files
    print(f"AWS Region: {AWS_REGION}")
    s3_client = boto3.client("s3", region_name=AWS_REGION)
    print(f"✓ Connected to S3")

    # Load cost recommendations
    if rec_path.startswith("s3://"):
        p = rec_path.replace("s3://", "").split("/", 1)
        body = s3_client.get_object(Bucket=p[0], Key=p[1])["Body"].read()
        cost_recs = json.loads(body)
    else:
        with open(rec_path) as f:
            cost_recs = json.load(f)

    print(f"Loaded {len(cost_recs)} cost recommendations")

    # Load perf recommendations if provided
    perf_recs_dict = {}
    if perf_rec_path:
        if perf_rec_path.startswith("s3://"):
            p = perf_rec_path.replace("s3://", "").split("/", 1)
            body = s3_client.get_object(Bucket=p[0], Key=p[1])["Body"].read()
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
        p = extract_dir.replace("s3://", "").split("/", 1)
        resp = s3_client.list_objects_v2(Bucket=p[0], Prefix=p[1])
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".json"):
                body = s3_client.get_object(Bucket=p[0], Key=obj["Key"])["Body"].read()
                d = json.loads(body)
                extracts[d.get("application_id", "")] = d
    else:
        import glob as g
        for fpath in g.glob(os.path.join(extract_dir, "*.json")):
            with open(fpath) as f:
                d = json.load(f)
            extracts[d.get("application_id", "")] = d

    # Build records list for DataFrame
    records = []
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

        # Generate app_id_hash directly (same algorithm as discovery job)
        app_id_hash = generate_app_id_hash(app_id)

        # Try multiple locations to find job_id (robust fallback strategy)
        job_id = (
            ai.get("job_id") or                    # 1. Check application_info section
            rec.get("job_id") or                   # 2. Check recommendation data
            ext.get("job_id") or                   # 3. Check task_stage_summary root
            ext.get("spark_config_extract", {}).get("job_id") or  # 4. Check spark_config_extract
            ""
        )
        job_id = safe_str(job_id)

        # CRITICAL VALIDATION: job_id cannot be empty or None
        if not job_id or job_id.strip() == "":
            error_msg = f"❌ VALIDATION ERROR: job_id is empty or None for app_id={app_id}"
            print(error_msg)
            print(f"   Recommendation data: {rec}")
            print(f"   Application info: {ai}")
            print(f"   Task stage summary (ext) keys: {list(ext.keys()) if ext else 'None'}")
            raise ValueError(error_msg)

        # Generate unique timestamp for each item (includes microseconds for uniqueness)
        now = datetime.utcnow().isoformat()

        # Build record dictionary
        record = {
            'Job_id': job_id,
            'created_at': safe_str(now),
            'application_name': safe_str(rec.get("application_name", "")),
            'app_id': safe_str(ai.get("app_id", app_id)),
            'app_id_hash': safe_str(app_id_hash),
            'optimization_mode': safe_str(rec.get("optimization_mode", "")),
            'input_gb': safe_float(io.get("total_input_gb", 0)),
            'shuffle_read_gb': safe_float(io.get("total_shuffle_read_gb", 0)),
            'shuffle_write_gb': safe_float(io.get("total_shuffle_write_gb", 0)),
            'peak_shuffle_write_per_stage': safe_float(sd.get("max_stage_shuffle_write_gb", 0)),
            'peak_disk_spill_per_stage': safe_float(sd.get("max_stage_disk_spill_gb", 0)),
            'duration_hours': safe_float(ext.get("total_run_duration_hours", 0)),
            'duration_minutes': safe_float(ext.get("total_run_duration_minutes", 0)),
            'avg_memory_utilization_percent': safe_float(es.get("avg_memory_utilization_percent", 0)),
            'avg_cpu_utilization_percent': safe_float(es.get("avg_cpu_utilization_percent", 0)),
            'max_memory_utilization_percent': safe_float(es.get("max_memory_utilization_percent", 0)),
            'idle_core_percentage': safe_float(es.get("idle_core_percentage", 0)),
            'total_memory_spilled_gb': safe_float(ext.get("spill_summary", {}).get("total_memory_spilled_gb", 0)),
            'cost_factor': safe_float(ext.get("total_cost_factor", 0)),
            'src_event_log_location': safe_str(ext.get("src_event_log_location", app_id)),
            'cost_config': safe_str(json.dumps(rec)),  # Store as JSON string
            'perf_config': safe_str(json.dumps(perf_rec) if perf_rec else ""),
            'datehour': datehour  # Add datehour column for partitioning
        }

        records.append(record)

    if not records:
        print(f"No recommendation rows to write to {s3_output_path}")
        print(f"\n{'='*60}")
        print(f"S3 WRITE SUMMARY")
        print(f"{'='*60}")
        print(f"Total recommendations loaded:    {total_recs_loaded}")
        print(f"Records without extracts:        {len(recs_without_extract)}")
        if recs_without_extract:
            print(f"  Apps without extracts:")
            for app_id in recs_without_extract:
                print(f"    - {app_id}")
        print(f"Records written to S3:           0")
        print(f"{'='*60}")
        return 0

    # Create DataFrame from records
    print(f"\n→ Creating DataFrame from {len(records)} records...")
    df = spark.createDataFrame(records)

    # Show schema
    print(f"\n{'='*80}")
    print(f"DataFrame Schema:")
    print(f"{'='*80}")
    df.printSchema()
    print(f"{'='*80}")

    # Show sample data
    print(f"\nSample data (first 2 rows):")
    df.show(2, truncate=80)

    # Write to S3 with partitioning by datehour
    # Use append mode so multiple jobs write to the same partition
    print(f"\n→ Writing {len(records)} records to S3 with partitioning...")
    print(f"   Output path: {s3_output_path}")
    print(f"   Partition: datehour={datehour}")
    print(f"   Mode: append (multiple jobs can write to same partition)")
    print(f"   Format: JSON")

    df.write \
        .mode("append") \
        .partitionBy("datehour") \
        .json(s3_output_path)

    print(f"✓ Successfully wrote {len(records)} records to S3")

    # Print comprehensive summary
    print(f"\n{'='*60}")
    print(f"S3 WRITE SUMMARY")
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
    print(f"Records successfully written:    {len(records)}")
    print(f"Target S3 path:                  {s3_output_path}")
    print(f"Partition:                       datehour={datehour}")
    print(f"Full path:                       {s3_output_path}datehour={datehour}/")
    print(f"{'='*60}")

    print(f"✅ Wrote {len(records)} records to {s3_output_path}datehour={datehour}/")
    return len(records)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Write advisor recommendations to S3 with partitioning')
    parser.add_argument('--rec-path', required=True, help='Path to cost recommendations JSON')
    parser.add_argument('--perf-rec-path', help='Path to performance recommendations JSON (optional)')
    parser.add_argument('--extract-path', required=True, help='Path to extracted metrics')
    parser.add_argument('--s3-output-path', default=DEFAULT_S3_OUTPUT_PATH, help='S3 output path')

    args = parser.parse_args()

    # Initialize Spark session for S3 writes
    spark = SparkSession.builder \
        .appName("EMR_Serverless_S3_Writer") \
        .getOrCreate()

    try:
        write_to_s3_partitioned(
            rec_path=args.rec_path,
            extract_path=args.extract_path,
            s3_output_path=args.s3_output_path,
            perf_rec_path=args.perf_rec_path,
            spark=spark
        )
    finally:
        spark.stop()
