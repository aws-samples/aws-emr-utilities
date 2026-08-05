# Configuration Transforms — EMR 5.0–5.35 (AL1) to 7.x (AL2023)

Detailed property mappings and adaptation rules applied during Stage 2 of the migration workflow.

> **Scope**: Source releases EMR 5.0–5.35 (Amazon Linux 1). EMR 5.36+ uses AL2 and has a different bootstrap/OS migration path.

---

## 1. Release Label

Set `ReleaseLabel` to target EMR 7.x (e.g., `emr-7.1.0`).

---

## 2. Application Version Mapping

| EMR 5.x Component | EMR 7.x Equivalent | Notes |
|---|---|---|
| Spark 2.4.x | Spark 3.5.x | Scala 2.11→2.12, major API changes |
| Hive 2.3.x | Hive 3.1.x | ACID default, managed table changes |
| Presto 0.2xx | Trino 4xx | Complete rebrand |
| HBase 1.4.x | HBase 2.5.x | Admin API rewrite |
| Hadoop 2.10.x | Hadoop 3.3.x | S3 scheme, API removals |
| Tez 0.9.x | Tez 0.10.x | Mostly compatible |
| Flink 1.x | Flink 1.18.x | Memory model, deployment mode |
| Pig 0.17.x | Pig 0.17.0 (deprecated) | Still available but unmaintained; recommend conversion to PySpark (Stage 3F) |
| Oozie 5.1.x | Oozie 5.2.1 (deprecated) | Still available but unmaintained; recommend Step Functions / MWAA |

---

## 3. Configuration Property Changes

### 3.1 Spark Configuration

**Add for backward compatibility (apply all by default):**
```json
[
  {"Classification": "spark-defaults", "Properties": {
    "spark.sql.ansi.enabled": "false",
    "spark.sql.storeAssignmentPolicy": "LEGACY",
    "spark.sql.legacy.timeParserPolicy": "LEGACY",
    "spark.sql.legacy.createHiveTableByDefault": "true",
    "spark.sql.legacy.parquet.int96RebaseModeInRead": "LEGACY",
    "spark.sql.legacy.parquet.int96RebaseModeInWrite": "LEGACY",
    "spark.sql.legacy.parquet.datetimeRebaseModeInRead": "LEGACY",
    "spark.sql.legacy.parquet.datetimeRebaseModeInWrite": "LEGACY",
    "spark.sql.legacy.avro.datetimeRebaseModeInRead": "LEGACY"
  }}
]
```

**Remove if present:**
- `spark.yarn.*` keys that reference YARN-specific internal APIs removed in Hadoop 3
- `spark.sql.hive.metastore.version` if set to < 2.3

**Update if present:**
- `spark.driver.extraJavaOptions` / `spark.executor.extraJavaOptions` — append `--add-opens` flags if custom JARs detected:
  ```
  --add-opens java.base/java.lang=ALL-UNNAMED
  --add-opens java.base/java.lang.invoke=ALL-UNNAMED
  --add-opens java.base/java.lang.reflect=ALL-UNNAMED
  --add-opens java.base/java.io=ALL-UNNAMED
  --add-opens java.base/java.net=ALL-UNNAMED
  --add-opens java.base/java.nio=ALL-UNNAMED
  --add-opens java.base/java.util=ALL-UNNAMED
  --add-opens java.base/java.util.concurrent=ALL-UNNAMED
  --add-opens java.base/sun.nio.ch=ALL-UNNAMED
  --add-opens java.base/sun.security.action=ALL-UNNAMED
  --add-opens java.security.jgss/sun.security.krb5=ALL-UNNAMED
  ```
  Only add what's needed — add targeted flags based on specific `InaccessibleObjectException` messages during validation.

### 3.2 Hive Configuration

