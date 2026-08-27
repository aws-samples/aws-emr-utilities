# EMR Platform / Infrastructure / Networking / Security / Resource Failures

## EMR Platform / Infrastructure

### EMR7_RPM_REPO_MISSING

- **Cause**: Certain EMR 7.x release labels (e.g., emr-7.1.0) have missing RPM packages in specific regions. EMR auto-includes Delta Lake and Apache Hudi RPMs even when only Hadoop+Flink (or other minimal applications) are requested in the cluster launch configuration. If those RPMs are absent from the region's dnf cache, the cluster fails during provisioning.
- **Detection**: Cluster terminates with `BOOTSTRAP_FAILURE`. Provisioning logs show: `Error: No match for argument: delta-3.0.0` or `Error: No match for argument: hudi-0.14.1`. Instance state logs show dnf install failures for packages not explicitly requested by the user.
- **Fix**:
  - Upgrade to a newer EMR 7.x release label (emr-7.5.0 or later) where the RPM repo is complete
  - If stuck on the specific release: try a different AZ in the same region
  - If the error persists across AZs: file a support ticket — this is an EMR platform-side issue, not a user configuration problem
  - Workaround: explicitly exclude the problematic applications if possible, though EMR may still attempt to install them as dependencies
- **Important**: This is a transient platform issue. The same subnet and configuration that launched EMR 5.x clusters (and earlier 7.x clusters) successfully will fail with this error. Retry with a newer EMR release label.

### SPARK_CLASSPATH_POISON

- **Cause**: Scala 2.11 fat/uber JARs placed on `spark.driver.extraClassPath` or `spark.executor.extraClassPath` corrupt the SparkSubmit launcher itself before application code runs. The Scala 2.11 runtime classes in the uber JAR conflict with Spark 3.5's bundled Scala 2.12, causing NoSuchMethodError or AbstractMethodError during Spark's own initialization — not in user code.
- **Detection**: 
  - `NoSuchMethodError` or `AbstractMethodError` in `scala.collection.*`, `scala.Predef$`, or `scala.runtime.*` occurring **during SparkSubmit startup** (before any user code executes)
  - Stack trace shows Spark framework classes (`org.apache.spark.deploy.SparkSubmit`, `org.apache.spark.SparkContext.<init>`) failing, not user application classes
  - Same JAR works when submitted via `--jars` (isolated classloader) but fails on `extraClassPath` (parent classloader)
- **Root Cause Detail**: `extraClassPath` prepends JARs to the parent classloader, so Scala 2.11 classes in the uber JAR take precedence over Spark's bundled Scala 2.12. This poisons the entire JVM — Spark cannot even initialize its own `SparkContext`. The `--jars` flag uses a child classloader, so conflicts are isolated.
- **Fix** (in order of preference):
  1. **Recompile the uber JAR** for Scala 2.12 (primary fix — this is the correct long-term solution)
  2. **Move from `extraClassPath` to `--jars`**: Change `spark.driver.extraClassPath=/path/to/uber.jar` → `spark-submit --jars /path/to/uber.jar`. The child classloader isolates the conflict.
  3. **Set classPathFirst flags**: `spark.driver.userClassPathFirst=true` + `spark.executor.userClassPathFirst=true` — this inverts the classloader precedence but may introduce other conflicts with bundled libraries
  4. **Extract only needed classes**: If the uber JAR bundles application code + Scala runtime, rebuild it to exclude `scala.*` and `org.scala-lang.*` packages (use Maven shade plugin exclusions or sbt assembly merge strategy)
- **Important**: This issue typically requires 2-3 fix iterations to diagnose because the error message (`NoSuchMethodError` in Scala internals) looks identical to SPARK_SCALA_BINARY but the root cause (classpath poisoning) requires a different fix approach (moving JARs off `extraClassPath` rather than just recompiling).

### EMRFS_TO_S3A_COMMITTER

