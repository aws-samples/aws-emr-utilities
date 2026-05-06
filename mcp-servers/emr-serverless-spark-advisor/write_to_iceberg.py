"""Write recommendations to an Iceberg table via Athena."""
import json
import time
import logging

log = logging.getLogger("dual-mode-recommender")


def _run_athena_query(athena, query, database, workgroup="V3"):
    """Execute an Athena query and wait for completion."""
    resp = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database, "Catalog": "AwsDataCatalog"},
        WorkGroup=workgroup,
    )
    qid = resp["QueryExecutionId"]

    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)
        state = status["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)

    if state != "SUCCEEDED":
        reason = status["QueryExecution"]["Status"].get("StateChangeReason", "")
        raise RuntimeError(f"Athena query {state}: {reason}\nSQL: {query[:200]}")
    return qid


def _escape(s):
    """Escape single quotes for SQL."""
    return str(s).replace("'", "''")


def write_to_iceberg(recommendations, table_path, region="us-east-1"):
    """Write recommendations to Iceberg table via Athena.

    Args:
        recommendations: list of recommendation dicts
        table_path: catalog.database.table (e.g. AwsDataCatalog.common.emr-serverless-config-advisor)
        region: AWS region
    """
    import boto3
    from format_to_job_config import format_to_job_config

    parts = table_path.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected catalog.database.table, got: {table_path}")
    _, database, table = parts

    athena = boto3.client("athena", region_name=region)

    # Create table if not exists
    s3_location = f"s3://suthan-event-logs/iceberg/{database}/{table}/"
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS `{database}`.`{table}` (
        job_id string,
        application_name string,
        optimization_mode string,
        recommendation string,
        created_at timestamp
    )
    LOCATION '{s3_location}'
    TBLPROPERTIES ('table_type'='ICEBERG')
    """
    log.info("Ensuring table %s.%s exists", database, table)
    _run_athena_query(athena, create_sql, database)

    # Build batch INSERT with UNION ALL
    selects = []
    for rec in recommendations:
        job_config = format_to_job_config(rec) if "spark_configs" in rec else rec
        job_id = _escape(rec.get("job_id", rec.get("application_id", "unknown")))
        app_name = _escape(rec.get("application_name", "unknown"))
        mode = _escape(rec.get("optimization_mode", "unknown"))
        rec_json = _escape(json.dumps(job_config))
        selects.append(
            f"SELECT '{job_id}', '{app_name}', '{mode}', '{rec_json}', current_timestamp"
        )

    batch_size = 20
    for i in range(0, len(selects), batch_size):
        batch = selects[i:i + batch_size]
        union_sql = " UNION ALL ".join(batch)
        insert_sql = f'INSERT INTO "{database}"."{table}" {union_sql}'
        _run_athena_query(athena, insert_sql, database)
        log.info("Inserted batch %d-%d of %d", i + 1, min(i + batch_size, len(selects)), len(selects))

    log.info("Wrote %d recommendations to %s.%s", len(recommendations), database, table)
