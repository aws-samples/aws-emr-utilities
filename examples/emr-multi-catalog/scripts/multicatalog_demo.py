"""
Multi-catalog demo for Amazon EMR 8.1 (release emr-8.1.0).

A single, generic PySpark script that creates small sample tables and
demonstrates the three multi-catalog capabilities of the redirecting session
catalog (RSC). Spark catalog configuration is supplied at spark-submit time by
the driver (run_demo.sh); this script only issues SQL and reports PASS/FAIL.

Phases (--phase):
  setup           Create one sample table per format (Iceberg, Delta, Hudi, Hive),
                  3 rows each, in --db under --warehouse. Run in the account where
                  your query engine lives (the "consumer").
  multiformat     Join all four formats in one query, unqualified names.
  producer-setup  Create ONE Hive sample table (--producer-table) with 3 rows,
                  in --db under --warehouse. Run this in the PRODUCER account
                  (point the driver at that account's app/role) so there is a
                  cross-account table to read.
  xacct-named     Read the producer table through a declared named catalog
                  (--named-catalog, default 'prod') and join to local Iceberg.
  xacct-autowire  Read the producer table via auto-wiring: reference it by its
                  backtick-quoted account id, no catalog declaration.
  all             setup + multiformat, plus the two xacct phases if
                  --producer-account is provided.

The cross-account phases assume the producer table exists and the cross-account
grants are in place (Lake Formation + Glue resource policy incl. database/default
+ S3 bucket policy + a decryptable catalog). See ../README.md.
"""
import argparse
import sys
import traceback

RESULTS = []


def step(name, fn, *args):
    try:
        fn(*args)
        RESULTS.append(("PASS", name, ""))
        print(f"\n===== PASS: {name} =====\n")
    except Exception as e:  # capture every failure as data
        msg = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else e}"
        RESULTS.append(("FAIL", name, msg))
        print(f"\n===== FAIL: {name} :: {msg} =====\n")
        traceback.print_exc()


# ---------- sample data ----------
ROWS = {"iceberg": "ice", "delta": "dl", "hudi": "hu", "hive": "hv"}


def _insert(spark, db, tbl, tag):
    spark.sql(f"INSERT INTO {db}.{tbl} (id, val) VALUES "
              f"(1,'{tag}-1'),(2,'{tag}-2'),(3,'{tag}-3')")


def setup(spark, a):
    def _db(spark):
        spark.sql(f"CREATE DATABASE IF NOT EXISTS {a.db} LOCATION '{a.warehouse}/{a.db}.db'")
        spark.sql(f"USE {a.db}")
    step("create-database", _db, spark)

    def _ice(spark):
        spark.sql(f"DROP TABLE IF EXISTS {a.db}.orders_iceberg")
        spark.sql(f"CREATE TABLE {a.db}.orders_iceberg (id INT, val STRING) USING iceberg "
                  f"LOCATION '{a.warehouse}/orders_iceberg'")
        _insert(spark, a.db, "orders_iceberg", "ice")
    step("create-iceberg", _ice, spark)

    def _delta(spark):
        spark.sql(f"DROP TABLE IF EXISTS {a.db}.returns_delta")
        spark.sql(f"CREATE TABLE {a.db}.returns_delta (id INT, val STRING) USING delta "
                  f"LOCATION '{a.warehouse}/returns_delta'")
        _insert(spark, a.db, "returns_delta", "dl")
    step("create-delta", _delta, spark)

    def _hudi(spark):
        spark.sql(f"DROP TABLE IF EXISTS {a.db}.shipments_hudi")
        spark.sql(f"CREATE TABLE {a.db}.shipments_hudi (id INT, val STRING) USING hudi "
                  f"TBLPROPERTIES (primaryKey = 'id') LOCATION '{a.warehouse}/shipments_hudi'")
        _insert(spark, a.db, "shipments_hudi", "hu")
    step("create-hudi", _hudi, spark)

    def _hive(spark):
        spark.sql(f"DROP TABLE IF EXISTS {a.db}.products_hive")
        spark.sql(f"CREATE TABLE {a.db}.products_hive (id INT, val STRING) USING parquet "
                  f"LOCATION '{a.warehouse}/products_hive'")
        _insert(spark, a.db, "products_hive", "hv")
    step("create-hive-parquet", _hive, spark)


def multiformat(spark, a):
    def _join(spark):
        spark.sql(f"USE {a.db}")
        df = spark.sql("""
            SELECT i.id, i.val AS iceberg, d.val AS delta, h.val AS hudi, p.val AS hive
            FROM   orders_iceberg  i
            JOIN   returns_delta   d ON i.id = d.id
            JOIN   shipments_hudi  h ON i.id = h.id
            JOIN   products_hive   p ON i.id = p.id
            ORDER BY i.id
        """)
        df.show(truncate=False)
        n = df.count()
        assert n == 3, f"expected 3 rows, got {n}"
    step("multiformat-four-way-join", _join, spark)


