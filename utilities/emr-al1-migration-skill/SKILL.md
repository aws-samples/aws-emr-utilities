---
name: emr-al1-migration
description: >
  Migrate EMR clusters and applications from EMR 5.0–5.35 (Amazon Linux 1) to EMR 7.x (Amazon Linux 2023).
  Use when a customer needs to upgrade EMR clusters off AL1 as part of the AL1 deprecation,
  upgrade Spark application code (Python/Scala/Java) using the Apache Spark Upgrade Agent MCP server,
  migrate Hive 2.3→3.1 scripts, convert Presto queries to Trino, adapt MapReduce jobs for Hadoop 3,
  update Flink deployment mode and memory model, convert Pig scripts to PySpark (via PigToSparkConversion MCP),
  migrate Zeppelin notebooks for EMR 7.x compatibility, adapt bootstrap actions for AL2023 compatibility,
  resolve Java 8-to-17 issues, or troubleshoot Hadoop 2-to-3 breaking changes on EMR.
  Covers EC2-based EMR clusters only (not EMR on EKS or EMR Serverless).
  Note: EMR 5.36+ already uses AL2 (not AL1) — those clusters have a different migration path.
owner_team: EMRMigrations
owner_cti: AWS/EMR/Migrations
stages: [preprod]
version: 2
metadata:
  service: [emr, ec2, s3, cloudwatch-logs]
  task: [migrate, upgrade, troubleshoot]
  persona: [developer, data-engineer, platform-engineer]
  workload: [big-data, analytics, etl]
---

# EMR AL1 Migration Skill

Migrate an EMR cluster configuration **and its applications** from EMR 5.0–5.35 (Amazon Linux 1) to EMR 7.x (Amazon Linux 2023). This skill handles the entire process end-to-end: cluster config adaptation, application code upgrades (Spark, Hive, Presto→Trino, MapReduce, Flink, Pig→PySpark, Zeppelin notebooks), test cluster launch, validation, and iterative fix loops. The original cluster is never modified; all work happens on a new test cluster.

> The AWS MCP server is recommended for sandboxed execution and audit logging, but this skill works with any agent that has AWS CLI access. For Spark source code upgrades, the skill directly uses the Apache Spark Upgrade Agent MCP tools when connected.

**Important version boundaries:**
- EMR 5.0–5.35 → Amazon Linux 1 (AL1) — **this skill's scope**
- EMR 5.36–5.37 → Amazon Linux 2 (AL2) — different migration path (no AL1 bootstrap issues)
- EMR 6.x → Amazon Linux 2 (AL2) — different migration path
- EMR 7.x → Amazon Linux 2023 (AL2023) — target

## Scope

| Source | EMR 5.0–5.35 (Amazon Linux 1) |
|--------|------------------------------------------|
| Target | EMR 7.x (latest stable on Amazon Linux 2023) |
| Cluster Migration | Configs, bootstrap actions, instance types, security |
| Application Migration | Spark (via Upgrade Agent MCP), Hive, Presto→Trino, MapReduce, Flink, Pig→PySpark (via PigToSparkConversion MCP), Zeppelin notebooks |
| Broken apps (still installable) | Pig 0.17.0 — installs on EMR 7.x but ORDER BY/JOIN fail due to Java 17 serialization bug; must convert to PySpark |
| Deprecated apps (still available) | Oozie 5.2.1 — functional but unmaintained; recommend conversion to Step Functions |
| Removed apps | Ganglia, Mahout, Sqoop (from EMR 7.5+) |
| Strategy | Gather → adapt cluster → upgrade apps → launch test cluster → validate → fix loop (≤5) → report |

## Parameters

Collect these from the user before proceeding:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `CLUSTER_ID` | Yes | Running cluster ID (`j-XXXXXXXXXXXXX`) or saved configuration name |
| `REGION` | Yes | AWS region |
| `TARGET_RELEASE` | No | Specific EMR 7.x release label (default: `emr-7.1.0`) |
| `DRY_RUN` | No | If true, produce config only without launching (default: false) |
| `VALIDATE_ONLY` | No | If true, run validation against an existing target cluster without migration steps (default: false) |
| `SPARK_APP_PATH` | No | Local path to Spark application code to upgrade (Python/Scala/Java project) |
| `STAGING_BUCKET` | No | S3 path for upgrade artifacts (required if SPARK_APP_PATH provided) |
| `PIG_DAG_PATH` | No | Local path to Airflow DAG file containing Pig script references (required for Pig migration) |
| `PIG_DOMAIN_NAME` | No | Domain name for Pig-to-Spark conversion output structure |
| `ZEPPELIN_NOTEBOOKS_PATH` | No | Local path or S3 path to Zeppelin notebook JSON exports to migrate |
| `ZEPPELIN_URL` | No | Target Zeppelin server URL for notebook upload (default: `http://localhost:8890`) |
| `RETRY_AZ` | No | If true, automatically retry cluster launch in a different AZ on provisioning failures (default: true) |

## Prerequisites

