# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Probe: what statistics can Spark 3.5 (EMR 7.14) use for Apache Iceberg tables with the cost-based optimizer?

Prints PROBE| lines: Iceberg version, whether ANALYZE TABLE and the Iceberg compute_table_stats procedure work,
and the statistics Spark reports in EXPLAIN COST before and after.
Args: <s3_warehouse>
"""
import sys

from pyspark.sql import SparkSession

WH = sys.argv[1].rstrip("/")
spark = (
    SparkSession.builder.appName("cbo-probe")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.demo", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.demo.type", "hadoop")
    .config("spark.sql.catalog.demo.warehouse", WH)
    .config("spark.sql.cbo.enabled", "true")
    .config("spark.sql.cbo.joinReorder.enabled", "true")
    .getOrCreate()
)
spark.sql("USE demo.tpcds")


def p(tag, text):
    for ln in str(text).splitlines():
        print(f"PROBE|{tag}|{ln}", flush=True)


def stats_of(sql):
    plan = spark.sql("EXPLAIN COST " + sql).collect()[0][0]
    return "\n".join(ln for ln in plan.splitlines() if "Statistics(" in ln or "ColumnStat" in ln or "== Optimized" in ln)


Q = "SELECT count(*) FROM date_dim WHERE d_year = 1998 AND d_moy = 11"
p("spark", spark.version)
try:
    p("iceberg_version", spark.sql("SELECT iceberg_version()").collect()[0][0])
except Exception as e:  # noqa: BLE001
    p("iceberg_version", f"unavailable: {str(e)[:120]}")
import glob
p("iceberg_jar", glob.glob("/usr/share/aws/iceberg/lib/*.jar"))
p("STATS_BEFORE", stats_of(Q))

try:
    spark.sql("ANALYZE TABLE date_dim COMPUTE STATISTICS FOR ALL COLUMNS")
    p("ANALYZE", "OK")
except Exception as e:  # noqa: BLE001
    p("ANALYZE", f"FAILED: {str(e).splitlines()[0][:300]}")

try:
    r = spark.sql("CALL demo.system.compute_table_stats(table => 'tpcds.date_dim')").collect()
    p("COMPUTE_TABLE_STATS", f"OK {r}")
except Exception as e:  # noqa: BLE001
    p("COMPUTE_TABLE_STATS", f"FAILED: {str(e).splitlines()[0][:300]}")

p("STATS_AFTER", stats_of(Q))
p("DONE", 1)
spark.stop()
