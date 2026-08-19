#!/usr/bin/env python3
"""EMR Test Drive — on-cluster job.

Uploaded to S3 by `etd setup` and run on the EMR Serverless application under
test. One script, three modes:

    --mode setup       build the test bed (Glue database + fact/dim tables)
    --mode functional  run the operation matrix for one table format
    --mode perf        run the SQL workload, N iterations

Writes a single JSON result document to S3 and prints a summary to the driver
log. Never raises on an individual operation: every operation's outcome is data.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback

import boto3
from pyspark.sql import SparkSession

# Operations run in lifecycle order. Kept identical across formats so the
# report's format x operation heatmap is a clean grid.
DEFAULT_OPERATIONS = [
    "CREATE_TABLE", "INSERT_INTO", "SELECT", "DESCRIBE", "SHOW_CREATE_TABLE",
    "CTAS", "INSERT_OVERWRITE", "ALTER_TABLE_ADD_COLUMN",
    "UPDATE", "DELETE", "MERGE_INTO", "DF_WRITER_V2", "DROP_TABLE",
]

USING = {"parquet": "parquet", "iceberg": "iceberg", "delta": "delta"}

# Lake Formation data cell filters. These must mirror FILTER_SPECS in
# etd/lakeformation.py: that module creates the filters, this one asserts they
# were enforced, and a disagreement between the two would look like a bug in the
# service rather than in the harness.
FILTER_ROW_EXPR = "category = 'c1'"
FILTER_ALL_ROWS = "TRUE"
FILTER_COLUMN_COLS = ["fact_id", "dim_id", "category"]
FILTER_CELL_COLS = ["fact_id", "category"]




# ------------------------------------------------------------------- utilities

def s3_split(uri: str):
    b, _, k = uri.replace("s3://", "").partition("/")
    return b, k


def put_json(uri: str, payload: dict) -> None:
    bucket, key = s3_split(uri)
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key,
        Body=(json.dumps(payload, indent=2, default=str) + "\n").encode(),
        ContentType="application/json")
    print(f"[etd] wrote {uri}")


def s3_stats(uri: str) -> dict:
    """Object count and total bytes under a prefix — used for orphan detection."""
    bucket, key = s3_split(uri.rstrip("/") + "/")
    n, total = 0, 0
    try:
        for page in boto3.client("s3").get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=key):
            for o in page.get("Contents", []):
                n += 1
                total += o["Size"]
    except Exception as exc:  # noqa: BLE001
        print(f"[etd] s3_stats failed for {uri}: {exc}")
        return {}
    return {"object_count": n, "bytes": total}


def purge_prefix(uri: str) -> int:
    """Delete every object under a prefix. Returns the count removed.

    Needed for idempotency: DROP TABLE on a table created with an explicit
    LOCATION removes the catalog entry but leaves the data files. A later
    CREATE TABLE at the same location then fails — Delta raises
    DELTA_CREATE_TABLE_SCHEME_MISMATCH if the leftover schema differs — and
    every dependent operation cascades into failure.
    """
    bucket, key = s3_split(uri.rstrip("/") + "/")
    s3 = boto3.client("s3")
    removed = 0
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                removed += len(objs)
    except Exception as exc:  # noqa: BLE001
        print(f"[etd] purge_prefix failed for {uri}: {exc}")
    if removed:
        print(f"[etd] purged {removed} leftover object(s) under {uri}")
    return removed


def checksum(spark, table: str, cols: list[str]) -> str | None:
    """Order-independent content hash: sum of per-row xxhash64."""
    try:
        expr = "concat_ws('|', " + ", ".join(f"cast({c} as string)" for c in cols) + ")"
        row = spark.sql(f"SELECT cast(sum(xxhash64({expr})) as string) h FROM {table}").collect()
        return row[0]["h"] if row else None
    except Exception as exc:  # noqa: BLE001
        print(f"[etd] checksum failed for {table}: {exc}")
        return None


def row_count(spark, table: str) -> int | None:
    try:
        return spark.sql(f"SELECT count(*) c FROM {table}").collect()[0]["c"]
    except Exception:  # noqa: BLE001
        return None


def table_version(spark, table: str, fmt: str) -> int | None:
    """Commit count, so we can tell 'wrote data' from 'advanced the log'."""
    try:
        if fmt == "delta":
            return spark.sql(f"DESCRIBE HISTORY {table}").count()
        if fmt == "iceberg":
            return spark.sql(f"SELECT count(*) c FROM {table}.history").collect()[0]["c"]
    except Exception as exc:  # noqa: BLE001
        print(f"[etd] table_version failed for {table}: {exc}")
    return None


# ---------------------------------------------------------------------- setup

def do_setup(spark, cfg: dict) -> dict:
    db, data_uri = cfg["database"], cfg["data_uri"]
    facts = int(cfg.get("fact_rows", 2_000_000))
    dims = int(cfg.get("dim_rows", 20_000))
    steps = []

    def step(name, fn):
        t0 = time.time()
        try:
            fn()
            steps.append({"name": name, "status": "SUCCESS", "duration_s": round(time.time() - t0, 2)})
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": name, "status": "FAILED", "duration_s": round(time.time() - t0, 2),
                          "error": f"{type(exc).__name__}: {exc}"})
            traceback.print_exc()

    step("create_database", lambda: spark.sql(
        f"CREATE DATABASE IF NOT EXISTS {db} LOCATION '{data_uri}/{db}/'"))

    def make_dim():
        spark.sql(f"DROP TABLE IF EXISTS {db}.dim")
        (spark.range(dims).selectExpr(
            "id as dim_id",
            "concat('r', cast(id % 5 as string)) as region",
            "concat('name-', cast(id as string)) as name",
            "cast(id % 100 as int) as tier")
            .write.mode("overwrite").format("parquet")
            .option("path", f"{data_uri}/{db}/dim/").saveAsTable(f"{db}.dim"))

    def make_fact():
        spark.sql(f"DROP TABLE IF EXISTS {db}.fact")
        (spark.range(facts).selectExpr(
            "id as fact_id",
            f"cast(id % {dims} as bigint) as dim_id",
            "concat('c', cast(id % 8 as string)) as category",
            "cast((id % 1000) / 7.0 as double) as amount",
            "date_add(date'2026-01-01', cast(id % 90 as int)) as event_date")
            .write.mode("overwrite").format("parquet")
            .option("path", f"{data_uri}/{db}/fact/").saveAsTable(f"{db}.fact"))

    step("create_dim", make_dim)
    step("create_fact", make_fact)

    counts = {}
    for t in ("fact", "dim"):
        counts[t] = row_count(spark, f"{db}.{t}")
    return {"mode": "setup", "database": db, "steps": steps, "row_counts": counts,
            "spark_version": spark.version}


# ------------------------------------------------------------------ functional

def do_functional(spark, cfg: dict) -> dict:
    db, fmt = cfg["database"], cfg["format"]
    using = USING[fmt]
    # Table names and locations are namespaced per variant. Variants run in
    # parallel against one shared Glue database, so without this they create,
    # mutate and drop the SAME table concurrently and clobber each other's
    # results — which looks exactly like a regression.
    suffix = cfg.get("table_suffix") or "x"
    tbl = f"{db}.etd_{fmt}_t_{suffix}"
    tbl2 = f"{db}.etd_{fmt}_ctas_{suffix}"
    loc = f"{cfg['data_uri']}/{db}/etd_{fmt}_t_{suffix}/"
    ops = cfg.get("operations") or DEFAULT_OPERATIONS
    cols = ["fact_id", "dim_id", "category", "amount"]
    units: list[dict] = []

    # Start from a clean slate so a re-run is idempotent. Dropping the catalog
    # entry is not enough; the data files must go too (see purge_prefix).
    for t in (tbl, tbl2):
        try:
            spark.sql(f"DROP TABLE IF EXISTS {t}")
        except Exception:  # noqa: BLE001
            pass
    leftover = purge_prefix(loc) + purge_prefix(
        f"{cfg['data_uri']}/{db}/etd_{fmt}_ctas_{suffix}/")

    filter_evidence: dict = {}

    def sql(q):
        return lambda: spark.sql(q)

    def run(op: str, fn, expect_rows=None):
        t0 = time.time()
        rec: dict = {"name": op, "table_format": fmt, "table_type": "managed",
                     "duration_s": None, "status": "SUCCESS", "error": None}
        ver_before = table_version(spark, tbl, fmt) if op.startswith(("INSERT", "UPDATE", "DELETE", "MERGE", "DF_WRITER")) else None
        # Orphan detection only makes sense for v1 file-source tables. Iceberg and
        # Delta retain previous data files by design (time travel / snapshot
        # isolation), so counting retained objects there is a false positive.
        check_orphans = op == "INSERT_OVERWRITE" and fmt == "parquet"
        s3_before = s3_stats(loc) if check_orphans else None
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "FAILED"
            rec["error"] = f"{type(exc).__name__}: {exc}"[:2000]
            print(f"[etd] {fmt}.{op} FAILED: {rec['error'][:300]}")
        rec["duration_s"] = round(time.time() - t0, 2)

        if op != "DROP_TABLE":
            rec["row_count"] = row_count(spark, tbl)
            rec["result_checksum"] = checksum(spark, tbl, cols)
        if ver_before is not None:
            ver_after = table_version(spark, tbl, fmt)
            rec["table_version_before"] = ver_before
            rec["table_version_after"] = ver_after
            if ver_after is not None:
                rec["table_version_advanced"] = ver_after > ver_before
        if s3_before is not None:
            after = s3_stats(loc)
            rec["s3_before"] = s3_before
            rec["s3_after"] = after
            # An overwrite that leaves more objects than it wrote is a candidate
            # orphan case; the compare layer decides severity.
            if s3_before.get("object_count") and after.get("object_count", 0) > s3_before["object_count"]:
                rec["orphaned_object_count"] = after["object_count"] - s3_before["object_count"]
                rec["orphaned_bytes"] = max(0, after.get("bytes", 0) - s3_before.get("bytes", 0))
        if expect_rows is not None and rec.get("row_count") is not None:
            rec["expected_row_count"] = expect_rows
        # Recorded even when the assertion raised: the numbers are what make a
        # non-enforcement finding actionable.
        ev = filter_evidence.get(op.split("_")[0].lower())
        if ev:
            rec.update(ev)
        units.append(rec)
        print(f"[etd] {fmt}.{op} {rec['status']} {rec['duration_s']}s rows={rec.get('row_count')}")

    # Deterministic row selection. `LIMIT n` without ORDER BY is NOT deterministic
    # in Spark, so two variants would sample different rows and every content
    # checksum would differ — reported as a divergence that does not exist.
    src = (f"(SELECT fact_id, dim_id, category, amount FROM {db}.fact "
           f"WHERE fact_id < 5000)")
    src_merge = (f"(SELECT fact_id, dim_id, category, amount FROM {db}.fact "
                 f"WHERE fact_id < 100)")

    def df_writer_v2():
        """Append through DataFrameWriterV2, matching the table's current schema.

        ALTER TABLE ADD COLUMNS runs earlier in the sequence, so a fixed
        four-column frame no longer matches the table and Spark rejects the write
        with INCOMPATIBLE_DATA_FOR_TABLE — a harness artefact, not an engine bug.
        """
        target_cols = [f.name for f in spark.table(tbl).schema.fields]
        base = spark.table(f"{db}.fact").where("fact_id < 100")
        sel = []
        for name in target_cols:
            if name in base.columns:
                sel.append(f"`{name}`")
            else:
                sel.append(f"cast(null as string) as `{name}`")
        base.selectExpr(*sel).writeTo(tbl).append()

    def filtered_read(kind: str, expect_expr: str, expect_cols):
        """Read the fact table and assert Lake Formation applied the filter.

        The assertion is the point, not the query. A granted data cell filter
        that returns unfiltered rows or unfiltered columns is a governance
        failure: the job succeeds, and the data is wrong in the direction of
        disclosure. Granting whole-table SELECT exercises the FGAC code path but
        proves nothing about enforcement, which is what this checks.

        Expectations are computed from the table itself rather than hardcoded, so
        they cannot drift from the test bed. Filters apply to the base fact
        table, which no workload operation mutates, so the expected shape is
        stable across variants.
        """
        def go():
            base = f"{db}.fact"
            visible = spark.table(base)
            cols = sorted(f.name for f in visible.schema.fields)
            rows = visible.count()

            if expect_expr == FILTER_ALL_ROWS:
                permitted = rows
                row_enforced = None
            else:
                permitted = spark.sql(
                    f"SELECT count(*) AS c FROM {base} WHERE {expect_expr}"
                ).collect()[0]["c"]
                row_enforced = rows == permitted

            want_cols = sorted(expect_cols) if expect_cols else None
            col_enforced = (cols == want_cols) if want_cols else None

            # Disclosure beyond what the filter permits is the finding worth
            # naming. Fewer rows than permitted is over-restriction: wrong, but
            # not a disclosure, so it is recorded without raising.
            over = bool(
                (expect_expr != FILTER_ALL_ROWS and rows > permitted)
                or (want_cols and not set(cols).issubset(set(want_cols)))
            )

            filter_evidence[kind] = {
                "filter_kind": kind,
                "rows_visible": rows,
                "rows_permitted": permitted,
                "columns_visible": cols,
                "columns_permitted": want_cols,
                "row_filter_enforced": row_enforced,
                "column_filter_enforced": col_enforced,
                "filter_over_disclosure": over,
            }

            if over:
                raise AssertionError(
                    f"data filter '{kind}' not enforced: {rows} rows visible, "
                    f"{permitted} permitted; columns visible {cols}, "
                    f"permitted {want_cols if want_cols else 'all'}"
                )
        return go

    handlers = {
        "CREATE_TABLE": lambda: run("CREATE_TABLE", sql(
            f"CREATE TABLE {tbl} (fact_id BIGINT, dim_id BIGINT, category STRING, amount DOUBLE) "
            f"USING {using} LOCATION '{loc}'")),
        "INSERT_INTO": lambda: run("INSERT_INTO", sql(
            f"INSERT INTO {tbl} SELECT * FROM {src} t"), expect_rows=5000),
        "SELECT": lambda: run("SELECT", sql(f"SELECT count(*) FROM {tbl}")),
        "DESCRIBE": lambda: run("DESCRIBE", sql(f"DESCRIBE TABLE {tbl}")),
        "SHOW_CREATE_TABLE": lambda: run("SHOW_CREATE_TABLE", sql(f"SHOW CREATE TABLE {tbl}")),
        "CTAS": lambda: run("CTAS", sql(
            f"CREATE TABLE {tbl2} USING {using} AS SELECT * FROM {tbl}")),
        "INSERT_OVERWRITE": lambda: run("INSERT_OVERWRITE", sql(
            f"INSERT OVERWRITE TABLE {tbl} SELECT * FROM {src} t"), expect_rows=5000),
        "ALTER_TABLE_ADD_COLUMN": lambda: run("ALTER_TABLE_ADD_COLUMN", sql(
            f"ALTER TABLE {tbl} ADD COLUMNS (note STRING)")),
        "UPDATE": lambda: run("UPDATE", sql(
            f"UPDATE {tbl} SET amount = amount + 1 WHERE category = 'c1'")),
        "DELETE": lambda: run("DELETE", sql(f"DELETE FROM {tbl} WHERE category = 'c7'")),
        "MERGE_INTO": lambda: run("MERGE_INTO", sql(
            f"MERGE INTO {tbl} t USING {src_merge} s ON t.fact_id = s.fact_id "
            f"WHEN MATCHED THEN UPDATE SET t.amount = s.amount "
            f"WHEN NOT MATCHED THEN INSERT (fact_id, dim_id, category, amount) "
            f"VALUES (s.fact_id, s.dim_id, s.category, s.amount)")),
        "DF_WRITER_V2": lambda: run("DF_WRITER_V2", df_writer_v2),
        "DROP_TABLE": lambda: run("DROP_TABLE", sql(f"DROP TABLE {tbl}")),
        # Lake Formation data cell filters. Only meaningful under FGAC: with plain
        # Glue or full table access there is no filter to enforce, so the
        # expected-support matrix marks these unsupported for those modes.
        "ROW_FILTER": lambda: run(
            "ROW_FILTER", filtered_read("row", FILTER_ROW_EXPR, None)),
        "COLUMN_FILTER": lambda: run(
            "COLUMN_FILTER", filtered_read("column", FILTER_ALL_ROWS, FILTER_COLUMN_COLS)),
        "CELL_FILTER": lambda: run(
            "CELL_FILTER", filtered_read("cell", FILTER_ROW_EXPR, FILTER_CELL_COLS)),
    }

    for op in ops:
        h = handlers.get(op)
        if not h:
            units.append({"name": op, "table_format": fmt, "status": "SKIPPED",
                          "error": "no handler in this harness version", "duration_s": 0.0})
            continue
        h()

    try:
        spark.sql(f"DROP TABLE IF EXISTS {tbl2}")
    except Exception:  # noqa: BLE001
        pass

    return {"mode": "functional", "database": db, "format": fmt, "units": units,
            "leftover_objects_purged": leftover, "table": tbl,
            "spark_version": spark.version}


# ------------------------------------------------------------------------ perf

def _execute(spark, sql: str, sink: str):
    """Force full execution of a query without materialising a large result.

    `noop` is the usual benchmark sink, but it is **not available under Lake
    Formation FGAC**: the record server's plan transformation rejects it with
      IllegalArgumentException: No transformation available for type
      '...datasources.noop.NoopTable$'
    so a run that includes an FGAC variant uses `count` for *every* variant. The
    sink must be identical across variants or the comparison is meaningless.
    """
    df = spark.sql(sql)
    if sink == "noop":
        df.write.format("noop").mode("overwrite").save()
    elif sink == "count":
        df.count()
    else:
        raise ValueError(f"unknown perf sink: {sink}")


def do_perf(spark, cfg: dict) -> dict:
    db = cfg["database"]
    iterations = int(cfg.get("iterations", 3))
    warmup = bool(cfg.get("warmup", True))
    sink = cfg.get("sink", "noop")
    queries: dict = cfg["queries"]
    results: dict[str, dict] = {name: {"name": name, "iterations": [], "status": "SUCCESS",
                                       "error": None, "row_count": None}
                                for name in queries}

    # One untimed pass first. Without it, iteration 1 carries executor ramp-up,
    # JIT and cold S3/catalog caches, which on short queries can be an order of
    # magnitude slower than steady state and swamps the real delta.
    if warmup:
        for name, q in queries.items():
            try:
                t0 = time.time()
                _execute(spark, q.replace("{db}", db), sink)
                results[name]["warmup_s"] = round(time.time() - t0, 3)
            except Exception as exc:  # noqa: BLE001
                print(f"[etd] warmup {name} failed (continuing): {exc}")
        print(f"[etd] warmup pass complete (sink={sink})")

    for i in range(iterations):
        for name, q in queries.items():
            sql = q.replace("{db}", db)
            t0 = time.time()
            try:
                _execute(spark, sql, sink)
                dt = round(time.time() - t0, 3)
                results[name]["iterations"].append(dt)
                print(f"[etd] iter {i+1} {name} {dt}s")
            except Exception as exc:  # noqa: BLE001
                results[name]["status"] = "FAILED"
                results[name]["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                print(f"[etd] iter {i+1} {name} FAILED: {results[name]['error'][:200]}")
                break

    for name in queries:
        if results[name]["status"] == "SUCCESS" and not results[name]["iterations"]:
            results[name]["status"] = "NO_DATA"
    return {"mode": "perf", "database": db, "iterations": iterations, "warmup": warmup,
            "sink": sink, "units": list(results.values()), "spark_version": spark.version}


# ------------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["setup", "functional", "perf"])
    ap.add_argument("--config", required=True, help="JSON blob")
    ap.add_argument("--output", required=True, help="s3:// URI for the result document")
    args = ap.parse_args()

    cfg = json.loads(args.config)
    spark = SparkSession.builder.appName(f"etd-{args.mode}").enableHiveSupport().getOrCreate()
    print(f"[etd] mode={args.mode} spark={spark.version} config={json.dumps(cfg)[:800]}")

    started = time.time()
    try:
        if args.mode == "setup":
            payload = do_setup(spark, cfg)
        elif args.mode == "functional":
            payload = do_functional(spark, cfg)
        else:
            payload = do_perf(spark, cfg)
        payload["job_status"] = "COMPLETED"
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        payload = {"mode": args.mode, "job_status": "ERROR",
                   "error": f"{type(exc).__name__}: {exc}", "units": []}
    payload["job_wall_clock_s"] = round(time.time() - started, 2)
    payload["variant_id"] = cfg.get("variant_id")
    payload["workload_id"] = cfg.get("workload_id")
    payload["release_label"] = cfg.get("release_label")
    put_json(args.output, payload)
    spark.stop()


if __name__ == "__main__":
    main()