The user MUST have:
- AWS CLI v2 configured with credentials that have the permissions listed in `references/iam-permissions.md`
- A VPC/subnet where EMR clusters can launch
- An S3 bucket for adapted scripts (or reuse the cluster's existing log bucket)

**Source cluster state detection**: Before beginning migration, the skill will auto-detect whether the source cluster is still running (`WAITING`/`RUNNING`) or terminated. If terminated, the skill will:
- Skip live cluster inspection steps
- Use the cluster's saved configuration and last-known state from `describe-cluster`
- Warn the user that data validation against source is not possible

For Spark application upgrades (optional):
- The Apache Spark Upgrade Agent MCP server configured (see Stage 3A)
- Python 3.10+ and `uv` package manager installed locally
- A target EMR 7.x cluster or the one launched in Stage 4

For Pig application migration (optional):
- The PigToSparkConversion MCP server configured (see Stage 3F)
- Airflow DAG file(s) that reference Pig scripts via SSHOperator
- Hive metastore connectivity for table dependency resolution
- Python 3.10+ and `poetry` for running converted PySpark tests

For Zeppelin notebook migration (optional):
- Exported Zeppelin notebook JSON files (from EMR 5.x cluster)
- Target Zeppelin server accessible (EMR 7.x cluster or standalone)

### Cloning the Skill Repository

```bash
# Clone from GitHub
git clone https://github.com/aws-samples/aws-emr-utilities.git
cd aws-emr-utilities/utilities/emr-al1-migration-skill
```

## Workflow

### Stage 1 — Gather Cluster Information

```bash
aws emr describe-cluster --cluster-id $CLUSTER_ID --region $REGION
aws emr list-instance-groups --cluster-id $CLUSTER_ID --region $REGION
aws emr list-steps --cluster-id $CLUSTER_ID --region $REGION
aws emr list-bootstrap-actions --cluster-id $CLUSTER_ID --region $REGION
```

From the output:
1. **Verify source is AL1**: Check `ReleaseLabel`. If `emr-5.36.0` or later, HALT — this cluster is already on AL2, not AL1. Inform user this skill targets EMR 5.0–5.35 only.
2. Extract installed applications, configurations, bootstrap actions, and step definitions.
3. Download all referenced bootstrap scripts and custom JARs from S3.
4. Classify the primary workload type(s): Spark, Hive, Presto, HBase, Flink, Pig, Zeppelin, Oozie, Custom JAR.
5. Identify Spark application source code if `SPARK_APP_PATH` provided or discoverable from step JARs/scripts.
6. Check for **hard blockers** — halt immediately if found:
   - MapR filesystem → not supported on EMR 7.x
   - Ganglia → removed; note CloudWatch/Prometheus alternative
7. Store the full original configuration as a JSON backup artifact in S3.

### Stage 2 — Adapt Cluster Configuration

Load `references/configuration-transforms.md` and apply all transformations in order:

1. **Release label**: set to `TARGET_RELEASE`
2. **Application versions**: map to EMR 7.x equivalents (Spark 2.4→3.5, Hive 2.3→3.1, Presto→Trino, HBase 1.4→2.5, Hadoop 2.10→3.3)
3. **Configuration properties**: remove deprecated, rename changed, add required new properties
4. **Bootstrap actions**: adapt for AL2023 (yum→dnf, service→systemctl, python→python3, Java paths, IMDSv1→IMDSv2)
   - **IMDSv2 is enforced on EMR 7.5** — any `curl http://169.254.169.254/...` calls (IMDSv1) will return HTTP 401. Replace with token-based IMDSv2:
     ```bash
     # OLD (fails on EMR 7.5):
     # INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)

     # NEW (IMDSv2):
     TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
       -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
     INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
       http://169.254.169.254/latest/meta-data/instance-id)
     ```
   - Scan ALL bootstrap scripts for patterns: `curl.*169.254.169.254`, `wget.*169.254.169.254`, `ec2-metadata` (deprecated CLI)
5. **Step definitions**: update spark-submit args, Hive DDL, Presto→Trino connection strings
6. **Security**: verify Kerberos config, Lake Formation integration, security groups
7. **Instance types**: validate availability; suggest current-gen replacements if deprecated

8. **Java 8 → 17 compatibility**: EMR 7.x uses Java 17 (Amazon Corretto 17) by default. Scan custom JARs and spark-submit args for:
   - `--add-opens` / `--add-exports` flags → keep (needed for internal API access)
   - Reflection on `sun.*` or `com.sun.*` packages → add `--add-opens java.base/sun.nio.ch=ALL-UNNAMED` (etc.) to `spark.driver.extraJavaOptions` and `spark.executor.extraJavaOptions`
   - Removed packages: `javax.xml.bind` (JAXB), `javax.annotation`, `javax.activation` → add explicit dependencies to application JARs
   - If Java 8 is absolutely required (rare): EMR 7.x supports Corretto 8 via `export JAVA_HOME=/usr/lib/jvm/java-1.8.0-amazon-corretto` in bootstrap — but this disables Java 17 performance improvements

9. **Log4j configuration format**: Spark on EMR 7.x uses `log4j2.properties` (Log4j2 format). Custom `log4j.properties` files (Log4j1 format) **will not be loaded** — EMR 7.x ignores them silently (no error, just default logging behavior). Convert as follows:
   - EMR classification: `spark-log4j` → `spark-log4j2`
   - File format: `log4j.properties` → `log4j2.properties` (different syntax)
   - Example conversion:
     ```properties
     # OLD (log4j.properties - Log4j1):
     # log4j.rootCategory=WARN,console
     # log4j.logger.org.apache.spark=INFO

     # NEW (log4j2.properties - Log4j2):
     rootLogger.level = warn
     rootLogger.appenderRef.stdout.ref = console
     logger.spark.name = org.apache.spark
     logger.spark.level = info
     ```
   - Note: Log4j1 Java API calls (`org.apache.log4j.Logger`) still work via the `log4j-1.2-api` bridge JAR bundled with EMR 7.x
   - Note: Hadoop still uses `log4j.properties` format (inconsistency — do NOT convert hadoop-log4j classification)
   - Hive uses `hive-log4j2` classification (already Log4j2 format)
   - See `references/configuration-transforms.md` section 4.8 for complete mapping.

10. **EMRFS Consistent View removal**: Remove all `fs.s3.consistent*` properties from `emrfs-site` classification. Remove `emrfs sync`/`emrfs delete` calls from bootstrap actions. The `emrfs` CLI does not exist on EMR 7.x. See `references/configuration-transforms.md` section 4.9.

11. **Glue Data Catalog compatibility**: If source cluster uses Glue as Hive metastore (`hive.metastore.client.factory.class = com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory`):
    - Glue does NOT support Hive ACID/transactional tables → all ACID tables must become EXTERNAL (see Stage 3B step 3)
    - Glue catalog ID format unchanged between EMR 5.x and 7.x
    - `spark.hadoop.hive.metastore.client.factory.class` must be set in `spark-defaults` (not just `hive-site`) for Spark to use Glue
    - Column statistics format changed in Hive 3 — run `ALTER TABLE ... SET TBLPROPERTIES('COLUMN_STATS_ACCURATE'='false')` to force recomputation

Upload adapted bootstrap scripts to S3 with `-emr7-migrated` suffix.

### Stage 3 — Upgrade Applications

> **Backward Compatibility Note**: EMR 7.5 has strong backward compatibility for many Spark and Hive patterns. Testing confirmed that `registerTempTable()`, `unionAll()`, `SQLContext`, implicit type casting, and basic Hive DDL all still work on EMR 7.5 without modification. The migration fixes applied by this skill are still recommended for **future-proofing** (these deprecated aliases will eventually be removed in future Spark/EMR releases), but users should understand that not all changes fix immediate failures. The skill distinguishes between:
> - **Critical breaks** (will fail immediately): Pig ORDER BY/JOIN, Presto CLI, Python 2 shebangs, IMDSv1, Hive ACID ORC, Zeppelin %pig/%sh
> - **Future-proofing** (works now but deprecated): `registerTempTable`, `unionAll`, `SQLContext`, `s3n://`, Flink `-m yarn-cluster`
>
> All changes are applied regardless, since the goal is a fully modernized codebase — but when communicating with users, be clear about which fixes are immediately necessary vs. best-practice upgrades.

This skill handles ALL application upgrades directly. For Spark code upgrades, the skill uses the Apache Spark Upgrade Agent MCP server tools when available — no separate prompting or manual invocation needed. The agent detects available MCP tools and calls them automatically.

#### Stage 3A — Spark Application Code Upgrade (if SPARK_APP_PATH provided)

**When the Spark Upgrade Agent MCP server is connected**, use its tools directly to upgrade application source code:

0. **Create a working copy**: Before any modifications, copy the entire `SPARK_APP_PATH` directory to a new location with `-emr7-migrated` suffix (e.g., `my-spark-app/` → `my-spark-app-emr7-migrated/`). All subsequent upgrade operations target the copy — the original source is never modified.
1. Call `check_and_update_build_environment` to update build files (pom.xml, build.sbt, requirements.txt, Pipfile) for Spark 3.5 / Scala 2.12 compatibility.
2. Call `check_and_update_python_environment` (for PySpark projects) to update Python dependencies.
3. Call `compile_and_build_project` to verify the upgraded code compiles.
4. Call `check_job_status` to monitor validation job runs on the target EMR cluster.

The Spark Upgrade Agent tools handle:
- **Build configuration**: Updates dependency versions for EMR 7.x compatibility
- **Source code**: Fixes deprecated API usage (SQLContext→SparkSession, registerTempTable→createOrReplaceTempView, etc.)
- **Scala version**: 2.11→2.12 binary compatibility fixes
- **Dependencies**: Upgrades to EMR 7.x-compatible versions
- **Test code**: Ensures unit/integration tests pass with target Spark version
- **Validation**: Compiles and submits application to target EMR cluster
- **Data quality**: Detects schema/value-level differences between source and target outputs

Supported languages: Python, Scala (Maven/SBT), Java (Maven)

Limitations:
- Private artifact repository dependencies must be upgraded manually
- Bootstrap actions are NOT upgraded by the Spark Upgrade Agent (handled in Stage 2)
- The upgrade agent iterates one fix at a time (error-driven approach)

**When the Spark Upgrade Agent MCP server is NOT connected**, copy the `SPARK_APP_PATH` to a new directory with `-emr7-migrated` suffix, then apply fixes directly to the copy using `references/failure-catalogue.md`:
- Apply cluster-level legacy compat flags (SPARK_SQL_LEGACY, SPARK_PARQUET_TIMESTAMP)
- Rewrite deprecated APIs in source code (SPARK_REMOVED_APIS)
- Fix Scala 2.11→2.12 issues (SPARK_SCALA_BINARY)
- Fix Python 2→3 issues (SPARK_PYTHON_VERSION)
- Resolve dependency conflicts (SPARK_DEPENDENCY_CONFLICT)

**Spark Upgrade Agent MCP server setup (prerequisite — user must complete before using this skill):**

The user needs the `spark-upgrade` MCP server connected to their agent. Setup instructions: https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-upgrade-agent-setup.html

Summary:
1. Deploy the CloudFormation stack `spark-upgrade-mcp-setup` in their region
2. Add the MCP server config to their agent (Kiro, Claude Code, etc.):
```json
{
  "mcpServers": {
    "spark-upgrade": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "https://sagemaker-unified-studio-mcp.<REGION>.api.aws/spark-upgrade/mcp",
        "--service", "sagemaker-unified-studio-mcp",
        "--profile", "spark-upgrade-profile",
        "--region", "<REGION>",
        "--read-timeout", "180"
      ],
      "timeout": 180000
    }
  }
}
```

Once connected, this skill will automatically detect and use the Spark Upgrade Agent tools.

#### Stage 3B — Hive Application Migration

For clusters with Hive workloads, adapt HQL scripts and queries for Hive 3.1:

1. **Inventory Hive assets**: List all `.hql` files in S3 step definitions, scheduled queries, and DDL scripts.

2. **Apply Hive 2.3→3.1 fixes** (run-fail-fix loop per script, max 5 iterations):

   a. **ACID compatibility** — Hive 3 defaults managed tables to ACID/transactional. The fix is to convert tables to EXTERNAL (do NOT use `SET hive.create.as.acid=false` — this property does not exist on EMR 7.x and will cause the script to fail):
   ```sql
   -- Convert existing managed tables:
   ALTER TABLE t SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');
   -- Or create new tables as EXTERNAL from the start:
   CREATE EXTERNAL TABLE ...
   ```

   b. **Reserved keywords** — Quote with backticks: `` `date` ``, `` `time` ``, `` `timestamp` ``, `` `interval` ``, `` `user` ``, `` `role` ``

   c. **INSERT OVERWRITE on managed tables** — Convert to EXTERNAL tables (preferred) or use transactional MERGE syntax

   d. **Type safety** — Add explicit `CAST()` for implicit conversions that Hive 3 rejects (e.g., `WHERE amount > '50'` → `WHERE amount > CAST('50' AS DOUBLE)`)

   e. **Execution engine** — Remove any `SET hive.execution.engine=mr;` from scripts. EMR 7.x defaults to Tez. MR engine is deprecated.

   f. **Metastore schema** — Glue Catalog: no action. External HMS: run `schematool -upgradeSchema`

   g. **Invalid SET properties** — EMR 7.x Hive rejects unknown properties with a non-zero exit. Remove: `hive.create.as.acid`, `hive.create.as.insert.only`, `hive.strict.managed.tables`. These do not exist in EMR 7.x Hive 3.1.

3. **ACID Table Data Migration (Critical)** — Hive 2.x ACID tables store data in ORC delta files (`delta_NNNNNN_NNNNNN/bucket_NNNNN`) that are **incompatible with Hive 3.x**. Recreating the table on EMR 7.x and running `MSCK REPAIR TABLE` discovers partitions but data is invisible (`SELECT *` returns 0 rows). This is NOT fixed by `schematool -upgradeSchema` alone — the on-disk file format must also be addressed.

   a. **Detect ACID tables** in the source cluster's metastore:
   ```sql
   -- Query the metastore database (MySQL/PostgreSQL) directly:
   SELECT t.TBL_NAME, d.NAME as DB_NAME
   FROM TBLS t
   JOIN DBS d ON t.DB_ID = d.DB_ID
   JOIN TABLE_PARAMS tp ON t.TBL_ID = tp.TBL_ID
   WHERE tp.PARAM_KEY = 'transactional' AND tp.PARAM_VALUE = 'true';

   -- Or from Hive CLI on the source cluster:
   -- Check individual tables:
   SHOW TBLPROPERTIES db.table_name;
   -- Look for: transactional = true
   ```

   b. **Export to non-ACID EXTERNAL table (REQUIRED — primary approach)**:

   > **WARNING**: Major compaction alone is NOT sufficient. Testing confirmed that even compacted `base_NNNNNN/` files retain Hive 2.x ACID ORC column encoding, which Hive 3.x cannot read (`ClassCastException: BytesColumnVector cannot be cast to LongColumnVector`). The ONLY reliable fix is exporting data to a new non-ACID table.

   On the source EMR 5.x cluster (or a temporary one if the original is terminated):
   ```sql
   -- 1. Export each ACID table to clean non-ACID format:
   CREATE EXTERNAL TABLE db.table_name_export
   STORED AS ORC
   LOCATION 's3://bucket/migration-export/table_name/'
   AS SELECT * FROM db.table_name;

   -- 2. Verify export contains all rows (including UPDATE/DELETE results):
   SELECT COUNT(*) FROM db.table_name;
   SELECT COUNT(*) FROM db.table_name_export;

   -- 3. On EMR 7.x, create table pointing to exported data:
   CREATE EXTERNAL TABLE db.table_name
   (... original schema ...)
   STORED AS ORC
   LOCATION 's3://bucket/migration-export/table_name/'
   TBLPROPERTIES ('external.table.purge'='true');
   ```

   c. **If source EMR 5.x cluster is already terminated** — launch a temporary EMR 5.x cluster with the same hive-site config (`hive.txn.manager=DbTxnManager`, same external metastore/Glue), then perform the export above.

   d. **If data can be regenerated from upstream** — re-run the ingestion pipeline directly on EMR 7.x to produce data natively in Hive 3.x format.

   e. **Why compaction is NOT sufficient** (do NOT use as primary fix):
   - Major compaction creates `base_NNNNNN/` files but the ORC data inside still uses Hive 2.x ACID column encoding
   - Hive 3.x's ORC reader expects a different column layout and throws `ClassCastException`
   - This was validated in end-to-end testing: compacted tables still fail on EMR 7.5

   f. **Important distinctions**:
   - `schematool -upgradeSchema` = fixes metastore DB schema (necessary but NOT sufficient for ACID tables)
   - Export to non-ACID = the ONLY reliable fix for ACID data (creates clean ORC without ACID encoding)
   - AWS Glue Data Catalog does NOT support Hive ACID — if using Glue, all ACID tables must become EXTERNAL regardless
   - Non-ACID tables (`transactional` not set or `false`) work fine without export — standard ORC is cross-version compatible

4. **Upload adapted scripts** to S3 with `-hive3-migrated` suffix.

5. **Validate** each script on the test cluster:
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=HIVE,Name=validate-hive-migration,ActionOnFailure=CONTINUE,Args=[-f,s3://bucket/script-hive3-migrated.hql]
```

6. On failure: fetch logs, classify against `references/failure-catalogue.md` (HIVE3_ACID_DEFAULT, HIVE3_MANAGED_TABLE, HIVE3_SYNTAX_CHANGES, HIVE3_TYPE_CONVERSION, HIVE_METASTORE_SCHEMA, HIVE2_ACID_DELTA_FORMAT_INCOMPATIBLE), apply fix, resubmit.

#### Stage 3C — Presto → Trino Migration

EMR 7.x replaces Presto with Trino (complete rebrand). For clusters running Presto workloads:

1. **Inventory Presto assets**: List all queries, JDBC connections, scripts referencing `presto-cli`, and applications using the Presto JDBC driver.

2. **Apply renames** (run-fail-fix loop per script/connection, max 5 iterations):

   a. **CLI**: `presto-cli` → `trino-cli`

   b. **JDBC driver**:
   - `com.facebook.presto.jdbc.PrestoDriver` → `io.trino.jdbc.TrinoDriver`
   - `jdbc:presto://host:port/catalog` → `jdbc:trino://host:port/catalog`

   c. **Configuration classifications**:
   - `presto-connector-*` → `trino-connector-*`
   - `presto-config` → `trino-config`

   d. **Connector names**: `connector.name=hive-hadoop2` → `connector.name=hive`

   e. **Session properties**: Remove `presto.` prefix from all session property references

   f. **SQL semantics**: `current_timestamp` now returns `timestamp with time zone`; `json_extract` returns JSON type (cast to VARCHAR if string expected)

3. **Upload adapted scripts** to S3 with `-trino-migrated` suffix.

4. **Validate** on test cluster:
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-trino-migration,ActionOnFailure=CONTINUE,Jar=command-runner.jar,\
  Args=[trino-cli,--execute,"SELECT 1"]
```

5. On failure: classify against `references/failure-catalogue.md` (PRESTO_TO_TRINO_RENAME, TRINO_SQL_CHANGES), apply fix, resubmit.

#### Stage 3D — MapReduce Application Migration

MapReduce is deprecated but still functional on EMR 7.x via YARN. For clusters with MR workloads:

1. **Inventory MR assets**: List all steps using `hadoop jar`, `hadoop streaming`, or custom MR JARs.

2. **Apply Hadoop 2→3 fixes** (run-fail-fix loop, max 5 iterations):

   a. **API removals**: Recompile JARs against Hadoop 3.3.x:
   - `org.apache.hadoop.mapred.*` (old API) → `org.apache.hadoop.mapreduce.*` (new API)
   - `JobConf` → `Job.getInstance(Configuration)`
   - `JobClient.runJob(conf)` → `job.waitForCompletion(true)`

   b. **S3 scheme**: `s3n://` → `s3://` in all input/output paths

   c. **Classpath changes**:
   - `/usr/lib/hadoop/lib/` → `/usr/lib/hadoop/share/hadoop/common/lib/`
   - `/usr/lib/hadoop-mapreduce/` paths unchanged

   d. **Configuration properties**:
   - Remove `fs.s3n.*` properties
   - `mapreduce.framework.name` remains `yarn`

   e. **Streaming jobs**: Verify Python/shell scripts use Python 3 and AL2023-compatible commands

3. **Upload adapted JARs and scripts** to S3 with `-hadoop3-migrated` suffix (e.g., `job.jar` → `job-hadoop3-migrated.jar`, `mapper.py` → `mapper-hadoop3-migrated.py`). Original files are never modified.

4. **Validate** on test cluster:
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-mr-migration,ActionOnFailure=CONTINUE,\
  Jar=s3://bucket/path/job-hadoop3-migrated.jar,Args=[<input-path>,<output-path>]
```

5. On failure: classify against `references/failure-catalogue.md` (HADOOP3_API_BREAK, S3_SCHEME_DEPRECATED, JAVA_VERSION_INCOMPATIBLE), apply fix, resubmit.

**Recommended path**: Convert MR jobs to Spark for long-term supportability. MR works on EMR 7.x but receives no performance improvements.

#### Stage 3E — Flink Application Migration

For clusters running Flink workloads on YARN:

1. **Inventory Flink assets**: List all Flink jobs (JAR submissions, Python DataStream/Table API scripts).

2. **Apply Flink migration fixes** (run-fail-fix loop, max 5 iterations):

   a. **Deployment mode** (breaking change):
   - `flink run -m yarn-cluster` → `flink run-application -t yarn-application`
   - Per-job mode (`-m yarn-cluster`) deprecated in Flink 1.15+, removed behavior in later versions
   - Use application mode (`yarn-application`) or session mode (`yarn-session`)

   b. **Memory model** (changed in Flink 1.10+):
   - Legacy `taskmanager.heap.mb` → `taskmanager.memory.process.size`
   - Legacy `jobmanager.heap.mb` → `jobmanager.memory.process.size`
   - Add `taskmanager.memory.managed.fraction=0.4` (default, verify against workload)

   c. **Configuration file**: `flink-conf.yaml` location unchanged on EMR, but verify properties:
   - `state.backend` → `state.backend.type` (renamed in Flink 1.13+)
   - `state.backend.rocksdb.*` → verify RocksDB native library compatible with AL2023

   d. **Connector versions**: Flink-Kafka, Flink-Kinesis connectors must match Flink 1.18.x:
   - Update connector JAR versions in S3
   - Verify Kafka client compatibility (Flink 1.18 uses Kafka client 3.4+)

   e. **Java version**: Flink 1.18 supports Java 11 and 17. Verify application code compatible:
   - Add `--add-opens` JVM flags if reflection-based serialization used
   - `env.java.opts: "--add-opens java.base/java.util=ALL-UNNAMED"`

3. **Upload adapted JARs and configs** to S3 with `-flink18-migrated` suffix (e.g., `flink-app.jar` → `flink-app-flink18-migrated.jar`). Original files are never modified.

4. **Validate** on test cluster:
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-flink-migration,ActionOnFailure=CONTINUE,\
  Jar=command-runner.jar,Args=[flink,run-application,-t,yarn-application,\
  -Dyarn.application.name=migration-test,s3://bucket/path/flink-app-flink18-migrated.jar]
```

5. On failure: classify against `references/failure-catalogue.md` (FLINK_YARN_CHANGES, JAVA_VERSION_INCOMPATIBLE), apply fix, resubmit.

#### Stage 3F — Pig Application Migration (Pig → PySpark)

Pig 0.17.0 is still installable on EMR 7.x but is **functionally broken** for non-trivial scripts. While Pig can still be added as an application to EMR 7.x clusters, it fails at runtime on any script that requires multi-vertex Tez DAGs (ORDER BY, JOIN, COGROUP) due to a Java 17 serialization incompatibility in Pig's internal `OperatorKey` class. Converting Pig workloads to PySpark is **required** (not just recommended). This stage leverages the **PigToSparkConversion MCP server** (`code.amazon.com/packages/PigToSparkConversion`) which provides AST-based parsing, automated conversion, test generation, and validation tooling.

> **CRITICAL**: Pig 0.17.0 on EMR 7.x (Java 17) crashes with `java.io.IOException: Deserialization error: Cannot invoke "org.apache.pig.impl.plan.OperatorKey.hashCode()" because "this.mKey" is null` on any operation requiring data exchange between Tez vertices (ORDER BY, JOIN, COGROUP, etc.). Simple single-vertex operations (LOAD, FILTER, GROUP BY + DUMP) may still work, but any production script with sorting or joins will fail. There is no fix — Pig 0.17.0 (June 2017) is the last Apache Pig release ever, and it was never updated for Java 17. The script itself cannot be modified to work around this; the failure is inside Pig's engine, not in user code. **The only migration path is converting to PySpark.**

**When the PigToSparkConversion MCP server is connected**, use its tools directly:

1. **Extract Pig scripts from DAG**: Call `extract_pig_files_from_dag` with the Airflow DAG file path to identify all Pig script references (SSHOperator tasks with `pig.sh` commands). Tasks already using `SparkLivyBatchOperator` are excluded automatically.

2. **Parse and analyze each Pig script**: Call `pig_ast_parser_tool` for each discovered `.pig` file to generate an AST with:
   - LOAD/STORE dependency graph
   - Column lineage tracking
   - UDF identification
   - JOIN type analysis

3. **Orchestrate conversion**: Call `pig_to_spark_converter_tool` with domain name, pig file list, and Hive metastore config. The tool handles:
   - **File classification**: prepares/ → `data_store/`, transforms/ → `info_store/`, maps → `maps_data_store/`
   - **PySpark class generation**: Each Pig script → a PySpark class with `compute()` entry point
   - **UDF mapping**: Pig UDFs → `pig_udfs.py` library (e.g., `stringsUDFs.NULLSTR()` → `PU.null_str()`, `datesUDFs.PIGDATE()` → `PU.pig_date()`)
   - **Table dependency resolution**: S3 paths → Hive table names via `enhance_table_dependencies`
   - **Deprecated operator conversion**: `FOREACH...GENERATE` → DataFrame select/withColumn, `FILTER` → `.filter()`, `GROUP BY` → `.groupBy()`, `COGROUP` → multi-DataFrame join, `SPLIT` → conditional filters, `FLATTEN` → `.explode()`
   - **S3 path scheme migration**: `s3n://` → `s3://`

4. **Apply iterative fixes** (run-fail-fix loop, max 5 iterations): Call `apply_conversion_fixes_tool` for each file that fails compilation or validation. The tool compares converted code against the original Pig source and applies LLM-generated corrections.

5. **Generate tests**: Call `generate_enhanced_pyspark_test` for each converted class. Tests use `pytest` with `PytestSparkHelper` for Spark session management.

6. **Generate replacement DAG**: Call `airflow_dag_generator_tool` (for regular DAGs) or `generate_dynamic_dag_replacement` (for dynamic DAGs) to produce Airflow DAG files that use `SparkLivyBatchOperator` instead of Pig SSH tasks.

7. **Generate Hive migrations**: Call `hive_migration_gen` to produce DDL for any final tables created by the converted PySpark code.

8. **Generate validation notebook**: Call `zeppelin_generator` to produce a Zeppelin notebook that compares production table outputs with user-schema outputs using DataComPy (schema comparison, record count, row-by-row analysis, data quality metrics).

**When the PigToSparkConversion MCP server is NOT connected**, apply conversion manually:

1. **Inventory Pig assets**: List all `.pig` files referenced in EMR steps or Airflow DAGs.

2. **Convert each Pig script to PySpark** applying these transformations:

   a. **LOAD statements** → `spark.table("database.table_name")` or `spark.read.parquet("s3://...")`

   b. **STORE statements** → `spark_utils.save_table(df, schema, table_name)`

   c. **FOREACH...GENERATE** → `.select()` / `.withColumn()` chains

   d. **FILTER** → `.filter(condition)` or `.where(condition)`

   e. **GROUP BY / COGROUP** → `.groupBy().agg()` or multi-DataFrame `.join()`

   f. **JOIN** (all types):
   - `JOIN a BY x, b BY y` → `a.join(b, a.x == b.y, "inner")`
   - `LEFT/RIGHT/FULL OUTER` → corresponding join type string
   - `REPLICATED JOIN` → `.join(broadcast(b), ...)`

   g. **SPLIT** → multiple filtered DataFrames from same source

   h. **FLATTEN** → `.explode()` for bags, `.select("struct.*")` for tuples

   i. **DISTINCT** → `.distinct()` or `.dropDuplicates()`

   j. **ORDER BY** → `.orderBy()`

   k. **LIMIT** → `.limit(n)`

   l. **UDF conversion** (common Pig UDFs → PySpark equivalents):
   - `TOKENIZE` → `F.split(col, regex)`
   - `TRIM/LTRIM/RTRIM` → `F.trim()/F.ltrim()/F.rtrim()`
   - `REPLACE` → `F.regexp_replace()`
   - `CONCAT` → `F.concat()`
   - `SIZE` → `F.size()` (bags/maps) or `F.length()` (strings)
   - `ToString(date, format)` → `F.date_format(col, format)`
   - `ToDate(str, format)` → `F.to_date(col, format)`
   - Custom registered UDFs → Python UDFs via `@F.udf`

   m. **Schemas/types**: `chararray` → `StringType`, `int` → `IntegerType`, `long` → `LongType`, `float` → `FloatType`, `double` → `DoubleType`, `bytearray` → `BinaryType`, `bag` → `ArrayType`, `tuple` → `StructType`, `map` → `MapType`

   n. **Macros** (`IMPORT`/`DEFINE` with macro files) → Python functions or shared modules

   o. **Parameters** (`$param` / `%default`) → Spark config or function arguments

3. **Validate** each converted PySpark job on test cluster:
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-pig-to-spark,ActionOnFailure=CONTINUE,\
  Jar=command-runner.jar,Args=[spark-submit,--deploy-mode,cluster,\
  s3://bucket/converted/domain/data_store/script_name.py]
```

4. On failure: classify against `references/failure-catalogue.md` (PIG_UDF_UNMAPPED, PIG_SCHEMA_MISMATCH, PIG_COGROUP_COMPLEX, PIG_NESTED_FOREACH, SPARK_DEPENDENCY_CONFLICT), apply fix, resubmit.

**PigToSparkConversion MCP server setup (prerequisite — user must complete before using this skill):**

The user needs the `pig-to-spark-conversion` MCP server connected to their agent. The server is available at `code.amazon.com/packages/PigToSparkConversion`.

Summary:
1. Clone the package and install dependencies (`npm install`)
2. Configure environment variables: `PIG_TO_SPARK_DATA_LAKE`, `PIG_TO_SPARK_S3_BASE_PATH`, `PIG_TO_SPARK_ZEPPELIN_URL`
3. Add the MCP server config to their agent:
```json
{
  "mcpServers": {
    "pig-to-spark-conversion": {
      "type": "stdio",
      "command": "node",
      "args": ["<path-to-PigToSparkConversion>/dist/index.js"],
      "timeout": 1800000
    }
  }
}
```

Once connected, this skill will automatically detect and use the PigToSparkConversion tools.

**Key limitations:**
- Private artifact repository dependencies must be upgraded manually
- Custom Pig UDFs written in Java require manual PySpark UDF re-implementation
- Pig scripts using streaming (STREAM operator) need manual conversion to Spark Structured Streaming or subprocess calls
- Nested FOREACH with complex bag operations may require manual review after automated conversion
- Hive metastore connectivity required for accurate table name resolution (otherwise S3 paths are used directly)

#### Stage 3G — Zeppelin Notebook Migration

Zeppelin notebooks on EMR 5.x may use deprecated interpreters, Spark 2.x APIs, Python 2 syntax, and Pig/Hive interpreters that behave differently on EMR 7.x. This stage adapts notebooks for compatibility.

1. **Inventory Zeppelin notebooks**: Export all notebooks from the source cluster's Zeppelin instance (typically at port 8890):
```bash
# Export all notebooks via Zeppelin REST API
curl -s http://<EMR_MASTER>:8890/api/notebook | jq -r '.body[].id' | while read id; do
  curl -s "http://<EMR_MASTER>:8890/api/notebook/$id" > "notebook_${id}.json"
done
```
   Or retrieve from S3 if Zeppelin notebook storage is configured with `zeppelin.notebook.s3.bucket`.

2. **Classify each notebook paragraph by interpreter**:
   - `%spark` / `%pyspark` / `%spark.pyspark` → Spark interpreter (needs API upgrades)
   - `%pig` → Pig interpreter (**removed in EMR 7.x** — must convert to `%pyspark`)
   - `%hive` / `%jdbc(hive)` → Hive interpreter (needs Hive 3.1 syntax fixes)
   - `%sql` / `%spark.sql` → Spark SQL (needs Spark 3.5 syntax updates)
   - `%sh` → Shell interpreter (**fully removed in EMR 7.x** — no JAR, no directory, cannot re-enable; must convert to `%python` with `subprocess`)
   - `%md` / `%angular` → Markdown/display (no changes needed)

3. **Apply Spark API upgrades** to `%spark` / `%pyspark` paragraphs:
   - `SQLContext(sc)` → `SparkSession.builder.getOrCreate()`
   - `sqlContext.sql(...)` → `spark.sql(...)`
   - `df.registerTempTable(...)` → `df.createOrReplaceTempView(...)`
   - `from pyspark.mllib` → `from pyspark.ml` (MLlib DataFrame API)
   - Python 2 syntax → Python 3 (`print "x"` → `print("x")`, `except E, e` → `except E as e`)
   - `sc.textFile("s3n://...")` → `sc.textFile("s3://...")`
   - Deprecated Spark configs → EMR 7.x equivalents

4. **Convert `%pig` paragraphs to `%pyspark`**:

   For each `%pig` paragraph, apply the Pig-to-PySpark conversion rules from Stage 3F:
   - Change interpreter directive from `%pig` to `%pyspark`
   - Convert Pig Latin statements to equivalent PySpark DataFrame operations
   - Replace Pig `DUMP` with `df.show()` or `display(df)` for notebook interactivity
   - Replace Pig `DESCRIBE` with `df.printSchema()`
   - Replace Pig `ILLUSTRATE` with `df.show(5, truncate=False)`
   - Maintain cell execution order and variable dependencies between paragraphs
   - If the PigToSparkConversion MCP server is connected, use `pig_ast_parser_tool` for complex multi-line Pig blocks

5. **Apply Hive 3.1 fixes** to `%hive` paragraphs (same rules as Stage 3B):
   - ACID/transactional table handling
   - Reserved keyword quoting
   - Type safety casts
   - Remove invalid SET properties

6. **Convert `%sh` paragraphs** — The `%sh` shell interpreter is **completely removed** from Zeppelin 0.11.1 (EMR 7.5+). It is not disabled — there is no JAR, no directory (`/usr/lib/zeppelin/interpreter/sh/` does not exist), and no registration in `interpreter.json`. It **cannot be re-enabled** via interpreter settings, bootstrap actions, or API. Convert `%sh` paragraphs as follows:

   - **Option A (recommended)**: Convert to `%python` using `subprocess`:
     ```python
     %python
     import subprocess
     result = subprocess.run(['dnf', 'list', 'installed'], capture_output=True, text=True)
     print(result.stdout)
     ```
   - **Option B**: Convert to `%pyspark` using `os.system()` or `subprocess` (if Spark context is needed)
   - Apply AL2023 compatibility to the commands themselves:
     - `yum` → `dnf`
     - `service X start` → `systemctl start X`
     - `python` / `python2` → `python3`
     - `/usr/bin/python` → `/usr/bin/python3`
     - Java path references (`/usr/lib/jvm/java-1.8.0`) → EMR 7.x Java 17 paths
     - IMDSv1 metadata calls → IMDSv2 (TOKEN request + header): `curl -s http://169.254.169.254/...` will return HTTP 401; must use `TOKEN=$(curl -X PUT ... -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")` then `curl -H "X-aws-ec2-metadata-token: $TOKEN" ...`

7. **Update Zeppelin configuration** in the notebook metadata:
   - Remove Pig interpreter binding
   - Update Spark interpreter settings for Spark 3.5
   - Update `spark.master` if hardcoded (should be `yarn`)
   - Set `PYSPARK_PYTHON=/usr/bin/python3`
   - Update any `spark.jars` or `spark.packages` references for EMR 7.x versions

8. **Upload adapted notebooks** to target Zeppelin:
```bash
# Upload via Zeppelin REST API
curl -X POST http://<EMR7_MASTER>:8890/api/notebook/import \
  -H "Content-Type: application/json" \
  -d @notebook_adapted.json
```
   Or place in the configured S3 notebook storage path for the EMR 7.x cluster.

9. **Validate** by executing key paragraphs on the test cluster's Zeppelin instance and checking for errors.

10. On failure: classify against `references/failure-catalogue.md` (ZEPPELIN_PIG_INTERPRETER_REMOVED, ZEPPELIN_SPARK_API_DEPRECATED, ZEPPELIN_PYTHON2_SYNTAX, ZEPPELIN_HIVE3_INCOMPATIBLE), apply fix, re-upload.

### Stage 4 — Launch Test Cluster (skip if DRY_RUN or VALIDATE_ONLY)

> **VALIDATE_ONLY mode**: If `VALIDATE_ONLY=true` and an existing target cluster ID is provided, skip this stage entirely and proceed to Stage 5 with the provided cluster.

```bash
aws emr create-cluster \
  --release-label $TARGET_RELEASE \
  --applications <adapted-app-list> \
  --instance-groups <adapted-instance-config> \
  --configurations <adapted-config-json> \
  --bootstrap-actions <adapted-bootstrap-list> \
  --tags emr-migration-skill=test-run \
  --auto-termination-policy '{"IdleTimeout":900}' \
  --region $REGION
```

> **Why IdleTimeout instead of --auto-terminate?** With `--auto-terminate`, the cluster shuts down after the last submitted step completes. This conflicts with the Stage 5/6 fix loop — if a validation step fails (with `ActionOnFailure=CONTINUE`), the cluster would terminate before the agent can diagnose and resubmit. The 15-minute idle timeout keeps the cluster alive long enough for log retrieval, diagnosis, and resubmission, while still auto-terminating if the agent crashes or abandons the workflow. The skill explicitly terminates the cluster in Stage 7 after all work is complete.

Use **minimum viable size**: 1 primary (m5.xlarge) + 1 core (m5.xlarge).

Poll until WAITING or terminal state:
```bash
aws emr describe-cluster --cluster-id $NEW_CLUSTER_ID --query 'Cluster.Status.State'
```

On bootstrap failure or provisioning timeout: fetch logs, classify against `references/failure-catalogue.md`, apply fix, relaunch.

**Auto-retry in different AZ** (when `RETRY_AZ=true`, default):
If the cluster fails with `APP_PROVISIONING_FAILED_TIME_OUT`, `BOOTSTRAP_FAILURE`, or `INTERNAL_ERROR` and the error appears to be infrastructure-related (not a user configuration issue):
1. List available subnets in the VPC across different AZs
2. Select a subnet in a different AZ from the failed attempt
3. Relaunch the cluster with the new subnet
4. If the second attempt also fails with the same error: halt and report (do NOT retry more than once)
5. If the failure is `EMR7_RPM_REPO_MISSING`: try a newer EMR release label instead of a different AZ

**Common launch failures for newly-migrated clusters:**

| Symptom | Likely Cause | Quick Fix |
|---------|-------------|-----------|
| `APP_PROVISIONING_FAILED_TIME_OUT` | S3 downloads routed through proxy instead of VPC endpoint | Add explicit `s3.<region>.amazonaws.com` to NO_PROXY (Java `*` only matches single DNS label) |
| Cluster never reaches WAITING, EC2 instances running | Instance Controller can't reach EMR control plane | Verify security group egress and NAT/VPC endpoints for `elasticmapreduce.<region>.amazonaws.com` |
| `BOOTSTRAP_FAILURE` with yum/dnf errors | Bootstrap action uses AL1 package manager commands | Replace `yum` → `dnf`, remove `amazon-linux-extras` calls |
| `BOOTSTRAP_FAILURE` with RPM missing | EMR 7.x RPM repo incomplete for release/region | Upgrade to newer EMR 7.x release label (see EMR7_RPM_REPO_MISSING) |
| Private subnet cluster stuck in STARTING | No route to S3 or EMR endpoints | Add S3 Gateway VPC Endpoint + interface endpoints for STS/CloudWatch, verify route table |
| `NoSuchMethodError` in `scala.*` before app code runs | Scala 2.11 uber JAR on extraClassPath | Move JAR to `--jars` flag or recompile for Scala 2.12 (see SPARK_CLASSPATH_POISON) |

See `references/failure-catalogue.md` categories: APP_PROVISIONING_TIMEOUT, NO_PROXY_REGIONAL_S3_MISMATCH, PRIVATE_SUBNET_CONNECTIVITY, EMR7_RPM_REPO_MISSING, SPARK_CLASSPATH_POISON.

### Stage 5 — Validate Workloads

Execute validation per detected application type. Load `references/failure-catalogue.md` for failure classification.

**Spark**: Submit smallest representative step from original history (or upgraded application from Stage 3A).
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps <spark-step-json> --region $REGION
```

**Hive**: Execute adapted DDL + queries from Stage 3B.
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps Type=HIVE,Args=[-f,s3://bucket/test-query-hive3-migrated.hql] --region $REGION
```

**Presto/Trino**: Execute adapted queries from Stage 3C via Trino CLI.
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-trino,ActionOnFailure=CONTINUE,Jar=command-runner.jar,\
  Args=[trino-cli,--catalog,hive,--execute,"<adapted-query>"] --region $REGION
```

**MapReduce**: Submit adapted MR JARs from Stage 3D.
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-mr,ActionOnFailure=CONTINUE,\
  Jar=s3://bucket/path/job-hadoop3-migrated.jar,Args=[<input>,<output>] --region $REGION
```

**Flink**: Submit adapted Flink application from Stage 3E.
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-flink,ActionOnFailure=CONTINUE,Jar=command-runner.jar,\
  Args=[flink,run-application,-t,yarn-application,s3://bucket/path/flink-app-flink18-migrated.jar] --region $REGION
```

**Pig (converted to PySpark)**: Submit converted PySpark scripts from Stage 3F.
```bash
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-pig-to-spark,ActionOnFailure=CONTINUE,\
  Jar=command-runner.jar,Args=[spark-submit,--deploy-mode,cluster,\
  s3://bucket/converted/$PIG_DOMAIN_NAME/data_store/script_name.py] --region $REGION
```
Compare output tables between original Pig results and converted PySpark results using DataComPy (schema match, record count, row-by-row). If the PigToSparkConversion MCP `zeppelin_generator` tool was used, run the generated validation notebook.

**Zeppelin notebooks**: Execute adapted notebooks on the test cluster's Zeppelin instance (port 8890).
```bash
# Trigger paragraph execution via Zeppelin API
curl -X POST "http://<EMR7_MASTER>:8890/api/notebook/job/$NOTEBOOK_ID"
# Check status
curl -s "http://<EMR7_MASTER>:8890/api/notebook/job/$NOTEBOOK_ID" | jq '.body[].status'
```
Verify: no ERROR status paragraphs, `%pyspark` cells produce expected output, former `%pig` cells now run as PySpark successfully.

**S3A Committer Validation** (for EMR 7.10+ targets):
When the target cluster uses EMR 7.10+, validate that S3A Magic Committer is working correctly:
```bash
# Run a small write job and verify output integrity
aws emr add-steps --cluster-id $NEW_CLUSTER_ID --steps \
  Type=CUSTOM_JAR,Name=validate-s3a-committer,ActionOnFailure=CONTINUE,\
  Jar=command-runner.jar,Args=[spark-submit,--deploy-mode,cluster,\
  --conf,spark.hadoop.fs.s3a.committer.name=magic,\
  --conf,spark.hadoop.fs.s3a.committer.magic.enabled=true,\
  --conf,spark.hadoop.fs.s3a.fast.upload=true,\
  s3://bucket/validation/s3a_committer_test.py] --region $REGION
```
Verify: output file count matches expected (no partial commits or duplicates), no `FileAlreadyExistsException` in logs.

**Data Validation** (recommended for production migrations):
When source cluster is still running, perform data comparison:
- **Row counts**: Compare `SELECT COUNT(*)` between source and target for key tables
- **Partition counts**: Verify `SHOW PARTITIONS` match between source and target
- **Aggregate comparison**: Run `GROUP BY` aggregations on key columns and compare results
- **Sample row comparison**: Compare a random sample of rows (e.g., `ORDER BY key LIMIT 100`) between source and target
- **HBase**: Run `scan` with row count and compare column families between source and target
- If source is terminated: validate against known expected outputs (golden files) or upstream data

On failure: fetch logs, classify against `references/failure-catalogue.md`, apply fix, resubmit.

### Stage 6 — Fix Loop (max 5 iterations)

For each failure:
1. Fetch logs: `aws logs filter-log-events --log-group-name /aws-emr/...`
2. Classify against `references/failure-catalogue.md`
3. Apply fix (config change → terminate + relaunch; script/step fix → resubmit)
4. Increment counter
5. **Halt if**: same failure recurs (cycle), budget exhausted, or unmappable failure

### Stage 7 — Report Results

**On success**:
- Output final EMR 7.x RunJobFlow configuration as JSON
- List all cluster-level fixes applied with explanations
- List all application-level fixes (Spark code changes, Hive script adaptations, Pig→PySpark conversions, Zeppelin notebook adaptations)
- Provide adapted bootstrap script and application S3 locations
- List converted Pig domains with their output structure and validation notebook URLs
- Terminate test cluster: `aws emr terminate-clusters --cluster-ids $NEW_CLUSTER_ID --region $REGION`

**On failure/halt**:
- Terminate test cluster: `aws emr terminate-clusters --cluster-ids $NEW_CLUSTER_ID --region $REGION`
- Report: fixes attempted, remaining blockers, manual remediation for each blocker
- Output partial configuration (what was successfully adapted)

## Halt Conditions

| Trigger | Action |
|---------|--------|
| MapR filesystem detected | Immediate halt — not supported |
| Oozie with no Spark/Hive equivalent | Halt with conversion guidance (see `references/removed-applications.md`) |
| Pig with STREAM operator or custom Java UDFs | Flag for manual conversion; continue with other scripts |
| Pig conversion produces >20% data mismatch in validation | Halt Pig domain, report discrepancies for manual review |
| Zeppelin notebook with unsupported custom interpreter | Skip notebook, report which interpreters need manual setup |
| 5 fixes exhausted | Report remaining blockers |
| Same failure repeats after fix | Report cycle detected |
| >2 cluster launch failures | Report infra issue |
| Instance type unavailable | Suggest alternatives, pause for user input |
| Spark Upgrade Agent reports unresolvable error | Report with manual remediation steps |
| PigToSparkConversion MCP reports unresolvable error | Report with manual conversion steps and Pig source reference |

## Safety Guarantees

1. Original cluster is **never modified**
2. Original application code is **never overwritten** — all migrated artifacts are written to new locations (see naming conventions below)
3. Original Pig scripts are preserved; converted PySpark is written to new paths with `-spark-migrated` or domain-structured output
4. Original Zeppelin notebooks are exported and preserved; adapted versions are uploaded as new notebooks (with `-emr7` suffix in notebook name)
5. Test cluster tagged `emr-migration-skill=test-run` for cost tracking
6. Test cluster auto-terminates after 15 minutes idle (or explicitly in Stage 7)
7. Validation steps are read-only or use test data
8. Minimum instance count (1 primary + 1 core) to limit cost

### Migrated Artifact Naming Conventions

All migrated scripts and JARs are written to **new S3 locations** — original files are never modified or replaced. The naming convention per application type:

| Application | Original Location | Migrated Location |
|-------------|------------------|-------------------|
| Bootstrap scripts | `s3://bucket/path/script.sh` | `s3://bucket/path/script-emr7-migrated.sh` |
| Hive scripts | `s3://bucket/path/query.hql` | `s3://bucket/path/query-hive3-migrated.hql` |
| Presto/Trino scripts | `s3://bucket/path/query.sql` | `s3://bucket/path/query-trino-migrated.sql` |
| MapReduce JARs | `s3://bucket/path/job.jar` | `s3://bucket/path/job-hadoop3-migrated.jar` |
| MapReduce streaming scripts | `s3://bucket/path/mapper.py` | `s3://bucket/path/mapper-hadoop3-migrated.py` |
| Flink JARs | `s3://bucket/path/flink-app.jar` | `s3://bucket/path/flink-app-flink18-migrated.jar` |
| Flink config | `s3://bucket/path/flink-conf.yaml` | `s3://bucket/path/flink-conf-flink18-migrated.yaml` |
| Spark application code | `local-path/src/` | `local-path/src-emr7-migrated/` (full copy with upgrades applied) |
| Pig → PySpark | `s3://bucket/pig/script.pig` | `s3://bucket/converted/$DOMAIN/data_store/script_name.py` |
| Zeppelin notebooks | `notebook_{id}.json` | `notebook_{id}_emr7_migrated.json` |

The original S3 objects and local files remain untouched. If a migration is re-run, the `-migrated` artifacts are overwritten (idempotent), but originals are never affected.

## Security Considerations

- All AWS API calls use the user's existing credentials; no credentials are stored or hardcoded
- Bootstrap scripts MUST NOT contain secrets — use AWS Secrets Manager or SSM Parameter Store
- Test clusters inherit the source cluster's security configuration (encryption, Kerberos, Lake Formation) unless explicitly overridden
- IMDSv2 is enforced on EMR 7.x test clusters by default
- Security group rules from source are validated but not modified; overly permissive rules (0.0.0.0/0) are flagged as warnings
- IAM roles follow least-privilege: test cluster uses the same instance profile as source; no additional permissions added
- TLS 1.2+ is enforced on AL2023 — connections to legacy endpoints using TLS 1.0/1.1 will fail and are flagged
- Spark Upgrade Agent uses cross-region inference; see AWS documentation on data processing regions
- PigToSparkConversion MCP server runs locally; Pig scripts and converted code stay on the user's machine unless explicitly uploaded to S3
- Zeppelin notebook exports may contain query results or sensitive data — review before storing in shared locations

## Reference Files

- `references/failure-catalogue.md` — 30+ failure categories with identification criteria and fixes (includes PIG_*, ZEPPELIN_* categories)
- `references/configuration-transforms.md` — detailed property mappings and adaptation rules
- `references/iam-permissions.md` — required IAM permissions for this skill
- `references/removed-applications.md` — migration paths for Oozie (Pig now handled by Stage 3F)
- `references/pig-to-spark-mapping.md` — complete Pig Latin operator → PySpark DataFrame API mapping reference
- `references/zeppelin-interpreter-migration.md` — interpreter compatibility matrix and paragraph conversion rules

## Tested Configurations (Field Feedback)

The following configurations have been tested by field SAs. Results inform the failure catalogue and skill behavior.

| Source EMR | Applications | Migration Result | Fix Iterations | Key Findings |
|-----------|-------------|-----------------|----------------|--------------|
| emr-5.33.0 | Spark, Hive, Hadoop (empty) | Success | 0 | Clean activation; git clone over SSH/HTTPS may fail on gitlab.aws.dev (use ZIP download) |
| emr-5.33.0 | Spark, Hive, Pig, MapReduce | Success | 0 | All 4 workloads migrated with zero manual effort; Pig→PySpark ORDER BY/JOIN correct |
| emr-5.35.0 | Spark, Hive, Pig, HBase, Oozie | Success | 2 | Scala 2.11 JARs need recompilation; Pig/Oozie flagged for manual redesign; new category HIVE3_CTAS_EXTERNAL found |
| emr-5.34.0 | Spark, Hive, Pig, HBase, Oozie | Success | 3 | New category SPARK_CLASSPATH_POISON found (Scala 2.11 fat JAR on extraClassPath corrupts SparkSubmit) |
| emr-5.33.0 | Spark, Hive, Pig, HBase, Oozie | Success | 2 | HBase 2.5 migration seamless; Hive ACID/reserved-keyword fixes correct |
| emr-5.35.0 | Spark, Hive, Pig, HBase, Oozie | Success | 2 | EMRFS→S3A committer config partially handled; new category EMRFS_TO_S3A_COMMITTER added |
| emr-5.33.0 | Presto (+Spark, Hive, Hadoop) | Success | 0 | Clean presto-cli to trino-cli rename; json_extract_scalar retained for VARCHAR |
| emr-5.33.0 | Flink (+Hadoop) | Partial | 3 | EMR platform-side failure: emr-7.1.0 delta/hudi RPMs missing; new category EMR7_RPM_REPO_MISSING added |
| emr-5.33.0 | Zeppelin (Spark, Pig, Shell) | Success | 0 | %sh→%python subprocess, %pig→%pyspark, Spark API deprecated patterns fixed; JSON-level validation only |

### Known Blockers Requiring Manual Intervention

| Blocker | Manual Step Required | Workaround |
|---------|---------------------|------------|
| Scala 2.11 JARs | Recompile all application JARs to Scala 2.12 | Use `userClassPathFirst=true` as temporary workaround |
| Pig scripts | Convert to PySpark (automated via PigToSparkConversion MCP) | Use Stage 3F for automated conversion |
| Oozie workflows | Redesign to Step Functions or MWAA | No automated conversion available |
| Scala 2.11 uber JAR on extraClassPath | Rebuild without bundled Scala runtime, or move to `--jars` | See SPARK_CLASSPATH_POISON in failure catalogue |

### Improvement Backlog (from field testing)

- [ ] Add `--validate-only` mode for running validation without migration steps ← **DONE (v2)**
- [ ] Auto-detect if source cluster is still running ← **DONE (v2)**
- [ ] Document ZIP-download alternative for gitlab.aws.dev clone ← **DONE (v2)**
- [ ] Auto-retry in different AZ for EMR internal errors ← **DONE (v2)**
- [ ] Add S3A committer validation to test suite ← **DONE (v2)**
- [ ] Add EMR7_RPM_REPO_MISSING to failure catalogue ← **DONE (v2)**
- [ ] Add SPARK_CLASSPATH_POISON to failure catalogue ← **DONE (v2)**
- [ ] Add HIVE3_CTAS_EXTERNAL to failure catalogue ← **DONE (v2)**
- [ ] Add EMRFS_TO_S3A_COMMITTER to failure catalogue ← **DONE (v2)**
- [ ] Optional live-execution validation for Zeppelin when target cluster available
- [ ] Handle `ada --region` flag incompatibility (use environment variable instead)