- **Cause**: EMR 7.10+ replaces EMRFS with native S3A filesystem. The S3A Magic Committer requires explicit configuration for correct write behavior (atomic commits, consistent listing). **This applies only to EMR 7.10+; earlier EMR 7.x releases (7.1–7.5) still use EMRFS for the s3:// scheme.**
- **Detection**: Output files missing after successful job completion, duplicate output files from speculative tasks, `DirectFileOutputCommitter is deprecated`, slow S3 writes.
- **Fix** (EMR 7.10+ only): Add S3A committer configuration to `spark-defaults` or `core-site`:
  ```
  spark.hadoop.fs.s3a.committer.name=magic
  spark.hadoop.fs.s3a.committer.magic.enabled=true
  spark.hadoop.fs.s3a.fast.upload=true
  spark.hadoop.fs.s3a.fast.upload.buffer=bytebuffer
  spark.hadoop.mapreduce.outputcommitter.factory.scheme.s3a=org.apache.hadoop.fs.s3a.commit.S3ACommitterFactory
  ```
  - Remove `fs.s3.impl` and `fs.s3n.impl` properties **only on EMR 7.10+** (EMRFS classes no longer exist on those releases)
  - Remove `emrfs-site` classification **only on EMR 7.10+**
  - **On EMR 7.1–7.5**: EMRFS is still in use for s3:// — do NOT remove `fs.s3.impl` or `emrfs-site`. Only remove the `fs.s3.consistent*` Consistent View properties (see platform.md EMRFS_CONSISTENT_VIEW_REMOVED)

## Presto → Trino

### PRESTO_TO_TRINO_RENAME

- **Cause**: Complete rebrand in EMR 7.x — Presto removed, Trino is the replacement.
- **Detection**: `presto-cli: command not found`, `ClassNotFoundException: com.facebook.presto.jdbc.PrestoDriver`, JDBC connection refused on presto port.
- **Fix**:

| Presto | Trino |
|--------|-------|
| `presto-cli` | `trino-cli` |
| `com.facebook.presto.jdbc.PrestoDriver` | `io.trino.jdbc.TrinoDriver` |
| `jdbc:presto://host:8889` | `jdbc:trino://host:8889` |
| `connector.name=hive-hadoop2` | `connector.name=hive` |
| `presto-connector-*` classification | `trino-connector-*` classification |
| `presto-config` classification | `trino-config` classification |

### TRINO_SQL_CHANGES

- **Cause**: Trino has stricter SQL semantics than Presto 0.2xx.
- **Detection**: `QueryFailed`, `FUNCTION_NOT_FOUND`, `TYPE_MISMATCH`, unexpected timestamp behavior.
- **Fix**:
  - `current_timestamp` returns `timestamp with time zone` — add explicit `CAST(current_timestamp AS timestamp)` if code expects timestamp without TZ
  - `json_extract` returns JSON type — use `json_extract_scalar` to get unwrapped string values (CAST to VARCHAR yields quoted JSON text, not the raw scalar)
  - Remove `presto.` prefix from session properties
  - `APPROX_DISTINCT` behavior unchanged but `APPROX_PERCENTILE` signature may differ

### TRINO_CONNECTOR_CONFIG

- **Cause**: Connector configuration properties renamed between Presto and Trino.
- **Detection**: `Catalog not found`, connector fails to initialize, `Unknown connector` errors.
- **Fix**:
  - Verify catalog properties files updated from Presto to Trino format
  - `hive.metastore.uri` unchanged; `hive.s3.endpoint` → verify still valid
  - Custom connectors must be recompiled against Trino SPI (not Presto SPI)

## HBase (1.4 → 2.5)

### HBASE2_API_BREAK

- **Cause**: Removed deprecated Admin/Table APIs.
- **Logs**: `NoSuchMethodError`/`ClassNotFoundException` for `HBaseAdmin`, `HTable`, descriptors
- **Fix**:

| Removed | Replacement |
|---------|-------------|
| `new HBaseAdmin(conf)` | `Connection.getAdmin()` |
| `new HTable(conf, name)` | `Connection.getTable(TableName.valueOf(name))` |
| `HTableDescriptor` | `TableDescriptorBuilder` |
| `HColumnDescriptor` | `ColumnFamilyDescriptorBuilder` |

### HBASE2_COPROCESSOR

