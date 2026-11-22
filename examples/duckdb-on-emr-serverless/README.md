# TPC-DS on Amazon EMR Serverless: DuckDB 2.0.1 (single node) vs Spark SQL

Companion code for the AWS Big Data Blog post *"Run single-node DuckDB SQL on Apache Iceberg with Amazon EMR Serverless."*

This folder contains the scripts and the raw results behind a TPC-DS comparison of DuckDB and Spark SQL
on Amazon EMR Serverless, reading Apache Iceberg tables on Amazon S3.

These are unaudited results from a modified TPC-DS setup. They cannot be compared with published TPC-DS results.

This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory and compliance requirements before deployment.

## Before you start

- An AWS account in a Region that offers EMR Serverless (the results use us-east-1) and permission to create S3 buckets,
  IAM roles and EMR Serverless applications.
- Python 3 with `pip install duckdb==2.0.1 boto3==1.43.123` on your machine: `duckdb` for `scripts/make_queries.py`, `boto3` for
  `scripts/run-tpcds.py`, which calls the EMR Serverless and S3 APIs with your AWS credentials (or `AWS_PROFILE`).
  Versions are pinned here and in `scripts/build_venv.sh`. DuckDB extensions are downloaded from the official DuckDB
  extension repository, and DuckDB refuses unsigned extensions by default.
- EMR Serverless vCPU quota. New accounts start at 16 concurrent vCPUs (see Service Quotas). The SF10 Iceberg conversion
  needs about 34 vCPU, so request an increase first. The account I tested in had a quota of 4000.
- A bucket, a job runtime role and an application. These commands create them
  (replace `amzn-s3-demo-bucket` with your bucket name):

```
export AWS_REGION=us-east-1
aws s3api create-bucket --bucket amzn-s3-demo-bucket --region $AWS_REGION

cat > trust.json <<'EOT'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"emr-serverless.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOT
cat > role-policy.json <<'EOT'
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":"arn:aws:s3:::amzn-s3-demo-bucket/*"},
 {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:ListBucketMultipartUploads"],"Resource":"arn:aws:s3:::amzn-s3-demo-bucket"}]}
EOT
aws iam create-role --role-name tpcds-job-runtime-role --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name tpcds-job-runtime-role --policy-name bucket-access --policy-document file://role-policy.json
aws emr-serverless create-application --name tpcds --type SPARK --release-label emr-7.14.0 --architecture X86_64
```

The last command prints the application id. The role ARN is `arn:aws:iam::<your-account-id>:role/tpcds-job-runtime-role`.
Wait a few seconds before using a new role: IAM needs a moment to propagate it.

## Quick start

A first run at SF10 (about 10 GB) on 8 vCPU for both engines. In my runs the DuckDB job took about 14 minutes and
the Spark SQL job about 19 minutes, and each job cost well under 1 USD at list rates.

```
export EMR_APP_ID=<application-id>            # EMR Serverless application: Spark, emr-7.14.0, x86_64
export EXEC_ROLE_ARN=<job-runtime-role-arn>   # read/write on the bucket, write on logs/
export S3_BUCKET=s3://<your-bucket>
export OUTDIR=../my-results                   # relative to scripts/; keeps the published results/ untouched

# 1. In AWS CloudShell (Amazon Linux 2023): build and upload the DuckDB 2.0.1 and tpcds extension archives.
#    Get the script into CloudShell first, for example: upload it with
#    aws s3 cp scripts/build_venv.sh $S3_BUCKET/tools/build_venv.sh, then in CloudShell (with S3_BUCKET exported):
#    aws s3 cp $S3_BUCKET/tools/build_venv.sh .
bash build_venv.sh

# 2. Generate the 99 queries and upload them with the scripts
python3 scripts/make_queries.py ./queries
aws s3 cp queries/queries-duckdb.json $S3_BUCKET/tpcds/queries-duckdb.json
aws s3 cp queries/queries-spark.json  $S3_BUCKET/tpcds/queries-spark.json
aws s3 cp scripts/ $S3_BUCKET/scripts/ --recursive --exclude "*" --include "tpcds_*.py"

# 3. Create the SF10 data and the Apache Iceberg tables
python3 scripts/run-tpcds.py gen-sf10
python3 scripts/run-tpcds.py iceberg-sf10

# 4. Run both engines at 8 vCPU
python3 scripts/run-tpcds.py duckdb-sf10-d8 spark-sf10-8
```

Per-query times (`best`, `all`) and billed resources land in `my-results/duckdb-sf10-d8.json` and `my-results/spark-sf10-8.json`;
compare them with the same files in `results/`.
Set `spark.emr-serverless.driver.disk` generously for DuckDB: it spills to local disk (see "Job configuration").
`run-tpcds.py` already sets 100 GB (8 and 16 vCPU) or 200 GB (32 vCPU). If you submit the jobs another way and keep the
default driver disk, DuckDB can run out of temporary space and queries can fail. With these settings, DuckDB 2.0.1
completed all 99 queries at every size (see "Query completion").
`scripts/analyze_results.py` needs the full set of runs, so use it on the provided `results/` folder.

