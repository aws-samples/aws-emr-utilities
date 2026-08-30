---
name: emr-al1-migration-skill
description: >
  Migrate EMR clusters and applications from EMR 5.0–5.35 (Amazon Linux 1) to EMR 7.x (Amazon Linux 2023).
  Use when a customer needs to upgrade EMR clusters off AL1 as part of the AL1 deprecation,
  upgrade Spark application code (Python/Scala/Java) using the SageMaker Unified Studio Spark Upgrade Agent MCP server,
  migrate Hive 2.3→3.1 scripts, convert Presto queries to Trino, adapt MapReduce jobs for Hadoop 3,
  update Flink deployment mode and memory model, convert Pig scripts to PySpark,
  migrate Zeppelin notebooks for EMR 7.x compatibility, adapt bootstrap actions for AL2023 compatibility,
  resolve Java 8-to-17 issues, or troubleshoot Hadoop 2-to-3 breaking changes on EMR.
  Covers EC2-based EMR clusters only (not EMR on EKS or EMR Serverless).
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

**Important version boundaries:**
- EMR 5.0–5.35 → Amazon Linux 1 (AL1) — **this skill's scope**
- EMR 5.36–5.37 → Amazon Linux 2 (AL2) — different migration path (no AL1 bootstrap issues)
- EMR 6.x → Amazon Linux 2 (AL2) — different migration path
- EMR 7.x → Amazon Linux 2023 (AL2023) — target

## Scope

**Supported application migrations**: Spark, Hive, Presto→Trino, MapReduce, Flink, Pig→PySpark, Zeppelin, Bootstrap actions

**Special cases**:
- Pig: broken on Java 17 (ORDER BY/JOIN crash) — must convert to PySpark, no fix possible
- Oozie: removed from EMR 7.x — redesign to Step Functions or MWAA
- Ganglia/Mahout/Sqoop: removed from EMR 7.5+ — inform user, no migration needed

**Strategy**: Gather → adapt cluster config → upgrade applications → launch test cluster → validate → fix loop (≤5) → report

## Parameters

Only one parameter is required from the user. Everything else is auto-discovered or uses sensible defaults.

**Required** (user must provide):

| Parameter | Description |
|-----------|-------------|
| `CLUSTER_ID` | Source EMR cluster ID (`j-XXXXXXXXXXXXX`) |

**Auto-discovered** (skill determines these — no user input needed):

| Parameter | How the skill infers it |
|-----------|------------------------|
| `REGION` | Uses `aws configure get region` or `AWS_DEFAULT_REGION`. If user provides it, use that. If describe-cluster fails, ask user. |
| `AWS_PROFILE` | Uses current active credentials (`aws sts get-caller-identity`). If it fails, ask user. |
| `TARGET_RELEASE` | Defaults to `emr-7.1.0`. Override only if user specifies a different EMR 7.x release. |
| `STAGING_BUCKET` | Creates `s3://emr-migration-staging-{account_id}-{region}` automatically. Uses cluster's log bucket if available. |
| `DRY_RUN` | Default: `false`. Set to `true` only if user explicitly asks for config-only output. |
| `VALIDATE_ONLY` | Default: `false`. Set to `true` only if user provides an existing target cluster and asks to skip migration steps. |
| `RETRY_AZ` | Default: `true`. Automatically retries in a different AZ on infra failures. |

**Conditionally required** (only ask if the workload is detected):

| Parameter | When to ask |
|-----------|-------------|
| `SPARK_APP_PATH` | Only if user wants Spark application source code upgraded (not just cluster config). Ask: "Do you have a local Spark project to upgrade?" |
| `PIG_SCRIPT_PATH` | Only if Pig detected. Ask: "Where are your Pig script files? (local path or S3 path)" |
| `ZEPPELIN_NOTEBOOKS_PATH` | Only if Zeppelin detected. Try to export automatically via Zeppelin REST API first; ask only if API is unreachable. |

## Prerequisites

