# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Convert the pipe-delimited TPC-DS text files (AWS public dataset, copied into our bucket) to Apache Iceberg.

Same destination layout as tpcds_to_iceberg.py: <warehouse>/tpcds/<table>, Hadoop catalog, zstd Parquet.
The schema comes from DuckDB's own TPC-DS DDL, so SF1000 tables match SF10/SF100 exactly.
Reads in FAILFAST mode, so any row that does not fit the schema fails the job instead of being dropped.

Args: <s3_text_prefix> <s3_warehouse> <schema_json_s3_or_local>
"""
import json
import sys
import time

from pyspark.sql import SparkSession
from pyspark.sql.types import (DateType, DecimalType, IntegerType, LongType, StringType, StructField, StructType)

SRC = sys.argv[1].rstrip("/")
WAREHOUSE = sys.argv[2].rstrip("/")
SCHEMA_PATH = sys.argv[3]

spark = (
    SparkSession.builder.appName("tpcds-text-to-iceberg")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.demo", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.demo.type", "hadoop")
    .config("spark.sql.catalog.demo.warehouse", WAREHOUSE)
    .getOrCreate()
)
schema_json = json.loads("".join(r.value for r in spark.read.text(SCHEMA_PATH, wholetext=True).collect()))


def spark_type(t):
    t = t.upper()
    if t == "INTEGER":
        return IntegerType()
    if t == "BIGINT":
        return LongType()
    if t == "VARCHAR":
        return StringType()
    if t == "DATE":
        return DateType()
    if t.startswith("DECIMAL"):
        p, s = t[t.index("(") + 1:t.index(")")].split(",")
        return DecimalType(int(p), int(s))
    raise ValueError(t)


spark.sql("CREATE NAMESPACE IF NOT EXISTS demo.tpcds")
# smallest tables first, so a schema problem shows up in minutes rather than after the big tables
order = sorted(schema_json, key=lambda t: {"store_sales": 3, "catalog_sales": 2, "web_sales": 1}.get(t, 0))
report = {}
for t in order:
    t0 = time.perf_counter()
    schema = StructType([StructField(n, spark_type(ty), True) for n, ty in schema_json[t]])
    df = (spark.read.schema(schema).option("sep", "|").option("header", "false").option("nullValue", "")
          .option("dateFormat", "yyyy-MM-dd").option("mode", "FAILFAST").csv(f"{SRC}/{t}/"))
    (df.writeTo(f"demo.tpcds.{t}").using("iceberg")
       .tableProperty("write.parquet.compression-codec", "zstd").createOrReplace())
    rows = spark.table(f"demo.tpcds.{t}").count()
    report[t] = {"rows": rows, "s": round(time.perf_counter() - t0, 1)}
    print(f"ICEBERG {t}: rows={rows} {report[t]['s']}s", flush=True)
print("TPCDS_ICEBERG_JSON=" + json.dumps(report), flush=True)
spark.stop()