What you should see at the end: both jobs finish with all 99 queries completed and the same row counts. In the
published runs the 99 queries added up to 139.8 s for DuckDB 2.0.1 and 544.0 s for Spark SQL, with DuckDB faster on
all 99. The runs were not isolated (see "Notes and limits"), so expect your times to differ somewhat from the
ones in `results/`.

## What was measured

- All 99 TPC-DS queries at scale factors (SF) 10, 100 and 1000, roughly 10 GB, 100 GB and 1 TB of raw data.
- Engine A: DuckDB 2.0.1, a patch release of the DuckDB 2.0 long-term support (LTS) line, on one EMR Serverless
  driver of 8, 16 or 32 vCPU, no executors. Official signed extensions: httpfs f71cfdb4bf, aws 9ba41b09a0,
  avro 4176191c96, iceberg f979b5b28a (engine source_id 8d78114e90).
- Engine B: Spark SQL 3.5.8 on the same application, with the same total vCPUs as 4-vCPU executors.
- Same Apache Iceberg tables for both engines (Hadoop catalog on S3, Parquet with zstd, unpartitioned).
- EMR Serverless release emr-7.14.0, x86_64, Python 3.11, us-east-1. The Spark SQL runs took place between 27 and
  29 October 2026, the DuckDB 2.0.1 runs on 21 and 22 November 2026 (UTC), on the same application, tables and job
  settings.
- Both engines with default settings. DuckDB reads the tables with `iceberg_scan`.
- Scope: DuckDB compared with Spark SQL on Amazon EMR Serverless only. This is not a comparison with Amazon Athena,
  Amazon Redshift or other engines, and the results should not be read as one.

## Layout

```
scripts/
  run-tpcds.py              submits jobs to EMR Serverless, waits, and saves per-query results to results/
  build_venv.sh             builds the DuckDB venv archive and the tpcds extension archive and uploads them to S3
  make_queries.py           writes queries-duckdb.json and queries-spark.json from the DuckDB tpcds extension
  tpcds_gen.py              DuckDB dsdgen -> Parquet on S3 (SF10, SF100)
  tpcds_to_iceberg.py       Spark: Parquet -> Iceberg tables
  tpcds_text_to_iceberg.py  Spark: pipe-delimited text (AWS public dataset, SF1000) -> Iceberg tables
  tpcds_duckdb.py           DuckDB runner, default settings
  tpcds_spark.py            Spark SQL runner
  tpcds_spark_stats.py      Spark: Iceberg table statistics, used by the tuned Spark runs
  analyze_results.py        recomputes the tables below from results/
  tpcds-schema.json         table schemas used for the text-to-Iceberg conversion
results/                    per-query timings and billed resources for every job (see "Results files")
```

## Configure

Set these before running `run-tpcds.py`:

| Variable | Meaning |
|---|---|
| `EMR_APP_ID` | EMR Serverless application (Spark, emr-7.14.0, x86_64) |
| `EXEC_ROLE_ARN` | job runtime role: read/write on the bucket, write on `logs/` |
| `S3_BUCKET` | `s3://<your-bucket>` that holds `envs/`, `scripts/`, `tpcds/` and `logs/` |
| `AWS_REGION` | default `us-east-1` |
| `AWS_PROFILE` | optional; use a profile whose credentials refresh themselves for long runs. If credentials expire, the runner stops polling and prints the job run id; run `label@jobRunId` to attach again |
| `REPEAT`, `TIMEOUT`, `ONLY`, `OUTDIR` | runs per query (default 2), per-query timeout in seconds (default 900), a list such as `19,64`, output folder (relative to `scripts/`; use `../my-results`) |

## Reproduce

1. Build the archives in AWS CloudShell (Amazon Linux 2023, x86_64): `S3_BUCKET=s3://<your-bucket> bash scripts/build_venv.sh`.
   This creates `envs/duckdb201_env.tar.gz` (DuckDB 2.0.1 with the httpfs, aws, avro and iceberg extensions)
   and `envs/tpcds_ext.tar.gz` (the tpcds extension, only needed to generate data). The archive script is adapted
   from the steps used for the original runs and was run end to end in CloudShell for this example.
2. Generate the query files and upload everything:
   ```
   python3 scripts/make_queries.py ./queries
   aws s3 cp queries/queries-duckdb.json $S3_BUCKET/tpcds/queries-duckdb.json
   aws s3 cp queries/queries-spark.json  $S3_BUCKET/tpcds/queries-spark.json
   aws s3 cp scripts/tpcds-schema.json   $S3_BUCKET/tpcds/tpcds-schema.json
   aws s3 cp scripts/ $S3_BUCKET/scripts/ --recursive --exclude "*" --include "tpcds_*.py"
   ```
   The query files regenerated this way are identical to the ones used for the results.
