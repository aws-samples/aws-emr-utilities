# Multi-catalog support in Amazon EMR 8.1

Companion demo for the AWS Big Data Blog post *"Simplify querying across AWS accounts and table
formats with multi-catalog support in Amazon EMR."*

Amazon EMR 8.1 introduces the **RedirectingSessionCatalog (RSC)**, a Spark default catalog that
resolves multiple table formats and multiple metastores. This demo shows, end to end, how a single
Spark query reads **Apache Iceberg, Delta Lake, Apache Hudi, and Apache Hive** tables through one
catalog — and how a **named catalog** and the **GlueCatalogResolver** extend that across **AWS
accounts** with no data copy.

> **Requires Amazon EMR 8.1** (the RSC classes do not exist in EMR 7.x). Validated on **EMR
> Serverless, Spark 4.1.1**. A single `run_demo.sh --phase all` created the four sample tables, ran
> the four-format join, and both cross-account phases (declared named catalog and auto-wiring),
> returning the remote account's rows joined to a local Iceberg table.

## What it demonstrates

| # | Capability | What it removes | Runs where |
|---|---|---|---|
| 1 | Multi-format | the *format* prefix | single account |
| 2 | Multi-catalog (cross-account) | the *account* boundary | two accounts |
| 3 | Auto-wiring | the *upfront configuration* | single + two accounts |

## Contents

```
env.template                    # copy to .env; holds app id / role / bucket / region
scripts/
├── bootstrap.sh                # create Glue DB, upload the worker, check the app
├── run_demo.sh                 # driver: sets Spark config per phase, submits to EMR Serverless
├── cleanup.sh                  # drop tables/database and wipe the warehouse
├── multicatalog_demo.py        # generic PySpark worker (issues SQL, reports PASS/FAIL)
├── bootstrap_consumer.sh       # (cross-account) create/prepare the consumer-account resources
├── bootstrap_producer.sh       # (cross-account) create the producer table + grants
└── crossaccount_policies/      # example Lake Formation grants, Glue resource + S3 bucket policies
```

The Spark catalog configuration is supplied at `spark-submit` time by `run_demo.sh`; the worker only
issues SQL, so the same script serves every phase. `bootstrap.sh`, `run_demo.sh`, and `cleanup.sh`
read `APP_ID` / `ROLE_ARN` / `BUCKET` / `REGION` from a `.env` file (see `env.template`), so you do
not repeat those flags on every command.

## Prerequisites

- An **Amazon EMR Serverless application on EMR 8.1** (Spark). The features also work on Amazon EMR
  on EC2 and Amazon EMR on EKS.
- An EMR Serverless **job execution role** with AWS Glue Data Catalog and Amazon S3 access.
- An **S3 bucket** for scripts, warehouse, and logs (this demo uses
  `s3://amzn-s3-demo-bucket/multicatalog/`).
- For the cross-account phases: a second (producer) account, and the cross-account grants in
  `scripts/crossaccount_policies/` (Lake Formation, Glue resource policy, S3 bucket policy, and a
  customer-managed KMS key).

## Quick start (single account, multi-format)

```bash
# 0) Configure once
cp env.template .env
# edit .env: APP_ID, ROLE_ARN, BUCKET, REGION

# 1) Create the Glue database, upload the worker, check the application
./scripts/bootstrap.sh

# 2) Create one sample table per format (Iceberg, Delta, Hudi, Hive), 3 rows each
./scripts/run_demo.sh --phase setup

# 3) Run the four-format join through one catalog, no format prefixes
./scripts/run_demo.sh --phase query
```

(`query` is an alias for the `multiformat` phase. All flags may still be passed explicitly —
`--app-id`, `--role-arn`, `--bucket`, `--region` — to override `.env`.)

Expected join output:

```
+---+--------+------+------+------+
| id| iceberg| delta| hudi | hive |
+---+--------+------+------+------+
|  1| ice-1  | dl-1 | hu-1 | hv-1 |
|  2| ice-2  | dl-2 | hu-2 | hv-2 |
|  3| ice-3  | dl-3 | hu-3 | hv-3 |
+---+--------+------+------+------+
```

Each column comes from a different table format, joined in one query with no format-specific catalog
configuration.

## Phases

`run_demo.sh --phase <phase>` supports:

| Phase | What it does |
|---|---|
| `setup` | Create one sample table per format in `salesdb`, 3 rows each: `orders_iceberg`, `returns_delta`, `shipments_hudi`, `products_hive`. |
| `multiformat` | Join all four formats in one query, unqualified names. |
| `named-local` | Single-account demo of the named-catalog mechanism (a named RSC pointed at this account). |
| `producer-setup` | Create the producer Hive table (run with the producer account's app/role). |
| `xacct-named` | Read the producer table through a declared named catalog, joined to local Iceberg. |
| `xacct-autowire` | Read the producer table by its backtick-quoted account id — no catalog declaration. |
| `cleanup` | Drop the demo tables/database and wipe the S3 warehouse. |
| `all` | `setup` + `multiformat`, plus the two cross-account phases when `--producer-account` is given. |

## Cross-account (optional)

Three steps, run in this order — the consumer execution role must exist before the producer can
grant to it:

```bash
# 1) CONSUMER account (where EMR runs): create the execution role, application, and bucket.
#    Prints the role ARN to use in step 2. Add --dry-run to preview without creating anything.
./scripts/bootstrap_consumer.sh --region us-east-1 --producer-account 111122223333

# 2) PRODUCER account (owns the data): create the sample table (salesdb.fulfillment) and grant
#    the consumer role read access across Lake Formation + Glue + S3. Supports --dry-run.
./scripts/bootstrap_producer.sh --consumer-role <ROLE_ARN from step 1>

# 3) CONSUMER account: read across the account boundary via auto-wiring, joined to a local
#    Iceberg table. (App/role/bucket come from .env.)
./scripts/run_demo.sh --phase xacct-autowire --producer-account 111122223333 --producer-table fulfillment
```

Use `--phase xacct-named` instead to read through a declared named catalog rather than auto-wiring;
`--phase all --producer-account <id>` runs the single-account demo and both cross-account phases in
one shot (it re-creates the local tables).

Cross-account access is query-time resolution only — it does not copy data. It requires Lake
Formation grants, a Glue resource policy, an S3 bucket policy, and (if encrypted) a customer-managed
KMS key. See `scripts/crossaccount_policies/` for examples.

## Clean up

```bash
./scripts/cleanup.sh
```

This drops the demo tables and database and wipes the S3 warehouse. Delete the EMR Serverless
application and execution role separately if you created them only for this demo.