**Add for backward compatibility (Glue Catalog clusters ONLY):**
```json
[
  {"Classification": "hive-site", "Properties": {
    "hive.create.as.acid": "false",
    "hive.create.as.insert.only": "false",
    "hive.txn.strict.locking.mode": "false"
  }}
]
```
Note: These properties prevent Hive 3 from creating ACID tables by default. Required for Glue Catalog (which doesn't support ACID). For standalone HMS clusters, omit these — EMR 7.x Hive 3 may reject them as unknown properties depending on the exact EMR release.

**Add for Spark-Glue integration (if Glue Catalog used):**
```json
[
  {"Classification": "spark-defaults", "Properties": {
    "spark.hadoop.hive.metastore.client.factory.class": "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory"
  }}
]
```

**Update if present:**
- `hive.metastore.schema.verification` — set to `false` for initial migration
- `javax.jdo.option.ConnectionURL` — verify JDBC driver compatible with Hive 3.x

### 3.3 Hadoop / Core-site

**Remove:**
- `fs.s3n.impl` and all `fs.s3n.*` properties
- `fs.s3.impl` if set to `NativeS3FileSystem`

**Update:**
- `fs.s3a.endpoint` — verify still valid; EMR 7.x uses regional endpoints by default
- `mapreduce.framework.name` — should remain `yarn`

### 3.4 YARN Configuration

**Remove deprecated:**
- `yarn.nodemanager.aux-services.mapreduce.shuffle.class` → rename to `mapreduce_shuffle.class` (underscore)
- `yarn.resourcemanager.resource-tracker.address` if duplicating hostname

**Verify:**
- `yarn.nodemanager.linux-container-executor.group` — ensure group exists on AL2023
- `yarn.nodemanager.resource.memory-mb` — still valid, no change

### 3.5 HBase Configuration

**Update if present:**
- `hbase.coprocessor.region.classes` — verify coprocessor JAR compatibility with HBase 2.x
- `hbase.rootdir` — if using HDFS path, verify HDFS still configured (EMR 7.x supports HDFS on local instance storage)

### 3.6 Presto → Trino Configuration

**Rename classification:**
- `presto-connector-*` → `trino-connector-*`
- `presto-config` → `trino-config`

**Update properties:**
- `connector.name=hive-hadoop2` → `connector.name=hive`
- Remove `presto.` prefix from session properties

---

## 4. Bootstrap Action Adaptation

Apply these transformations to every bootstrap script:

### 4.1 Package Manager
```bash
# Before (AL1)
yum install -y <package>
# After (AL2023)
dnf install -y <package>
```

### 4.2 Init System
```bash
# Before (AL1)
service <name> start
chkconfig <name> on
# After (AL2023)
systemctl start <name>
systemctl enable <name>
```

### 4.3 Python
```bash
# Before (AL1)
#!/usr/bin/python
pip install <pkg>
# After (AL2023)
#!/usr/bin/python3
python3 -m pip install <pkg>
```

### 4.4 Java Paths
```bash
# Before (AL1)
export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk
# After (AL2023) — if Java 17 compatible:
export JAVA_HOME=/usr/lib/jvm/java-17-amazon-corretto
# Or if Java 8 fallback required:
export JAVA_HOME=/usr/lib/jvm/java-1.8.0-amazon-corretto
```

### 4.5 Hadoop Paths
```bash
# Before (AL1 / Hadoop 2.x)
/usr/lib/hadoop/lib/
# After (AL2023 / Hadoop 3.x)
/usr/lib/hadoop/share/hadoop/common/lib/
```

### 4.6 IMDSv2 (Metadata Service)
```bash
# Before (AL1 — IMDSv1)
curl http://169.254.169.254/latest/meta-data/instance-id
# After (AL2023 — IMDSv2 required)
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id
```

**Important**: EMR 7.x launches instances with `HttpTokens=required` (IMDSv2 only). Any bootstrap action, application code, or custom script that calls the instance metadata endpoint without an IMDSv2 token will get HTTP 401. Common places this hides:
- Bootstrap scripts fetching instance type/AZ for conditional logic
- Custom metrics scripts reading instance ID
- Application code resolving IAM role credentials via instance profile (AWS SDK v1 < 1.11.x does NOT support IMDSv2 automatically)
- Ganglia replacement scripts reading hostname

### 4.8 Log4j 1.x → Log4j2

EMR 7.x ships Log4j2 for all components (Spark, Hive, YARN, etc.). Custom `log4j.properties` files are **silently ignored** — they produce no error but also no configured logging behavior.

```bash
# Before (AL1 — Log4j 1.x)
# File: log4j.properties
log4j.rootLogger=INFO, console
log4j.appender.console=org.apache.log4j.ConsoleAppender
log4j.appender.console.layout=org.apache.log4j.PatternLayout
log4j.appender.console.layout.ConversionPattern=%d{yy/MM/dd HH:mm:ss} %p %c: %m%n
log4j.logger.org.apache.spark=WARN

# After (AL2023 — Log4j2)
# File: log4j2.properties
rootLogger.level = info
rootLogger.appenderRef.console.ref = console
appender.console.type = Console
appender.console.name = console
appender.console.layout.type = PatternLayout
appender.console.layout.pattern = %d{yy/MM/dd HH:mm:ss} %p %c: %m%n
logger.spark.name = org.apache.spark
logger.spark.level = warn
```

**EMR classification mapping:**
| EMR 5.x Classification | EMR 7.x Classification |
|---|---|
| `spark-log4j` | `spark-log4j2` |
| `hive-log4j` | `hive-log4j2` |
| `yarn-env` (log4j portion) | `yarn-env` (log4j2 format) |

**Detection**: After cluster launch, verify logging by checking `/var/log/spark/` or step stderr. If logs are missing or at unexpected levels, the old `log4j.properties` format is being silently dropped.

**Custom JARs**: If application code uses Log4j 1.x API directly (`org.apache.log4j.Logger`), it still works via the `log4j-1.2-api` bridge JAR included in EMR 7.x. But custom `log4j.properties` bundled inside JARs are ignored — configure via EMR classifications or `log4j2.properties` on classpath.

### 4.9 EMRFS Consistency View Removal

EMR 7.x removes EMRFS Consistent View (which used DynamoDB for S3 metadata tracking). This feature was useful with S3 eventual consistency but is **unnecessary since December 2020** when S3 became strongly consistent.

**Remove these properties if present:**
```json
{"Classification": "emrfs-site", "Properties": {
  "fs.s3.consistent": "REMOVE — property ignored",
  "fs.s3.consistent.metadata.tableName": "REMOVE",
  "fs.s3.consistent.retryPeriodSeconds": "REMOVE",
  "fs.s3.consistent.retryCount": "REMOVE",
  "fs.s3.consistent.metadata.read.capacity": "REMOVE",
  "fs.s3.consistent.metadata.write.capacity": "REMOVE"
}}
```

**Also remove:**
- The `emrfs` CLI tool calls in bootstrap actions (tool no longer exists)
- DynamoDB table provisioned for EMRFS metadata (no longer needed — can be decommissioned after migration verified)
- IAM policy statements granting `dynamodb:*` to the EMR role specifically for EMRFS (review if still needed for workload)

**Impact**: None for data correctness (S3 is strongly consistent). However, bootstrap scripts that call `emrfs sync` or `emrfs delete` will fail with "command not found".

### 4.7 Package Renames

| AL1 Package | AL2023 Package |
|---|---|
| `mysql` | `mariadb` |
| `mysql-server` | `mariadb-server` |
| `python-pip` | `python3-pip` |
| `python-devel` | `python3-devel` |
| `java-1.8.0-openjdk` | `java-17-amazon-corretto` |
| `java-1.8.0-openjdk-devel` | `java-17-amazon-corretto-devel` |
| `libffi-devel` | `libffi-devel` (same) |

---

## 5. Step Definition Migration

### 5.1 Spark Steps
- Verify `--class` references compile against Scala 2.12
- Add `--conf spark.driver.userClassPathFirst=true` if custom JARs present
- Replace `--master yarn-cluster` with `--master yarn --deploy-mode cluster` (former is deprecated)

### 5.2 Hive Steps
- Scan HQL for reserved keywords → quote with backticks
- Scan for `INSERT OVERWRITE` on managed tables → add `SET` statements or convert to EXTERNAL

### 5.3 Presto → Trino Steps
- Replace `presto-cli` with `trino-cli`
- Update JDBC connection strings: `jdbc:presto://` → `jdbc:trino://`
- Update driver class: `com.facebook.presto.jdbc.PrestoDriver` → `io.trino.jdbc.TrinoDriver`

### 5.4 Custom JAR Steps
- Verify JAR compiled for Java 8 will run on Java 17
- Check Hadoop API usage (Hadoop 2 → 3 removals)
- Check Scala version if Scala-based

---

## 6. Instance Type Substitution

If source instance types are deprecated or unavailable:

| Deprecated | Current-Gen Replacement |
|---|---|
| m5.* | m6i.* or m7i.* |
| r5.* | r6i.* or r7i.* |
| c5.* | c6i.* or c7i.* |
| i3.* | i3en.* or i4i.* |
| d2.* | d3.* |
| m4.* | m6i.* |
| r4.* | r6i.* |
| c4.* | c6i.* |

Verify with:
```bash
aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters Name=location,Values=<az> \
  --query 'InstanceTypeOfferings[].InstanceType' \
  --region $REGION
```
