# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compute Apache Iceberg table statistics (NDV sketches, via the Iceberg compute_table_stats procedure)
for all 24 TPC-DS tables, so Spark's cost-based optimizer has column statistics.

Spark's ANALYZE TABLE is not supported for Iceberg (v2) tables, so the Iceberg procedure is used.
Args: <s3_warehouse>
"""
import sys
import time

from pyspark.sql import SparkSession

WH = sys.argv[1].rstrip("/")
spark = (
    SparkSession.builder.appName("tpcds-iceberg-stats")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.demo", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.demo.type", "hadoop")
    .config("spark.sql.catalog.demo.warehouse", WH)
    .getOrCreate()
)
TABLES = ["call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer", "customer_address",
          "customer_demographics", "date_dim", "household_demographics", "income_band", "inventory", "item",
          "promotion", "reason", "ship_mode", "store", "store_returns", "store_sales", "time_dim", "warehouse",
          "web_page", "web_returns", "web_sales", "web_site"]
total = time.perf_counter()
for t in TABLES:
    t0 = time.perf_counter()
    spark.sql(f"CALL demo.system.compute_table_stats(table => 'tpcds.{t}')").collect()
    print(f"STATS {t}: {time.perf_counter() - t0:.1f}s", flush=True)
print(f"STATS TOTAL: {time.perf_counter() - total:.1f}s", flush=True)
spark.stop()