def producer_setup(spark, a):
    def _mk(spark):
        spark.sql(f"CREATE DATABASE IF NOT EXISTS {a.db} LOCATION '{a.warehouse}/{a.db}.db'")
        spark.sql(f"DROP TABLE IF EXISTS {a.db}.{a.producer_table}")
        spark.sql(f"CREATE TABLE {a.db}.{a.producer_table} (id INT, val STRING) USING parquet "
                  f"LOCATION '{a.warehouse}/{a.producer_table}'")
        _insert(spark, a.db, a.producer_table, "prod")
        spark.sql(f"SELECT * FROM {a.db}.{a.producer_table} ORDER BY id").show(truncate=False)
    step("producer-create-hive-table", _mk, spark)


def _xacct_join(spark, a, ref, label):
    n = spark.sql(f"SELECT count(*) AS n FROM {ref}").collect()[0]["n"]
    print(f"    -> {label} read OK, {ref} count={n}")
    spark.sql(f"""
        SELECT r.id, r.val AS remote_{a.producer_account}, i.val AS local_iceberg
        FROM   {ref}                    r
        JOIN   {a.db}.orders_iceberg    i ON r.id = i.id
        ORDER BY r.id
    """).show(truncate=False)
    assert n == 3, f"expected 3 rows, got {n}"


def named_local(spark, a):
    # Single-account demonstration of the NAMED-CATALOG mechanism (no 2nd account
    # needed). 'cat2' is a named redirecting catalog pointed at THIS account's own
    # Glue catalog id (set at submit time). It proves that a named catalog resolves
    # and routes just like spark_catalog. Requires the setup phase to have run.
    ref = f"cat2.{a.db}.orders_iceberg"

    def _r(spark):
        n = spark.sql(f"SELECT count(*) AS n FROM {ref}").collect()[0]["n"]
        print(f"    -> named-catalog (same account) read OK, {ref} count={n}")
        spark.sql(f"""
            SELECT c.id, c.val AS via_named_cat2, h.val AS via_spark_catalog
            FROM   {ref}                          c
            JOIN   spark_catalog.{a.db}.products_hive h ON c.id = h.id
            ORDER BY c.id
        """).show(truncate=False)
        assert n == 3, f"expected 3 rows, got {n}"
    step(f"named-local [{ref}]", _r, spark)


def cleanup(spark, a):
    def _c(spark):
        for t in ("orders_iceberg", "returns_delta", "shipments_hudi", "products_hive"):
            spark.sql(f"DROP TABLE IF EXISTS {a.db}.{t}")
        spark.sql(f"DROP DATABASE IF EXISTS {a.db} CASCADE")
        print(f"dropped demo tables and database '{a.db}'")
    step("cleanup-drop-tables", _c, spark)


def xacct_named(spark, a):
    ref = f"{a.named_catalog}.{a.producer_db}.{a.producer_table}"
    step(f"xacct-named [{ref}]", _xacct_join, spark, a, ref, "named-catalog")


def xacct_autowire(spark, a):
    # backtick-quote the producer account id (leading digit requires it)
    ref = f"`{a.producer_account}`.{a.producer_db}.{a.producer_table}"
    step(f"xacct-autowire [{ref}]", _xacct_join, spark, a, ref, "auto-wired")


def parse_args():
    p = argparse.ArgumentParser(description="EMR 8.1 multi-catalog demo")
    p.add_argument("--phase", required=True,
                   choices=["setup", "multiformat", "named-local", "producer-setup",
                            "xacct-named", "xacct-autowire", "cleanup", "all"])
    p.add_argument("--warehouse", default="s3://amzn-s3-demo-bucket/multicatalog/warehouse")
    p.add_argument("--db", default="salesdb")
    p.add_argument("--producer-account", default=None, help="12-digit producer account id")
    p.add_argument("--producer-db", default="salesdb")
    p.add_argument("--producer-table", default="fulfillment")
    p.add_argument("--named-catalog", default="prod")
    return p.parse_args()


def main():
    a = parse_args()
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.appName(f"multicatalog-demo-{a.phase}").getOrCreate()
    print("=== Spark version:", spark.version)
    print("=== phase:", a.phase)
    print("=== spark_catalog =", spark.conf.get("spark.sql.catalog.spark_catalog", "<unset>"))
    print("=== catalogResolver =", spark.conf.get("spark.sql.catalogResolver", "<unset>"))

    if a.phase in ("setup", "all"):
        setup(spark, a)
    if a.phase in ("multiformat", "all"):
        multiformat(spark, a)
    if a.phase == "named-local":
        named_local(spark, a)
    if a.phase == "cleanup":
        cleanup(spark, a)
    if a.phase == "producer-setup":
        producer_setup(spark, a)
    if a.phase in ("xacct-named",) or (a.phase == "all" and a.producer_account):
        xacct_named(spark, a)
    if a.phase in ("xacct-autowire",) or (a.phase == "all" and a.producer_account):
        xacct_autowire(spark, a)

    print("\n\n########## RESULT SUMMARY ##########")
    for status, name, msg in RESULTS:
        print(f"{status:4}  {name}  {msg}")
    print("####################################\n")

    spark.stop()
    if any(s == "FAIL" for s, _, _ in RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