3. Create the data and the Iceberg tables (labels run in parallel when given on one command line):
   ```
   python3 scripts/run-tpcds.py gen-sf10 gen-sf100
   python3 scripts/run-tpcds.py iceberg-sf10 iceberg-sf100
   ```
   SF1000 uses the AWS public TPC-DS dataset (about 360 GB of gzip-compressed pipe-delimited text, dsdgen 2.13).
   Copy it in the same Region with
   `aws s3 sync s3://redshift-downloads/TPC-DS/2.13/1TB/ $S3_BUCKET/tpcds/text/sf1000/ --region us-east-1 --copy-props none`
   (`--copy-props none` is required: the public bucket denies reading object tags). Then run
   `python3 scripts/run-tpcds.py txticeberg-sf1000` (about 15 minutes with 128 vCPU of executors).
   Tables end up at `$S3_BUCKET/tpcds/warehouse_sf<N>/tpcds/<table>`.
4. Run the benchmark (set `OUTDIR=../my-results` so your runs land in `my-results/` and do not overwrite the published `results/`). The label is `<engine>-sf<scale>-<size>`:
   ```
   REPEAT=2 python3 scripts/run-tpcds.py duckdb-sf10-d8 duckdb-sf10-d16 duckdb-sf10-d32 spark-sf10-8 spark-sf10-16 spark-sf10-32
   REPEAT=1 TIMEOUT=1800 python3 scripts/run-tpcds.py duckdb-sf1000-d32 spark-sf1000-32
   ```
5. `python3 scripts/analyze_results.py results` prints the summary below.

## Job configuration

| Engine | Setting |
|---|---|
| DuckDB, 8 vCPU | `spark.driver.cores=8`, `spark.driver.memory=30g`, `spark.emr-serverless.driver.disk=100g` |
| DuckDB, 16 vCPU | `spark.driver.cores=16`, `spark.driver.memory=60g`, `spark.emr-serverless.driver.disk=100g` |
| DuckDB, 32 vCPU | `spark.driver.cores=32`, `spark.driver.memory=109g`, `spark.emr-serverless.driver.disk=200g` |
| Spark SQL | 2, 4 or 8 executors of 4 vCPU and 14 GB for the 8, 16 and 32 vCPU budgets, dynamic allocation off, 2 vCPU / 8 GB driver (not counted in the budget); SF1000 executors use `spark.emr-serverless.executor.disk=200g` |

The `run-tpcds.py` file also sets `spark.executor.disk=100g` for Spark, which is not an EMR Serverless setting, so
the Spark executors ran with the default disk size at SF10 and SF100.

DuckDB spills to the driver's local disk, and its temporary-directory limit follows the disk size (by default 90% of
the available disk space). This is why the disk setting matters: with too little disk, queries that spill a lot can
fail with an out-of-temporary-space error.

## Results

Totals are sums of the median per-query times over the queries completed on both engines: all 99 at every size.
SF10 has two runs per query, SF100 and SF1000 one. The cost column divides the billed compute of the Spark SQL job
by that of the DuckDB job (approximate list rates, only meaningful as a ratio).

```
scale   vCPU  DuckDB s   Spark s  ratio   n   cost
SF10       8     139.8     544.0   3.89  99   1.59
SF10      16     116.0     399.4   3.44  99   1.14
SF10      32     105.7     335.5   3.17  99   0.99
SF100      8     575.7    2210.7   3.84  99   3.03
SF100     16     364.5    1302.1   3.57  99   2.15
SF100     32     245.3     860.3   3.51  99   1.76
SF1000    32    1938.8    6285.2   3.24  99   3.10
```

"n" is the number of queries compared. DuckDB was faster on 99 of the 99 queries at SF10 (all three sizes) and at
SF100 on 32 vCPU, on 97 at SF100 on 8 vCPU, on 96 at SF100 on 16 vCPU and on 98 at SF1000. The queries where Spark
SQL was as fast or faster are Q8, Q9 and Q88, by under 3 s each.

The cost ratio is lower than the speed ratio because of time that is billed but not timed. `tpcds_duckdb.py` opens a
fresh connection for every run, and loading the extensions and creating the 24 table views happens before the timer
starts. At SF10 each DuckDB job ran for 763 to 850 s, and its timed runs added up to 211 to 280 s, so about 550 to
570 s per job was billed but not timed. Each Spark SQL job spent about 60 s outside the timer, which is the worker
start-up both engines have. The other 490 to 510 s on DuckDB is the per-run setup: about 2.5 s for each of the 198
runs (99 queries, two runs each). The SF10 queries are short, so this setup is about two thirds of each DuckDB job,
and the cost ratio drops to 1.59, 1.14 and 0.99 on 8, 16 and 32 vCPU. On 32 vCPU at SF10 the two engines cost about
the same. At SF100 and SF1000 there is one run per query and the queries run longer, so the untimed time (about 300 s
per job at SF100, 390 s at SF1000) weighs less and the cost ratios are 1.76 to 3.10. The extra disk DuckDB used is
under 1.5% of its job cost. Reusing one connection would cut this overhead, so the cost ratios are conservative for
DuckDB.