The user MUST have:
- AWS CLI v2 configured with credentials that have the permissions listed in `references/iam-permissions.md`
- A VPC/subnet where EMR clusters can launch
- An S3 bucket for adapted scripts (or reuse the cluster's existing log bucket)

**Source cluster state detection**: Before beginning migration, auto-detect whether the source cluster is still running (`WAITING`/`RUNNING`) or terminated. If terminated:
- Skip live cluster inspection steps
- Use the cluster's saved configuration from `describe-cluster`
- Warn the user that data validation against source is not possible

For Spark application upgrades (optional):
- The SageMaker Unified Studio Spark Upgrade Agent MCP server configured (see `references/spark-upgrade-agent-guide.md`)
- Python 3.9+ installed locally (for pyupgrade)

For Pig script conversion (optional):
- Pig `.pig` files accessible (in S3, local path, or referenced in EMR step definitions)

For Zeppelin notebook migration (optional):
- Source cluster Zeppelin accessible (port 8890) for auto-export, OR pre-exported notebook JSON files

## Reference Loading Map

Load reference files ONLY when the corresponding workload is detected:

| Workload/Situation | Load |
|---|---|
| Always (config adaptation) | `references/configuration-transforms.md` |
| Spark 2.4+ code upgrade | `references/spark-upgrade-agent-guide.md` |
| Pig scripts detected | `references/pig-to-spark-mapping.md` |
| Zeppelin notebooks detected | `references/zeppelin-interpreter-migration.md` |
| Oozie detected | `references/failures/infrastructure.md` (OOZIE_REMOVED) |
| Validation failure (any) | `references/failures/<domain>.md` matching the failure category |
| IAM permission errors | `references/iam-permissions.md` |

## Tools

- **SQLGlot**: `sqlglot.transpile(sql, read="presto", write="trino")` for Presto→Trino
- **pyupgrade**: `pyupgrade --py3-plus <file>` (fix `print`/`except` syntax manually first)
- **Spark Upgrade Agent MCP** (`spark-upgrade:*` tools): For Spark 2.4+ code upgrades

## Validation Rule — MANDATORY for ALL components

**No migrated artifact is presented to the customer until it has been executed on the target cluster and validated.**

| Component | Validation before confirming to customer |
|-----------|----------------------------------------|
| **Spark 3.0+** | Spark Upgrade Agent runs it on target + DQ comparison passes |
| **Spark 2.4** | Spark Upgrade Agent runs it on target, exit 0 |
| **Spark 2.0–2.2** | Submit to target cluster as EMR step, exit 0 |
| **Hive** | Submit migrated HQL to target as Hive step, exit 0 + `DESCRIBE` + `COUNT(*)` on output tables |
| **Presto→Trino** | Submit migrated SQL via `trino-cli` on target, exit 0 |
| **Pig→PySpark** | Submit converted PySpark to target, exit 0 + compare output where it naturally lands |
| **Flink** | Submit migrated JAR to target in application mode, exit 0 |
| **MapReduce** | Submit migrated JAR/script to target, exit 0 |
| **Bootstrap** | Target cluster reaches WAITING state |
| **Zeppelin** | Execute paragraphs on target Zeppelin via REST API, no ERROR status |

> **Bootstrap testing note**: Real bootstrap actions run as `root` during cluster creation. EMR steps run as `hadoop`. The correct validation for bootstrap scripts is that the **target cluster reaches WAITING state** — not submitting the script as a step.

If validation fails → enter fix loop (or Spark Upgrade Agent iteration) → re-validate → only confirm after pass.

---

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
7. Check for **warnings** (continue but inform user):
   - Ganglia → removed from EMR 7.5+; recommend CloudWatch Container Insights or Prometheus as alternative
   - Mahout → removed; recommend Spark MLlib
   - Sqoop → removed from EMR 7.5+; recommend Spark JDBC or AWS Glue
8. Store the full original configuration as a JSON backup artifact in S3.

### Stage 2 — Adapt Cluster Configuration

Load `references/configuration-transforms.md` and apply all transformations in order:

1. **Release label**: set to `TARGET_RELEASE`
2. **Application versions**: map to EMR 7.x equivalents (Spark 2.4→3.5, Hive 2.3→3.1, Presto→Trino, HBase 1.4→2.5, Hadoop 2.10→3.3)
3. **Configuration properties**: remove deprecated, rename changed, add required new properties
4. **Bootstrap actions**: adapt for AL2023 (yum→dnf, service→systemctl, python→python3, Java paths, IMDSv1→IMDSv2)
   - **IMDSv2 is enforced on EMR 7.5** — any `curl http://169.254.169.254/...` calls (IMDSv1) will return HTTP 401. Replace with token-based IMDSv2 (see `references/configuration-transforms.md` section 4.6)
   - Scan ALL bootstrap scripts for patterns: `curl.*169.254.169.254`, `wget.*169.254.169.254`, `ec2-metadata` (deprecated CLI)
5. **Step definitions**: update spark-submit args, Hive DDL, Presto→Trino connection strings (details in `references/configuration-transforms.md` section 3)
6. **Security**: verify Kerberos config, Lake Formation integration, security groups
7. **Instance types**: validate availability; suggest current-gen replacements if deprecated
8. **Java 8→17, Log4j, EMRFS**: Apply remaining transforms from `references/configuration-transforms.md` (package renames, Log4j2 format, EMRFS Consistent View removal on all 7.x, full EMRFS removal on 7.10+ only)

Upload adapted bootstrap scripts to S3 with `-emr7-migrated` suffix.

### Stage 3 — Upgrade Applications

> **Backward Compatibility Note**: EMR 7.5 has strong backward compatibility — `registerTempTable()`, `unionAll()`, `SQLContext`, implicit type casting, and basic Hive DDL still work without modification. The skill still applies all modernizations since the goal is a fully updated codebase, but distinguishes **critical breaks** (Pig, Presto CLI, Python 2, IMDSv1, Hive ACID ORC, Zeppelin %pig/%sh) from **future-proofing** (deprecated aliases that still work).

#### Stage 3A — Spark Application Code Upgrade (if SPARK_APP_PATH provided)

Route based on source Spark version:

**Spark 2.0–2.2 (EMR 5.0–5.19) — Static tools + agent:**

1. **Create a working copy**: Copy `SPARK_APP_PATH` to `-emr7-migrated` suffix. Original is never modified.
2. **Fix Python 2 syntax-breaking patterns first** — `print "x"` → `print("x")`, `except Exception, e:` → `except Exception as e:`. Then run `pyupgrade --py3-plus` on all .py files. Finally, apply Spark API changes referencing the [Spark SQL Migration Guide](https://spark.apache.org/docs/latest/sql-migration-guide.html).
3. **Agent applies additional fixes** referencing `references/failures/spark.md` (SPARK_SQL_LEGACY, SPARK_REMOVED_APIS, SPARK_SCALA_BINARY, SPARK_PYTHON_VERSION, SPARK_DEPENDENCY_CONFLICT).
4. **Validate**: Submit migrated code to target cluster as EMR step. Must exit 0.
5. **If validation fails**: Enter fix loop (max 5 iterations using failure catalogue), re-validate until pass.

**Spark 2.4+ (EMR 5.20–5.35) — Spark Upgrade Agent MCP:**

The Spark Upgrade Agent MCP handles all Spark code and dependency upgrades for Spark 2.4+. See `references/spark-upgrade-agent-guide.md` for the complete invocation guide (prompt template, version format requirements, post-upgrade result retrieval, two-hop model).

Key points:
- Use EMR release versions (e.g., `5.33.0`, `7.1.0`), NOT Spark versions
- Let it iterate to completion (up to 40 tool calls) — do NOT pause or interrupt
- For Spark 2.4: exit-code validation only (no data quality comparison)
- For Spark 3.0+: full data quality comparison is automatic

**When the Spark Upgrade Agent MCP server is NOT connected**, copy the `SPARK_APP_PATH` to a new directory with `-emr7-migrated` suffix, then apply fixes directly using `references/failures/spark.md`:
- Fix Python 2 syntax-breaking patterns first, then run `pyupgrade --py3-plus`
- Apply cluster-level legacy compat flags (SPARK_SQL_LEGACY, SPARK_PARQUET_TIMESTAMP)
- Rewrite deprecated APIs in source code (SPARK_REMOVED_APIS)
- Fix Scala 2.11→2.12 issues (SPARK_SCALA_BINARY)
- Resolve dependency conflicts (SPARK_DEPENDENCY_CONFLICT)

#### Stage 3B — Hive Application Migration

For clusters with Hive workloads, adapt HQL scripts and queries for Hive 3.1:

1. **Inventory Hive assets**: List all `.hql` files in S3 step definitions, scheduled queries, and DDL scripts.

2. **Apply Hive 2.3→3.1 fixes** (run-fail-fix loop per script, max 5 iterations):
   - Convert managed tables to EXTERNAL: `ALTER TABLE t SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');`
   - Quote reserved keywords with backticks: `date`, `time`, `timestamp`, `interval`, `user`, `role`
   - Add explicit `CAST()` for implicit conversions Hive 3 rejects
   - Remove `SET hive.execution.engine=mr;` and invalid properties (`hive.create.as.acid`, `hive.create.as.insert.only`, `hive.strict.managed.tables`)
   - Metastore: Glue Catalog → no action. External HMS → `schematool -upgradeSchema`
   - On failure: classify against `references/failures/hive.md`

3. **ACID Table Data Migration (Critical)** — Hive 2.x ACID delta files are incompatible with Hive 3.x. Load `references/failures/hive.md` category HIVE2_ACID_DELTA_FORMAT_INCOMPATIBLE for the full fix procedure. Key points:
   - Major compaction is NOT sufficient (validated in E2E testing)
   - Export to non-ACID EXTERNAL table is the ONLY reliable fix
   - If source cluster is terminated, launch a temporary EMR 5.x cluster for export
   - Non-ACID tables work fine without any action

4. **Upload adapted scripts** to S3 with `-hive3-migrated` suffix.

5. **Validate** each script on test cluster. Submit as EMR steps per the Validation Rule above.

6. On failure: fetch logs, classify against `references/failures/hive.md`, apply fix, resubmit.

#### Stage 3C — Presto → Trino Migration

EMR 7.x replaces Presto with Trino (complete rebrand). Use `sqlglot.transpile(sql, read="presto", write="trino")` for SQL changes. For non-SQL changes apply these renames:

| Presto | Trino |
|--------|-------|
| `presto-cli` | `trino-cli` |
| `com.facebook.presto.jdbc.PrestoDriver` | `io.trino.jdbc.TrinoDriver` |
| `jdbc:presto://host:port/catalog` | `jdbc:trino://host:port/catalog` |
| `presto-connector-*` classification | `trino-connector-*` classification |
| `presto-config` classification | `trino-config` classification |
| `connector.name=hive-hadoop2` | `connector.name=hive` |

SQL semantics: `current_timestamp` now returns `timestamp with time zone`; `json_extract` returns JSON type (use `json_extract_scalar` to get unwrapped string values). Remove `presto.` prefix from session properties.

Upload with `-trino-migrated` suffix. Validate on test cluster. On failure: classify against `references/failures/infrastructure.md` (PRESTO_TO_TRINO_RENAME, TRINO_SQL_CHANGES).

#### Stage 3D — MapReduce Application Migration

Apply Hadoop 2→3 fixes from `references/failures/hadoop-mr.md`. Key: recompile JARs for Hadoop 3.3, `s3n://`→`s3://`, `/usr/lib/hadoop/lib/`→`/usr/lib/hadoop/share/hadoop/common/lib/`, Python 3 shebangs for streaming jobs. Upload with `-hadoop3-migrated` suffix. Validate by submitting as EMR step. On failure: classify against `references/failures/hadoop-mr.md`.

**Recommended path**: Convert MR jobs to Spark for long-term supportability. MR works on EMR 7.x but receives no performance improvements.

#### Stage 3E — Flink Application Migration

Apply Flink fixes from `references/failures/flink.md`. Key: `-m yarn-cluster` → `flink run-application -t yarn-application`, `taskmanager.heap.mb` → `taskmanager.memory.process.size`, `state.backend` → `state.backend.type`. Update connector JARs to match Flink 1.18. Add `--add-opens` JVM flags if needed. Upload with `-flink18-migrated` suffix. Validate by submitting as EMR step. On failure: classify against `references/failures/flink.md`.

#### Stage 3F — Pig Application Migration (Pig → PySpark)

Pig 0.17.0 is still installable on EMR 7.x but is **functionally broken** for non-trivial scripts due to a Java 17 serialization incompatibility. Converting Pig workloads to PySpark is **required**. The agent handles this conversion directly using `references/pig-to-spark-mapping.md` as guidance.

> **CRITICAL**: Pig 0.17.0 on EMR 7.x crashes with `java.io.IOException: Deserialization error: Cannot invoke "org.apache.pig.impl.plan.OperatorKey.hashCode()"` on any operation requiring data exchange between Tez vertices. There is no fix — **the only migration path is converting to PySpark.**

**Conversion process** (agent applies using `references/pig-to-spark-mapping.md`):

1. **Inventory Pig assets**: List all `.pig` files referenced in EMR steps or Airflow DAGs.

2. **Convert each Pig script to PySpark** using `references/pig-to-spark-mapping.md` as the complete operator mapping reference. Key conversions: LOAD→spark.read, FILTER→.filter(), GROUP BY→.groupBy().agg(), JOIN→.join(), ORDER BY→.orderBy(), STORE→.write().

3. **Validate** each converted PySpark job. Submit as EMR steps per the Validation Rule above.

4. On failure: classify against `references/failures/pig.md` (PIG_UDF_UNMAPPED, PIG_SCHEMA_MISMATCH, PIG_COGROUP_COMPLEX, PIG_NESTED_FOREACH), apply fix, resubmit.

**Key limitations:**
- Custom Pig UDFs written in Java require manual PySpark UDF re-implementation
- Pig scripts using STREAM operator need manual conversion
- Nested FOREACH with complex bag operations may require manual review

#### Stage 3G — Zeppelin Notebook Migration

Zeppelin notebooks on EMR 5.x may use deprecated interpreters, Spark 2.x APIs, Python 2 syntax, and Pig/Hive interpreters that behave differently on EMR 7.x.

1. **Inventory Zeppelin notebooks**: Export all notebooks from the source cluster's Zeppelin instance (port 8890) or retrieve from S3 notebook storage.

2. **Classify each notebook paragraph by interpreter**:
   - `%spark` / `%pyspark` → Spark interpreter (needs API upgrades)
   - `%pig` → **removed in EMR 7.x** — must convert to `%pyspark`
   - `%hive` / `%jdbc(hive)` → Hive interpreter (needs Hive 3.1 syntax fixes)
   - `%sh` → **fully removed in EMR 7.x** — must convert to `%python` with `subprocess`
   - `%md` / `%angular` → no changes needed

3. **Apply interpreter-specific migrations** using `references/zeppelin-interpreter-migration.md`:
   - `%pyspark`: Spark 2.x→3.5 API upgrades + Python 2→3
   - `%pig` → `%pyspark`: Full Pig-to-PySpark conversion (Stage 3F rules)
   - `%hive`: Hive 3.1 syntax fixes (Stage 3B rules)
   - `%sh` → `%python`: Convert to subprocess calls

4. **Update Zeppelin configuration** in notebook metadata: remove Pig interpreter binding, set `PYSPARK_PYTHON=/usr/bin/python3`.

5. **Upload adapted notebooks** to target Zeppelin (REST API or S3 storage path).

6. **Validate** by executing key paragraphs on the test cluster's Zeppelin instance.

7. On failure: classify against `references/zeppelin-interpreter-migration.md` (Failure Categories section), apply fix, re-upload.

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
  --no-auto-terminate \
  --region $REGION
```

Use **minimum viable size**: 1 primary (m5.xlarge) + 1 core (m5.xlarge). The skill terminates the cluster in Stage 7 after validation completes.

Poll until WAITING or terminal state. On bootstrap failure or provisioning timeout: fetch logs, classify against `references/failures/infrastructure.md` (APP_PROVISIONING_TIMEOUT, NO_PROXY_REGIONAL_S3_MISMATCH, PRIVATE_SUBNET_CONNECTIVITY, EMR7_RPM_REPO_MISSING, SPARK_CLASSPATH_POISON). Apply fix and relaunch.

**Auto-retry in different AZ** (when `RETRY_AZ=true`, default):
If the cluster fails with infrastructure-related errors, select a subnet in a different AZ and relaunch. If the second attempt also fails: halt and report. If the failure is `EMR7_RPM_REPO_MISSING`: try a newer EMR release label instead.

### Stage 5 — Validate Workloads

Submit as EMR steps per the Validation Rule above. For each detected application type, submit the migrated artifact and verify success:
- **Spark**: Submit smallest representative step (or upgraded application from Stage 3A)
- **Hive**: Execute adapted DDL + queries from Stage 3B
- **Presto/Trino**: Execute adapted queries via `trino-cli`
- **MapReduce**: Submit adapted MR JARs from Stage 3D
- **Flink**: Submit adapted Flink application from Stage 3E
- **Pig→PySpark**: Submit converted PySpark scripts from Stage 3F
- **Zeppelin**: Execute adapted notebooks via Zeppelin REST API
- **S3A Committer** (EMR 7.10+): Verify output file counts match expected, no duplicates

**Data Validation** (recommended for production migrations when source is still running):
- Row counts, partition counts, aggregate comparison, sample row comparison between source and target
- If source is terminated: validate against known expected outputs (golden files) or upstream data

On failure: fetch logs, classify against appropriate `references/failures/<domain>.md`, apply fix, resubmit.

### Stage 6 — Fix Loop (max 5 iterations)

For each failure:
1. Fetch logs: `aws logs filter-log-events --log-group-name /aws-emr/...`
2. Classify against `references/failures/<domain>.md`
3. Apply fix (config change → terminate + relaunch; script/step fix → resubmit)
4. Increment counter
5. **Halt if**: same failure recurs (cycle), budget exhausted, or unmappable failure

### Stage 7 — Report Results

**On success**:
- Output final EMR 7.x RunJobFlow configuration as JSON
- List all cluster-level fixes applied with explanations
- List all application-level fixes (Spark code changes, Hive script adaptations, Pig→PySpark conversions, Zeppelin notebook adaptations)
- Provide adapted bootstrap script and application S3 locations
- Terminate test cluster

**On failure/halt**:
- Terminate test cluster
- Report: fixes attempted, remaining blockers, manual remediation for each blocker
- Output partial configuration (what was successfully adapted)

## Halt Conditions

| Trigger | Action |
|---------|--------|
| MapR filesystem detected | Immediate halt — not supported |
| Oozie with no Spark/Hive equivalent | Halt with conversion guidance (see `references/failures/infrastructure.md` OOZIE_REMOVED) |
| Pig with STREAM operator or custom Java UDFs | Flag for manual conversion; continue with other scripts |
| Pig conversion produces >20% data mismatch in validation | Halt Pig domain, report discrepancies for manual review |
| Zeppelin notebook with unsupported custom interpreter | Skip notebook, report which interpreters need manual setup |
| 5 fixes exhausted | Report remaining blockers |
| Same failure repeats after fix | Report cycle detected |
| >2 cluster launch failures | Report infra issue |
| Instance type unavailable | Suggest alternatives, pause for user input |

## Safety Guarantees

1. Original cluster is **never modified**
2. Original application code is **never overwritten** — all migrated artifacts are written to new locations (see naming conventions below)
3. Original Pig scripts are preserved; converted PySpark is written to new paths
4. Original Zeppelin notebooks are exported and preserved; adapted versions uploaded as new notebooks
5. Test cluster tagged `emr-migration-skill=test-run` for cost tracking
6. Test cluster terminated by skill in Stage 7 (on success or failure)
7. Validation steps are read-only or use test data
8. Minimum instance count (1 primary + 1 core) to limit cost

### Migrated Artifact Naming Conventions

| Application | Original Location | Migrated Location |
|-------------|------------------|-------------------|
| Bootstrap scripts | `s3://bucket/path/script.sh` | `s3://bucket/path/script-emr7-migrated.sh` |
| Hive scripts | `s3://bucket/path/query.hql` | `s3://bucket/path/query-hive3-migrated.hql` |
| Presto/Trino scripts | `s3://bucket/path/query.sql` | `s3://bucket/path/query-trino-migrated.sql` |
| MapReduce JARs | `s3://bucket/path/job.jar` | `s3://bucket/path/job-hadoop3-migrated.jar` |
| Flink JARs | `s3://bucket/path/flink-app.jar` | `s3://bucket/path/flink-app-flink18-migrated.jar` |
| Spark application code | `local-path/src/` | `local-path/src-emr7-migrated/` |
| Pig → PySpark | `s3://bucket/pig/script.pig` | `s3://bucket/pig/script-pyspark-migrated.py` |
| Zeppelin notebooks | `notebook_{id}.json` | `notebook_{id}_emr7_migrated.json` |

The original S3 objects and local files remain untouched. If a migration is re-run, the `-migrated` artifacts are overwritten (idempotent), but originals are never affected.


