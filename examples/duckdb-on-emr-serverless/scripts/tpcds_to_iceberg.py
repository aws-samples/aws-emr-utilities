# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Convert the generated TPC-DS Parquet files to Apache Iceberg tables (Hadoop catalog on S3).

Runs as a Spark job. Tables land at <warehouse>/tpcds/<table>, which DuckDB reads with
iceberg_scan('<warehouse>/tpcds/<table>') and Spark reads as demo.tpcds.<table>.

Args: <s3_parquet_prefix> <s3_warehouse>
"""
import json
import sys
import time

from pyspark.sql import SparkSession

SRC = sys.argv[1].rstrip("/")
WAREHOUSE = sys.argv[2].rstrip("/")

spark = (
    SparkSession.builder.appName("tpcds-to-iceberg")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.demo", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.demo.type", "hadoop")
    .config("spark.sql.catalog.demo.warehouse", WAREHOUSE)
    .getOrCreate()
)

TABLES = ["call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer",
          "customer_address", "customer_demographics", "date_dim", "household_demographics",
          "income_band", "inventory", "item", "promotion", "reason", "ship_mode", "store",
          "store_returns", "store_sales", "time_dim", "warehouse", "web_page", "web_returns",
          "web_sales", "web_site"]

spark.sql("CREATE NAMESPACE IF NOT EXISTS demo.tpcds")
report = {}
for t in TABLES:
    t0 = time.perf_counter()
    df = spark.read.parquet(f"{SRC}/{t}")
    (df.writeTo(f"demo.tpcds.{t}").using("iceberg")
       .tableProperty("write.parquet.compression-codec", "zstd")
       .createOrReplace())
    rows = spark.table(f"demo.tpcds.{t}").count()
    report[t] = {"rows": rows, "s": round(time.perf_counter() - t0, 1)}
    print(f"ICEBERG {t}: rows={rows} {report[t]['s']}s", flush=True)
print("TPCDS_ICEBERG_JSON=" + json.dumps(report), flush=True)
spark.stop()