These results fit DuckDB's sweet spot: analytical queries on data that fits on one node, up to about 1 TB here.
For larger data, many concurrent users or long-running pipelines, Spark on Amazon EMR remains the better choice.
The two are complementary: pick the engine per workload.

### Query completion

DuckDB 2.0.1 completed all 99 queries in all seven configurations, and its row counts match Spark SQL for every query.
Spark SQL completed all 99 queries at every size, except in the tuned Spark runs (below).

Each DuckDB 2.0.1 job ran once, and none of the seven was killed. If a job ends early (for example a driver killed
for exceeding memory, exit code 137), set `ONLY` to the query numbers that are missing and retry. Set `OUTDIR`
explicitly for retries so they do not overwrite earlier results.

### Tuned Spark (not used as the baseline)

Spark SQL with Iceberg column statistics (`stats-sf*`) and `spark.sql.cbo.enabled` /
`spark.sql.cbo.joinReorder.enabled` (`results/tuned/sparkT-*`) was slower overall than the default Spark run.
Over the queries that completed in both runs, with the median per-query time as in "Results", suite totals were
33 to 51% higher at SF10 and 8 to 30% higher at SF100. Q72 failed at every size, and Q85 failed (8 and 16 vCPU) or
timed out (32 vCPU) at SF100. The default Spark run is the baseline in all comparisons, and the tuned Spark results
are for reference only. Run them with `stats-sf<N>` and then `sparkT-sf<N>-<V>` (same sizes as `spark-sf<N>-<V>`).

## Results files

Each file in `results/` is one job: `label`, `state`, `duration_s`, `billed` (vCPU-hours, memory GB-hours, storage
GB-hours), `misc` (engine version and settings echoed by the job) and `queries` (one record per query:
`q`, `best`, `all` timed runs in seconds, `rows`, `error`). Job run ids and account details were removed, and the
bucket name is replaced by `amzn-s3-demo-bucket`, in every results file. The `misc` line of the DuckDB 2.0.1 files
also shows `SETTINGS=default` and the effective `memory_limit`, `max_temp_directory_size` and `threads`, which the
runner used for those jobs echoed before the first query.

| Folder | Content |
|---|---|
| `results/*.json` | DuckDB 2.0.1 and Spark SQL runs for SF10 and SF100, and the Iceberg conversion |
| `results/sf1000/` | DuckDB 2.0.1 and Spark SQL runs for SF1000 and the text-to-Iceberg conversion |
| `results/tuned/` | tuned Spark and Spark statistics |

## Notes and limits

- The runs were not isolated: several jobs ran at the same time on separate workers reading the same bucket.
- Time covers query execution plus fetching the results. Job submission and worker start-up are not included, and
  neither is opening the fresh DuckDB connection for each run (see Results).
- DuckDB 2.0.1 was run about three weeks after Spark SQL. Spark SQL was run once; both used the same tables,
  application and job settings.
- SF10 and SF100 data come from DuckDB's dsdgen. SF1000 comes from the AWS public dataset, so row counts match the
  TPC-DS specification for the eight largest tables but the files were not produced by the same generator.
- Governance: this example reads Iceberg tables from a Hadoop catalog on S3, and DuckDB reads the table files directly.
  Catalog-level controls such as AWS Lake Formation row, column or cell permissions are therefore not enforced.
  If you adapt this to a governed data lake (for example tables registered in the AWS Glue Data Catalog with Lake
  Formation permissions), enforce access with your own controls, such as S3 and IAM permissions limited to the data
  the job may read.

## Clean up

The jobs, the application and the data cost money while they exist. When you are done, delete them
(use the same names as in "Before you start"):

```
aws emr-serverless stop-application --application-id $EMR_APP_ID
aws emr-serverless delete-application --application-id $EMR_APP_ID     # wait until the application is STOPPED
aws s3 rb s3://amzn-s3-demo-bucket --force                              # deletes ALL objects in the bucket
aws iam delete-role-policy --role-name tpcds-job-runtime-role --policy-name bucket-access
aws iam delete-role --role-name tpcds-job-runtime-role
```

Check that nothing is left with `aws emr-serverless list-applications` and `aws s3 ls`. S3 storage for the SF1000 data
is a few hundred GiB, so remove it promptly.