- **Cause**: Coprocessor API refactored.
- **Logs**: `ClassCastException`/`AbstractMethodError` in coprocessor classes
- **Fix**: Requires manual rewrite to `RegionObserver`/`RegionCoprocessor`/`MasterObserver`. **Flag as manual remediation.**

## Removed Applications

### OOZIE_REMOVED

- **Cause**: Oozie not in EMR 7.x.
- **Logs**: `oozie: command not found`
- **Fix**: Step Functions (recommended), MWAA (Airflow), or EMR Steps + EventBridge. **Manual redesign required.**

## Networking / Security

### IMDSV1_DISABLED

- **Cause**: EMR 7.x enforces IMDSv2.
- **Detection/Fix**: See `failures/platform.md` category IMDSV2_METADATA_DENIED for full detection patterns, SDK version thresholds, and fix procedures.

### TLS_VERSION_MINIMUM

- **Cause**: AL2023 requires TLS 1.2+.
- **Logs**: `SSLHandshakeException`, `protocol_version alert`
- **Fix**: Update clients to TLS 1.2+.

### NO_PROXY_REGIONAL_S3_MISMATCH

- **Cause**: Private subnet clusters using HTTP proxy have `NO_PROXY=*.amazonaws.com` but Java's `DefaultProxySelector` treats `*` as matching a **single DNS label only** — does NOT match across dot separators.
- **Detection**: Cluster launches succeed but are extremely slow, S3 download speeds ~15 MB/s instead of ~80+ MB/s, `APP_PROVISIONING_FAILED_TIME_OUT` on larger clusters.
- **Fix**: Add explicit regional S3 entries to `NO_PROXY`:
  ```
  s3.<region>.amazonaws.com
  *.s3.<region>.amazonaws.com
  s3.dualstack.<region>.amazonaws.com
  *.s3.dualstack.<region>.amazonaws.com
  ```

### APP_PROVISIONING_TIMEOUT

- **Cause**: EMR 7.x test cluster nodes fail to provision within the dynamically computed timeout.
- **Detection**: Cluster terminates with `APP_PROVISIONING_FAILED_TIME_OUT`.
- **Fix** (investigate in order):
  1. **Slow S3 downloads** (most common): Check NO_PROXY config, verify S3 VPC Gateway Endpoint
  2. **Bootstrap actions too slow**: Move heavy operations into custom AMI
  3. **Custom AMI issues**: Ensure based on AL2023
  4. **Resource contention**: Use instance types with sufficient network bandwidth
  5. **Workaround**: Reduce cluster size or use larger instance types

### PRIVATE_SUBNET_CONNECTIVITY

- **Cause**: EMR 7.x test cluster in private subnet cannot reach required endpoints during provisioning or runtime.
- **Detection**: Cluster fails with `APP_PROVISIONING_FAILED_TIME_OUT` or `BOOTSTRAP_FAILURE`. Instance Controller never checks in.
- **Fix**:
  - Verify route table has routes to: S3 VPC Gateway Endpoint, NAT Gateway
  - Verify EMR-managed security groups allow ALL egress
  - Verify DNS resolution works (`enableDnsHostnames=true`, `enableDnsSupport=true`)

## Resource

### OOM_RESOURCE

- **Cause**: Insufficient memory; may be worse with Java 17 or Spark 3.x memory model.
- **Logs**: `Container killed by YARN for exceeding memory limits`, `OutOfMemoryError`
- **Fix**: Increase `spark.executor.memory` → increase instance type → increase count.

### TRANSIENT_INFRA

- **Cause**: Spot interruption, AZ issue, throttling.
- **Logs**: AWS-internal error, no application stack trace
- **Fix**: Retry. After 2 retries: escalate.

### INSTANCE_TYPE_UNAVAILABLE

- **Cause**: Instance type not available for EMR 7.x in selected AZ.
- **Logs**: `INSTANCE_NOT_AVAILABLE`, `InsufficientInstanceCapacity`
- **Fix**: Substitute: m5→m6i/m7i, r5→r6i/r7i, c5→c6i/c7i, i3→i3en/i4i, d2→d3.
