# EMR AL1 → AL2023 — Failure Catalogue

Failure categories for classifying errors during EMR 5.x to 7.x migration. Each entry has: category ID, cause, log-based identification, and prescribed fix.

---

## Platform-Level (All Applications)

### JAVA_VERSION_INCOMPATIBLE

- **Cause**: EMR 7.x defaults to Java 17 (Amazon Corretto 17). Java 8 code may use removed APIs or illegal reflective access.
- **Logs**: `NoSuchMethodError`, `IllegalAccessError`, `InaccessibleObjectException`, `UnsupportedClassVersionError`
- **Fix**:
  - Add JVM flags: `--add-opens java.base/java.lang=ALL-UNNAMED --add-opens java.base/java.util=ALL-UNNAMED`
  - Or set `JAVA_HOME=/usr/lib/jvm/java-1.8.0-amazon-corretto` in `spark-env`/`hadoop-env` classification
  - Or recompile targeting Java 17

### JAVA17_REMOVED_PACKAGES

- **Cause**: Java EE modules removed from JDK 11+ (JAXB, JAX-WS, CORBA, Activation, Annotation). Custom JARs compiled for Java 8 that import these packages fail at runtime on Java 17.
- **Logs**: `ClassNotFoundException: javax.xml.bind.JAXBException`, `NoClassDefFoundError: javax/annotation/PostConstruct`, `ClassNotFoundException: javax.activation.DataHandler`
- **Fix** (choose one):
  - Add explicit dependencies to application JAR:
    ```
    jakarta.xml.bind:jakarta.xml.bind-api:4.0.0
    org.glassfish.jaxb:jaxb-runtime:4.0.3
    jakarta.annotation:jakarta.annotation-api:2.1.1
    jakarta.activation:jakarta.activation-api:2.1.2
    ```
  - Or add `--add-modules java.xml.bind` (Java 9-10 only — does NOT work on 17)
  - Or bundle the JARs and add via `spark.driver.extraClassPath` / `spark.executor.extraClassPath`
  - Or fall back to Java 8: `export JAVA_HOME=/usr/lib/jvm/java-1.8.0-amazon-corretto` in bootstrap

### JAVA17_REFLECTION_DENIED

- **Cause**: Java 17 enforces strong encapsulation. Code using reflection to access JDK internals (e.g., `sun.misc.Unsafe`, `java.lang.reflect.Field.setAccessible` on JDK classes) throws `InaccessibleObjectException`.
- **Logs**: `java.lang.reflect.InaccessibleObjectException: Unable to make ... accessible: module java.base does not "opens" ... to unnamed module`
- **Fix**: Add `--add-opens` flags to `spark.driver.extraJavaOptions` and `spark.executor.extraJavaOptions`:
  ```
  --add-opens java.base/java.lang=ALL-UNNAMED
  --add-opens java.base/java.lang.invoke=ALL-UNNAMED
  --add-opens java.base/java.lang.reflect=ALL-UNNAMED
  --add-opens java.base/java.io=ALL-UNNAMED
  --add-opens java.base/java.net=ALL-UNNAMED
  --add-opens java.base/java.nio=ALL-UNNAMED
  --add-opens java.base/java.util=ALL-UNNAMED
  --add-opens java.base/java.util.concurrent=ALL-UNNAMED
  --add-opens java.base/java.util.concurrent.atomic=ALL-UNNAMED
  --add-opens java.base/sun.nio.ch=ALL-UNNAMED
  --add-opens java.base/sun.nio.cs=ALL-UNNAMED
  --add-opens java.base/sun.security.action=ALL-UNNAMED
  --add-opens java.base/sun.util.calendar=ALL-UNNAMED
  --add-opens java.security.jgss/sun.security.krb5=ALL-UNNAMED
  ```
  - Spark/Hadoop already set many of these — only add those triggered by YOUR application's reflection usage
  - Check the exact module/package from the error message and add targeted `--add-opens`

### IMDSV2_METADATA_DENIED

- **Cause**: EMR 7.5 on AL2023 enforces IMDSv2 (`HttpTokens=required`). Code that calls the instance metadata service without an IMDSv2 session token receives HTTP 401. **Validated**: bootstrap action testing confirmed IMDSv1 returns HTTP 401 on all EMR 7.5 nodes (master + core).
- **Logs**: `HTTP 401` from `169.254.169.254`, `Unable to retrieve credentials`, `metadata service returned 401`, custom scripts returning empty/null for instance metadata, bootstrap actions failing silently when metadata calls return empty
- **Detection**: Scan bootstrap actions and custom scripts for: `curl.*169.254.169.254` (without token header), `wget.*169.254.169.254`, `ec2-metadata` (deprecated CLI). Also check application code that reads instance identity for logging/tagging.
- **Fix**:
  - Update shell scripts:
    ```bash
    TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
    curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id
    ```
  - Update AWS SDK: Upgrade to SDK v1 >= 1.11.x or SDK v2 (both handle IMDSv2 automatically)
  - Python `requests` to metadata: Add `X-aws-ec2-metadata-token` header

### LOG4J_CONFIG_SILENTLY_IGNORED

- **Cause**: Spark on EMR 7.x uses Log4j2 (`/etc/spark/conf/log4j2.properties`). Custom `log4j.properties` (Log4j 1.x format) files are **silently ignored** — no error, but configured logging behavior is lost. **Validated**: Testing confirmed `/etc/spark/conf/log4j.properties` does NOT exist on EMR 7.5; only `log4j2.properties` is present. Hadoop still uses `log4j.properties` (inconsistency). Hive uses `hive-log4j2.properties`.
- **Logs**: No error — the symptom is *missing* logs or unexpected log levels. Debug/trace logging stops working, custom appenders (file rotation, remote syslog) stop receiving events.
- **Detection**: Check if EMR cluster configuration uses `spark-log4j` classification (old name) instead of `spark-log4j2` (new name). Check if application bundles a `log4j.properties` file via `--files` or jar resources.
- **Fix**:
  - Convert Log4j 1.x config to Log4j2 format (see `references/configuration-transforms.md` section 4.8)
  - Change EMR classification: `spark-log4j` → `spark-log4j2`, `hive-log4j` → `hive-log4j2`
  - Do NOT convert `hadoop-log4j` — Hadoop on EMR 7.5 still uses `log4j.properties` format
  - For custom appenders, find Log4j2 equivalents (e.g., `org.apache.log4j.DailyRollingFileAppender` → `RollingFile` with `TimeBasedTriggeringPolicy`)
  - Log4j 1.x API calls in code still work (bridge JAR `log4j-1.2-api` is included) — only the config file format changed

### EMRFS_CONSISTENT_VIEW_REMOVED

- **Cause**: EMRFS Consistent View (which used DynamoDB for S3 metadata tracking) is removed from EMR 7.x. The `emrfs` CLI tool does not exist.
- **Logs**: `emrfs: command not found`, `Class not found: com.amazon.ws.emr.hadoop.fs.consistency.*`
- **Detection**: Bootstrap actions calling `emrfs sync`, `emrfs delete`, or `emrfs import`. Configuration using `fs.s3.consistent*` properties.
- **Fix**:
  - Remove all `emrfs` CLI calls from bootstrap actions — they are unnecessary (S3 has been strongly consistent since December 2020)
  - Remove `fs.s3.consistent*` properties from `emrfs-site` classification
  - DynamoDB table previously used by EMRFS can be decommissioned after verifying no other workload uses it
  - Remove IAM permissions for EMRFS DynamoDB access from EMR role (if solely for EMRFS)

### GLUE_CATALOG_HIVE3_INCOMPATIBILITY

- **Cause**: Glue Data Catalog does not support Hive 3.x ACID/transactional tables. On EMR 7.x, Hive 3 attempts to create managed tables as ACID by default, which fails against Glue.
- **Logs**: `MetaException: Glue does not support transactional tables`, `UnsupportedOperationException: Transactional operations are not supported`
- **Detection**: Cluster uses Glue as metastore (`hive.metastore.client.factory.class = ...AWSGlueDataCatalogHiveClientFactory`) AND scripts create tables without EXTERNAL keyword.
- **Fix**:
  - Set `hive.create.as.acid=false` and `hive.create.as.insert.only=false` in `hive-site` classification (prevents default ACID table creation)
  - Convert all existing ACID tables to EXTERNAL (see Stage 3B step 3)
  - Ensure all new DDL uses `CREATE EXTERNAL TABLE`
  - Set `spark.hadoop.hive.metastore.client.factory.class` in `spark-defaults` for Spark-Glue integration (not just `hive-site`)
  - After migration, invalidate stale Glue column stats: `ALTER TABLE ... SET TBLPROPERTIES('COLUMN_STATS_ACCURATE'='false')`

### PYTHON2_REMOVED

- **Cause**: Python 2.7 not installed on AL2023.
- **Logs**: `/usr/bin/python: No such file or directory`, Python 2 `SyntaxError`
- **Fix**: Update shebangs to `#!/usr/bin/python3`. Convert: `print x` → `print(x)`, `dict.iteritems()` → `dict.items()`, `except E, e:` → `except E as e:`

### BOOTSTRAP_AL2023_COMPAT

- **Cause**: Bootstrap scripts use AL1-specific paths, packages, or init system.
- **Logs**: `No such file or directory`, `command not found`, `No match for argument`
- **Fix**:

| AL1 | AL2023 |
|-----|--------|
| `yum install -y` | `dnf install -y` |
| `service <x> start` | `systemctl start <x>` |
| `/etc/init.d/<x>` | `systemctl` |
| `chkconfig <x> on` | `systemctl enable <x>` |
| `/usr/bin/python` | `/usr/bin/python3` |
| `pip install` | `pip3 install` |
| `/usr/lib/hadoop/lib/` | `/usr/lib/hadoop/share/hadoop/common/lib/` |
| `amazon-linux-extras install` | `dnf install` |

### YUM_PACKAGE_MISSING

- **Cause**: Package renamed or removed in AL2023.
- **Logs**: `No match for argument: <package>`
- **Fix**: `mysql`→`mariadb`, `python-pip`→`python3-pip`, `java-1.8.0-openjdk`→`java-17-amazon-corretto`. Use `dnf provides <file>` to find replacements.

### SYSTEMD_SERVICE_CHANGES

- **Cause**: Service names changed between AL1 and AL2023.
- **Logs**: `Failed to start <service>`, `Unit <service>.service not found`
- **Fix**: `mysqld`→`mariadb`, verify with `systemctl list-unit-files`.

---

## Hadoop / YARN / MapReduce

### HADOOP3_API_BREAK

- **Cause**: Hadoop 3.x removed/relocated APIs from Hadoop 2.x.
- **Detection**: `NoSuchMethodError`/`ClassNotFoundException` for `org.apache.hadoop.*` classes.
- **Fix**: `NativeS3FileSystem` → `S3AFileSystem`. Recompile against Hadoop 3.3.x.

### MAPREDUCE_OLD_API

- **Cause**: Code using the deprecated `org.apache.hadoop.mapred.*` (old MR API) may encounter removed methods in Hadoop 3.
- **Detection**: `NoSuchMethodError` in `org.apache.hadoop.mapred.JobConf`, `org.apache.hadoop.mapred.FileInputFormat`, etc.
- **Fix**:

| Old API (`org.apache.hadoop.mapred.*`) | New API (`org.apache.hadoop.mapreduce.*`) |
|-----|--------|
| `JobConf conf = new JobConf()` | `Job job = Job.getInstance(new Configuration())` |
| `conf.setMapperClass(MyMapper.class)` | `job.setMapperClass(MyMapper.class)` |
| `conf.setReducerClass(MyReducer.class)` | `job.setReducerClass(MyReducer.class)` |
| `FileInputFormat.setInputPaths(conf, ...)` | `FileInputFormat.addInputPath(job, ...)` |
| `JobClient.runJob(conf)` | `job.waitForCompletion(true)` |
| `Reporter reporter` parameter | `Context context` parameter |

### MAPREDUCE_CLASSPATH

- **Cause**: Hadoop JAR paths changed between EMR 5.x and 7.x.
- **Detection**: `ClassNotFoundException` when launching MR jobs, missing JARs at expected paths.
- **Fix**:
  - `/usr/lib/hadoop/lib/` → `/usr/lib/hadoop/share/hadoop/common/lib/`
  - `/usr/lib/hadoop-mapreduce/hadoop-streaming.jar` path unchanged
  - Update any hardcoded paths in scripts or step definitions

### MAPREDUCE_STREAMING_PYTHON

- **Cause**: Hadoop Streaming jobs using Python scripts fail because Python 2 is removed on AL2023.
- **Detection**: `/usr/bin/python: No such file or directory` in streaming mapper/reducer, `SyntaxError` from Python 2 code.
- **Fix**: Update `-mapper`/`-reducer` to use `python3`. Convert scripts to Python 3 syntax.

### YARN_DEPRECATED_CONFIG

- **Cause**: YARN properties removed or renamed in Hadoop 3.x.
- **Detection**: Warnings in ResourceManager logs, unexpected defaults, `InvalidConfigurationException`.
- **Fix**: `yarn.nodemanager.aux-services.mapreduce.shuffle.class` → `mapreduce_shuffle.class` (underscore). `resource-tracker.address` auto-derived from hostname.

### S3_SCHEME_DEPRECATED

- **Cause**: `s3n://` scheme removed in Hadoop 3.x.
- **Detection**: `No FileSystem for scheme: s3n`, `ClassNotFoundException: NativeS3FileSystem`
- **Fix**: Replace `s3n://` → `s3://` in all input/output paths, configs, and scripts. Remove `fs.s3n.impl` and all `fs.s3n.*` config properties.

---

## Spark (2.4 → 3.5)

### SPARK_SCALA_BINARY

- **Cause**: Scala 2.11 → 2.12 binary incompatibility. All Spark 2.4 JARs compiled against Scala 2.11 must be recompiled.
- **Detection**: `NoSuchMethodError`/`AbstractMethodError` in `scala.collection.*`, `scala.Predef$`, `scala.runtime.*`, OR `ClassNotFoundException` for third-party JARs NOT in bundled namespaces (`org.apache.spark.`, `com.fasterxml.jackson.`, `io.netty.`).
- **Fix**:
  - Recompile JARs for Scala 2.12 (primary fix — use Spark Upgrade Agent if available)
  - Workaround: `spark.driver.userClassPathFirst=true` + `spark.executor.userClassPathFirst=true`
  - If UDF uses `udf(AnyRef, DataType)` form: rewrite to typed UDF, or set `spark.sql.legacy.allowUntypedScalaUDF=true` (temporary only)

### SPARK_SQL_LEGACY

- **Cause**: Spark 3.x ANSI compliance, type coercion changes, implicit conversion removal.
- **Detection**: `AnalysisException`, `ArithmeticException` (overflow on implicit cast), `SparkNumberFormatException`, wrong results in aggregations.
- **Fix** (apply all by default for backward compatibility):
  ```
  spark.sql.ansi.enabled=false
  spark.sql.storeAssignmentPolicy=LEGACY
  spark.sql.legacy.timeParserPolicy=LEGACY
  spark.sql.legacy.createHiveTableByDefault=true
  spark.sql.legacy.sizeOfNull=-1
  ```

### SPARK_PARQUET_TIMESTAMP

- **Cause**: Proleptic Gregorian calendar for Parquet/Avro timestamps replaces hybrid Julian/Gregorian.
- **Detection**: Shifted timestamps in output, `SparkUpgradeException` mentioning rebase, `DateTimeException`, `RebaseException`, or `int96` references.
- **Fix** (apply all by default):
  ```
  spark.sql.legacy.parquet.int96RebaseModeInRead=LEGACY
  spark.sql.legacy.parquet.int96RebaseModeInWrite=LEGACY
  spark.sql.legacy.parquet.datetimeRebaseModeInRead=LEGACY
  spark.sql.legacy.parquet.datetimeRebaseModeInWrite=LEGACY
  spark.sql.legacy.avro.datetimeRebaseModeInRead=LEGACY
  ```

### SPARK_DATASOURCE_V2

- **Cause**: Spark 3.x defaults to DataSource V2 for some connectors.
- **Detection**: `UnsupportedOperationException` from data source, unexpected behavior in read/write paths.
- **Fix**: `spark.sql.sources.useV1SourceList=csv,json,parquet,orc,text,avro`

### SPARK_SHUFFLE_CHANGES

- **Cause**: Adaptive Query Execution (AQE) enabled by default in Spark 3.x, changes partition behavior.
- **Detection**: OOM during shuffle stages, unexpected repartitioning, `FetchFailedException` at reduced partition counts.
- **Fix**: `spark.sql.adaptive.enabled=false` (temporary). Tune `spark.sql.adaptive.coalescePartitions.minPartitionSize`.

### SPARK_REMOVED_APIS

- **Cause**: Deprecated Spark 2.x APIs removed in 3.x.
- **Detection**: `NoSuchMethodError`/`ClassNotFoundException` in `org.apache.spark.sql.*` classes.
- **Fix** (code changes — use Spark Upgrade Agent for automated rewrite):

| Removed | Replacement |
|---------|-------------|
| `SQLContext` | `SparkSession` |
| `HiveContext` | `SparkSession.builder().enableHiveSupport()` |
| `registerTempTable` | `createOrReplaceTempView` |
| `unionAll` | `union` |
| `approxCountDistinct` | `approx_count_distinct` |
| `toDegrees`/`toRadians` | `degrees`/`radians` |
| `--master yarn-cluster` | `--master yarn --deploy-mode cluster` |
| `spark.yarn.*` internal keys | Remove (YARN API changed in Hadoop 3) |

### SPARK_HIVE_METASTORE

- **Cause**: Metastore client version mismatch with Glue Catalog or external HMS.
- **Detection**: `MetaException`, `InvalidObjectException`, `IncompatibleMetastoreException`
- **Fix**: Set `spark.hadoop.hive.metastore.client.factory.class=com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory`

### SPARK_PYTHON_VERSION

- **Cause**: PySpark scripts using Python 2 syntax or Python 2-only modules.
- **Detection**: `SyntaxError` in Python traceback, `ImportError`/`ModuleNotFoundError` for `ConfigParser`, `cPickle`, `urllib2`, `urlparse`, `Queue`.
- **Fix**:
  - `print x` → `print(x)`
  - `unicode(x)` → `str(x)`, `basestring` → `str`, `xrange` → `range`
  - `dict.iteritems()` → `dict.items()`, `dict.has_key(k)` → `k in dict`
  - `import ConfigParser` → `import configparser`
  - `import cPickle` → `import pickle`
  - `import urllib2` → `import urllib.request`

### SPARK_DEPENDENCY_CONFLICT

- **Cause**: Bundled library version conflicts (Jackson, Netty, Log4j upgraded in Spark 3.5).
- **Detection**: `NoSuchMethodError`/`IncompatibleClassChangeError` in bundled namespaces (`com.fasterxml.jackson.*`, `io.netty.*`, `org.slf4j.*`).
- **Fix**: Set `--user-jars-first true` in spark-submit. If Log4j: rename `log4j.properties` → `log4j2.properties`, update `org.apache.log4j` imports → `org.apache.logging.log4j`.

---

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

### HIVE3_CTAS_EXTERNAL

- **Cause**: Hive 3.x rejects `CREATE EXTERNAL TABLE ... AS SELECT` (CTAS with EXTERNAL keyword). In Hive 2.x, this was silently accepted (the EXTERNAL keyword was ignored and the table was created as managed). In Hive 3.x, this is an explicit error because EXTERNAL tables and SELECT-based creation have conflicting semantics around data ownership.
- **Detection**: `SemanticException: CREATE-TABLE-AS-SELECT cannot create an external table`, `FAILED: SemanticException ... CTAS ... EXTERNAL`
- **Fix** (choose one):
  - **Option A — Two-step creation (recommended)**:
    ```sql
    -- Step 1: Create external table with explicit schema
    CREATE EXTERNAL TABLE db.table_name (col1 STRING, col2 INT, ...)
    STORED AS ORC
    LOCATION 's3://bucket/path/';
    
    -- Step 2: Insert data
    INSERT INTO db.table_name SELECT col1, col2, ... FROM source_table;
    ```
  - **Option B — Create as managed, then convert**:
    ```sql
    CREATE TABLE db.table_name AS SELECT * FROM source_table;
    ALTER TABLE db.table_name SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');
    ```
  - **Option C — Use INSERT OVERWRITE with pre-created table**:
    ```sql
    CREATE EXTERNAL TABLE db.table_name LIKE source_table STORED AS ORC LOCATION 's3://...';
    INSERT OVERWRITE TABLE db.table_name SELECT * FROM source_table;
    ```
- **Important**: When scanning Hive scripts, flag ALL occurrences of `CREATE EXTERNAL TABLE ... AS SELECT` for rewrite. The pattern `CREATE EXTERNAL TABLE ... LIKE` (without AS SELECT) is fine and unchanged.

### EMRFS_TO_S3A_COMMITTER

- **Cause**: EMR 7.10+ replaces EMRFS with native S3A filesystem. The S3A Magic Committer requires explicit configuration for correct write behavior (atomic commits, consistent listing). Without these settings, Spark/Hive jobs may produce partial or duplicate output files during task failures/speculation, or write performance may degrade due to non-optimized upload paths.
- **Detection**:
  - Output files missing after successful job completion (partial commits)
  - Duplicate output files from speculative tasks
  - Warning: `DirectFileOutputCommitter is deprecated` or `FileAlreadyExistsException` during task commit
  - Slow S3 writes compared to EMR 5.x/6.x performance (no multipart upload optimization)
- **Fix**: Add S3A committer configuration to `spark-defaults` or `core-site` classification:
  ```
  spark.hadoop.fs.s3a.committer.name=magic
  spark.hadoop.fs.s3a.committer.magic.enabled=true
  spark.hadoop.fs.s3a.fast.upload=true
  spark.hadoop.fs.s3a.fast.upload.buffer=bytebuffer
  spark.hadoop.mapreduce.outputcommitter.factory.scheme.s3a=org.apache.hadoop.fs.s3a.commit.S3ACommitterFactory
  ```
  For Hive:
  ```
  hive.blobstore.use.blobstore.as.scratchdir=true
  ```
  - Remove all `fs.s3.impl` and `fs.s3n.impl` properties (EMRFS classes no longer exist)
  - Remove `emrfs-site` classification entirely
  - Add S3A committer validation to migration test suite: verify output file counts match expected and no duplicates exist after job completion
- **Important**: S3A Fast Upload must be enabled (`fs.s3a.fast.upload=true`) for acceptable write performance. Without it, S3 uploads use single-buffer mode which is significantly slower than EMRFS's optimized multipart upload. The `bytebuffer` buffer type provides best performance for most workloads; use `disk` for memory-constrained executors processing large partitions.

---

## Hive (2.3 → 3.1)

### HIVE3_ACID_DEFAULT

- **Cause**: Hive 3.x defaults all managed tables to ACID/transactional. `INSERT OVERWRITE` on transactional tables fails.
- **Detection**: `SemanticException [Error 10265]`, `INSERT OVERWRITE` failures on managed tables, `FAILED: SemanticException ... is not an INSERT-only table`.
- **Fix**:
  - Convert affected tables: `ALTER TABLE t SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');`
  - For new tables: explicitly use `CREATE EXTERNAL TABLE`
  - **WARNING**: Do NOT use `SET hive.create.as.acid=false` or `SET hive.create.as.insert.only=false` in scripts — these properties do NOT exist on EMR 7.x and will cause the script to fail with `hive configuration ... does not exists`. Handle ACID at the table level (EXTERNAL) or via cluster-level `hive-site` configuration classification at launch time.

### HIVE3_MANAGED_TABLE

- **Cause**: Hive 3 enforces that external tools cannot access managed table data files directly. S3/HDFS reads of managed table paths fail with permission errors.
- **Detection**: Permission errors accessing managed table paths, `HiveAccessControlException`, tools reading warehouse paths get empty results.
- **Fix**: `ALTER TABLE <t> SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');`
- For bulk conversion: query `information_schema.tables` or Glue Catalog to list all managed tables and batch-convert.

### HIVE3_SYNTAX_CHANGES

- **Cause**: New reserved keywords in Hive 3.x that were unreserved in 2.x.
- **Detection**: `ParseException`, `SemanticException` on DDL/DML that was valid in Hive 2.3.
- **Fix**: Backtick-quote reserved words: `` `date` ``, `` `time` ``, `` `timestamp` ``, `` `interval` ``, `` `user` ``, `` `role` ``, `` `groups` ``, `` `index` ``, `` `exchange` ``
- Scan ALL .hql files for unquoted usage of these keywords as column/table names.

### HIVE3_TYPE_CONVERSION

- **Cause**: Stricter type checking; implicit conversions (string↔numeric, timestamp↔string) removed.
- **Detection**: `SemanticException: Cannot convert column`, `TypeError`, unexpected NULL results from comparisons.
- **Fix**: Add explicit `CAST(col AS <type>)`. Workaround: `SET hive.strict.checks.type.safety=false;`

### HIVE3_EXECUTION_ENGINE

- **Cause**: MapReduce execution engine deprecated in Hive 3.x; `hive.execution.engine=mr` may produce errors or degraded performance.
- **Detection**: Slow execution, warnings about deprecated MR engine, `FAILED: Execution Error` from MR tasks.
- **Fix**: `SET hive.execution.engine=tez;` — this is the default on EMR 7.x. Remove any `hive.execution.engine=mr` from configurations.

### HIVE3_MERGE_STATEMENT

- **Cause**: Hive 3 MERGE syntax differs from Hive 2 workarounds.
- **Detection**: `ParseException` on MERGE statements, unexpected behavior in upsert patterns.
- **Fix**: Use standard Hive 3 MERGE: `MERGE INTO target USING source ON condition WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...`

### HIVE_METASTORE_SCHEMA

- **Cause**: Hive 3.x metastore schema incompatible with 2.x.
- **Detection**: `MetaException` on schema validation, connection errors to HMS.
- **Fix**: Glue Catalog: no action (fully managed). External HMS: `schematool -upgradeSchema -dbType <type>`.

### HIVE2_ACID_DELTA_FORMAT_INCOMPATIBLE

- **Cause**: Hive 2.x ACID tables store data in ORC delta files (`delta_NNNNNN_NNNNNN/bucket_NNNNN`) that use a file format incompatible with Hive 3.x. When migrating to EMR 7.x, recreating the table at the same S3 LOCATION and running `MSCK REPAIR TABLE` discovers the partitions but **data is invisible** — `SELECT *` returns zero rows. This is because Hive 3.x cannot read the Hive 2.x delta file layout; it expects either base files produced by major compaction or the Hive 3.x delta format.
- **Detection**:
  - `SELECT * FROM table` returns 0 rows on EMR 7.x despite partitions being present
  - `MSCK REPAIR TABLE` successfully adds partitions but data is empty
  - S3 listing shows `delta_NNNNNN_NNNNNN/` directories (not `base_NNNNNN/`) under partition paths
  - Table has `transactional=true` in TBLPROPERTIES
  - Source cluster was EMR 5.x with `hive.txn.manager=org.apache.hadoop.hive.ql.lockmgr.DbTxnManager`
- **Root Cause Detail**: Hive 2.x ACID writes produce delta files only (no base file). Hive 3.x changed the internal transaction file format — it can read Hive 2.x **base files** (produced by major compaction) but NOT raw Hive 2.x delta files. The metastore schema upgrade (`schematool -upgradeSchema`) fixes the metadata schema but does **NOT** fix the on-disk ORC delta file format — this is a common misconception.
- **Fix**:

  **Option A — Major compaction on source cluster BEFORE migration (Preferred)**:

  Run major compaction on the EMR 5.x cluster for ALL ACID tables before terminating it. This converts delta files into base files that Hive 3.x can read.

  ```sql
  -- On EMR 5.x cluster (source)
  -- 1. Identify all ACID tables
  SELECT TBL_NAME, DB.NAME as DB_NAME
  FROM TBLS t
  JOIN DBS DB ON t.DB_ID = DB.DB_ID
  JOIN TABLE_PARAMS tp ON t.TBL_ID = tp.TBL_ID
  WHERE tp.PARAM_KEY = 'transactional' AND tp.PARAM_VALUE = 'true';

  -- 2. For each ACID table, run major compaction on every partition
  ALTER TABLE db.table_name PARTITION (partition_col='value') COMPACT 'major';

  -- For unpartitioned tables:
  ALTER TABLE db.table_name COMPACT 'major';

  -- 3. Monitor compaction progress (wait for all to complete)
  SHOW COMPACTIONS;
  -- All entries should show state = 'succeeded'

  -- 4. Verify base files exist in S3
  -- Should see base_NNNNNN/ directories alongside or replacing delta_NNNNNN/ directories
  ```

  After compaction completes, the data is in base files readable by Hive 3.x. Proceed with normal migration.

  **Option B — Re-ingestion via temporary EMR 5.x cluster (when source is terminated)**:

  If the original EMR 5.x cluster has already been terminated and data exists only as delta files in S3:

  ```sql
  -- 1. Launch a TEMPORARY EMR 5.x cluster with same Hive config
  --    (hive.txn.manager=DbTxnManager, same external metastore)

  -- 2. On the temporary EMR 5.x cluster, export data to a new non-ACID location:
  CREATE EXTERNAL TABLE db.table_name_export
  STORED AS ORC
  LOCATION 's3://bucket/migration-export/table_name/'
  AS SELECT * FROM db.table_name;

  -- 3. On EMR 7.x cluster, create table pointing to exported data:
  CREATE EXTERNAL TABLE db.table_name
  (... schema ...)
  STORED AS ORC
  LOCATION 's3://bucket/migration-export/table_name/'
  TBLPROPERTIES ('external.table.purge'='true');

  -- 4. Verify data is accessible:
  SELECT COUNT(*) FROM db.table_name;
  ```

  **Option C — Full re-ingestion from upstream source**:

  If data can be regenerated from upstream (source of truth), re-run the ingestion pipeline on the EMR 7.x cluster. This produces data natively in Hive 3.x format.

- **Important Notes**:
  - `schematool -upgradeSchema` upgrades the **metastore schema** (MySQL/PostgreSQL tables) but does NOT convert the **ORC delta file format** — both steps are needed for ACID table migration
  - AWS Glue Data Catalog does NOT support Hive ACID transactions — if customer uses Glue Catalog, ACID tables must be converted to EXTERNAL tables regardless
  - Non-ACID tables (no `transactional=true`) work fine without compaction — their ORC files are standard and readable by any Hive version
  - The compaction process can be time-consuming for large tables; plan for adequate cluster runtime
  - After successful compaction, delta files can optionally be cleaned via `CLEANER` (runs automatically after compaction succeeds, or manually: keep cluster running until `SHOW COMPACTIONS` shows cleaner state = 'succeeded')

---

## Presto → Trino

### PRESTO_TO_TRINO_RENAME

- **Cause**: Complete rebrand in EMR 7.x — Presto removed, Trino is the replacement.
- **Detection**: `presto-cli: command not found`, `ClassNotFoundException: com.facebook.presto.jdbc.PrestoDriver`, `No such file or directory: /usr/bin/presto-cli`, JDBC connection refused on presto port.
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
  - `json_extract` returns JSON type — wrap with `CAST(json_extract(...) AS VARCHAR)` if string expected
  - Remove `presto.` prefix from session properties (e.g., `presto.optimize_hash_generation` → `optimize_hash_generation`)
  - `APPROX_DISTINCT` behavior unchanged but `APPROX_PERCENTILE` signature may differ

### TRINO_CONNECTOR_CONFIG

- **Cause**: Connector configuration properties renamed between Presto and Trino.
- **Detection**: `Catalog not found`, connector fails to initialize, `Unknown connector` errors.
- **Fix**:
  - Verify catalog properties files updated from Presto to Trino format
  - `hive.metastore.uri` unchanged; `hive.s3.endpoint` → verify still valid
  - Custom connectors must be recompiled against Trino SPI (not Presto SPI)

---

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

---

## Pig → PySpark Conversion

### PIG_JAVA17_SERIALIZATION_FAILURE

- **Cause**: Pig 0.17.0 on EMR 7.x (Java 17) has a fatal serialization bug. ORDER BY, JOIN, and COGROUP operations fail because Pig's internal `OperatorKey` objects cannot be deserialized in downstream Tez vertices. Simple single-vertex operations (LOAD, FILTER, GROUP BY + DUMP) may still work, but any production script with sorting or joins will crash. This is a bug in Pig's engine, not in the user's script — **no modification to the `.pig` file can fix it**. Pig 0.17.0 (2017) is the final Apache Pig release and will never be patched for Java 17.
- **Logs**: `java.io.IOException: Deserialization error: Cannot invoke "org.apache.pig.impl.plan.OperatorKey.hashCode()" because "this.mKey" is null` at `org.apache.pig.impl.util.ObjectSerializer.deserialize(ObjectSerializer.java:62)`, `org.apache.pig.backend.hadoop.executionengine.tez.runtime.PigProcessor.initialize(PigProcessor.java:174)`
- **Fix**: Convert to PySpark. Use Stage 3F (PigToSparkConversion MCP) for automated conversion, or manually convert Pig scripts to PySpark DataFrame API. See `references/pig-to-spark-mapping.md`. There is no Pig-side workaround.

### PIG_UDF_UNMAPPED

- **Cause**: Pig script uses a custom or uncommon UDF with no automatic PySpark equivalent.
- **Detection**: Conversion tool reports "unknown UDF", or converted PySpark raises `NameError`/`AttributeError` for unmapped function.
- **Fix**:
  - Check if UDF is in the `pig_udfs.py` library (common UDFs already mapped)
  - For custom Java UDFs: rewrite as Python `@F.udf` or use `spark._jvm` bridge
  - For built-in Pig functions: map to PySpark equivalents (TOKENIZE→`F.split`, SIZE→`F.size`, FLATTEN→`.explode()`)
  - Flag for manual review if UDF has complex logic

### PIG_SCHEMA_MISMATCH

- **Cause**: Converted PySpark produces different schema or data than original Pig output.
- **Detection**: DataComPy validation shows column differences, type mismatches, or row count discrepancies >1%.
- **Fix**:
  - Compare schemas: check that column names and types match (Pig `chararray`→`StringType`, `int`→`IntegerType`)
  - Check null handling: Pig and Spark handle nulls differently in JOINs and aggregations
  - Verify FLATTEN behavior: Pig FLATTEN on empty bags produces no rows; Spark `.explode()` on null arrays produces no rows (same behavior) but on empty arrays produces no rows
  - Use `apply_conversion_fixes_tool` to iteratively correct the conversion

### PIG_COGROUP_COMPLEX

- **Cause**: Pig COGROUP with multiple relations and nested FOREACH produces complex conversion.
- **Detection**: Converted PySpark raises `AnalysisException`, produces incorrect join results, or has missing columns after nested operations.
- **Fix**:
  - COGROUP with 2+ relations: convert to chained `.join()` with appropriate join types
  - Nested FOREACH inside COGROUP: flatten into `.groupBy().agg()` with multiple aggregation expressions
  - COGROUP with FLATTEN: may need `.explode()` followed by `.groupBy()`
  - If automated fix fails after 5 iterations: flag for manual review with source Pig reference

### PIG_NESTED_FOREACH

- **Cause**: Pig nested FOREACH blocks (block-style with `{...}` operators) are difficult to express as linear DataFrame operations.
- **Detection**: Conversion produces syntactically invalid PySpark, or runtime `AnalysisException` / incorrect results from nested bag operations.
- **Fix**:
  - Simple nesting (FILTER within FOREACH): convert to `.filter()` before `.groupBy()`
  - Complex nesting (multiple operations on inner bag): use window functions (`F.window`) or `.flatMap()` with row-level UDF
  - Nested DISTINCT/ORDER: convert to `F.collect_set()` or `F.sort_array(F.collect_list(...))`
  - Flag for manual review if >3 levels of nesting

### PIG_STREAMING_OPERATOR

- **Cause**: Pig STREAM operator (pipes data through external process) has no direct DataFrame equivalent.
- **Detection**: `pig_ast_parser_tool` flags STREAM statement; conversion tool skips or produces placeholder.
- **Fix**:
  - If streaming process is a simple text transform: convert to PySpark UDF or `rdd.pipe()`
  - If streaming process is a complex binary: use `spark.sparkContext.pipe()` or `subprocess` in a mapPartitions UDF
  - If streaming is for ML inference: consider replacing with Spark MLlib or SageMaker endpoint
  - **Flag as manual remediation** — automated conversion not reliable

### PIG_PARAMETER_SUBSTITUTION

- **Cause**: Pig `%default` and `$param` substitutions not converted to Spark equivalents.
- **Detection**: Converted PySpark contains literal `$param` strings or `%default` markers; runtime `NameError`.
- **Fix**:
  - `$param` → function arguments or `spark.conf.get("spark.app.param")`
  - `%default param value` → Python default arguments: `param = spark.conf.get("spark.app.param", "value")`
  - For Airflow integration: use Jinja templates `{{ params.param }}` in the DAG step definition

---

## Zeppelin Notebook Migration

### ZEPPELIN_PIG_INTERPRETER_REMOVED

- **Cause**: The `%pig` interpreter is **fully removed** from Zeppelin 0.11.1 (EMR 7.5+). No Pig interpreter JAR or directory exists. **Validated**: Testing confirmed conversion from `%pig` to `%pyspark` is correct and necessary — converted paragraphs execute successfully on EMR 7.5 Zeppelin.
- **Detection**: Notebook paragraph shows `Interpreter pig not found`, `InterpreterNotFoundException`.
- **Fix**: Convert `%pig` paragraphs to `%pyspark` using Pig→PySpark conversion rules (Stage 3F/3G). Replace:
  - `DUMP x` → `x.show()` or `display(x)`
  - `DESCRIBE x` → `x.printSchema()`
  - `ILLUSTRATE x` → `x.show(5, truncate=False)`
  - All Pig Latin statements → PySpark DataFrame operations

### ZEPPELIN_SHELL_INTERPRETER_REMOVED

- **Cause**: The `%sh` shell interpreter is **completely removed** from Zeppelin 0.11.1 (EMR 7.5+). There is no JAR, no directory at `/usr/lib/zeppelin/interpreter/sh/`, and no registration in `interpreter.json`. It cannot be re-enabled via settings, API, or bootstrap action. **Validated**: Testing confirmed the interpreter directory does not exist and the Zeppelin REST API reports 0 shell interpreters.
- **Detection**: Notebook paragraph shows `Interpreter sh not found`, `InterpreterNotFoundException`.
- **Fix**: Convert `%sh` paragraphs to `%python` using `subprocess`:
  ```python
  %python
  import subprocess
  result = subprocess.run(['command', 'args'], capture_output=True, text=True)
  print(result.stdout)
  if result.returncode != 0:
      print(f"ERROR: {result.stderr}")
  ```
  Also apply AL2023 command fixes within the subprocess calls: `yum`→`dnf`, `python`→`python3`, IMDSv1→IMDSv2.

### ZEPPELIN_SPARK_API_DEPRECATED

- **Cause**: `%spark` / `%pyspark` paragraphs use Spark 2.x APIs removed in 3.5.
- **Detection**: `AttributeError: 'SparkSession' has no attribute 'x'`, `NameError: name 'sqlContext' is not defined`, `NoSuchMethodError`.
- **Fix**:
  - `sqlContext` → `spark` (SparkSession is pre-bound in EMR 7.x Zeppelin)
  - `sc.parallelize` still works; `SQLContext(sc)` → remove (use `spark` directly)
  - `df.registerTempTable` → `df.createOrReplaceTempView`
  - `from pyspark.mllib` → `from pyspark.ml`
  - Python 2 print/except syntax → Python 3

### ZEPPELIN_PYTHON2_SYNTAX

- **Cause**: `%pyspark` paragraphs contain Python 2 syntax; EMR 7.x uses Python 3 only.
- **Detection**: `SyntaxError: Missing parentheses in call to 'print'`, `SyntaxError: invalid syntax` on except clauses.
- **Fix**:
  - `print "x"` → `print("x")`
  - `except Exception, e:` → `except Exception as e:`
  - `dict.iteritems()` → `dict.items()`
  - `unicode(x)` → `str(x)`
  - `xrange` → `range`

### ZEPPELIN_HIVE3_INCOMPATIBLE

- **Cause**: `%hive` / `%jdbc(hive)` paragraphs contain Hive 2.x syntax incompatible with Hive 3.1.
- **Detection**: Same errors as HIVE3_* categories but occurring within Zeppelin notebook execution.
- **Fix**: Apply same fixes as HIVE3_ACID_DEFAULT, HIVE3_SYNTAX_CHANGES, HIVE3_TYPE_CONVERSION within the notebook paragraphs.

### ZEPPELIN_INTERPRETER_BINDING

- **Cause**: Notebook references interpreter groups that don't exist or are misconfigured on EMR 7.x.
- **Detection**: `InterpreterNotFoundException`, `Interpreter setting not found`, notebook fails to bind interpreter on open.
- **Fix**:
  - Remove Pig interpreter binding from notebook JSON (`interpreterBindings` array)
  - Verify `spark`, `jdbc`, `sh`, `md` interpreters are bound
  - Update interpreter config if `%jdbc(hive)` connection string needs Hive 3 JDBC URL

### ZEPPELIN_S3_NOTEBOOK_STORAGE

- **Cause**: Notebook storage config references old S3 paths or uses deprecated storage class.
- **Detection**: Notebooks not visible after cluster creation, `NotebookRepoException`, `S3Exception`.
- **Fix**:
  - Verify `zeppelin.notebook.s3.bucket` and `zeppelin.notebook.s3.user` in `zeppelin-site` classification
  - Update `zeppelin.notebook.storage` class if using deprecated implementation
  - Ensure IAM role has `s3:GetObject`/`s3:PutObject` on notebook bucket

---

## Removed Applications (Other)

### OOZIE_DEPRECATED

- **Cause**: Oozie 5.2.1 is still available on EMR 7.x but is unmaintained and deprecated. It may be removed in future EMR releases. Workflows relying on Oozie should plan migration to modern alternatives.
- **Logs**: No immediate failure — Oozie runs but receives no updates or patches. May encounter issues with newer Hadoop 3.x APIs over time.
- **Detection**: Cluster includes Oozie in applications list; scheduled workflows use `oozie job -run` or Oozie coordinator/bundle definitions.
- **Fix**: Migrate to Step Functions (recommended), MWAA (Managed Airflow), or EMR Steps + EventBridge for orchestration. **Proactive redesign recommended** — Oozie will not receive compatibility fixes for future EMR releases.

---

## Flink (1.x → 1.18)

### FLINK_YARN_CHANGES

- **Cause**: Per-job deployment mode (`-m yarn-cluster`) deprecated in Flink 1.15, effectively removed in favor of application mode.
- **Detection**: `IllegalArgumentException: Unknown execution target`, `Could not find a valid target`, YARN session start failure, `Deployment mode is not supported`.
- **Fix**:
  - `flink run -m yarn-cluster <jar>` → `flink run-application -t yarn-application <jar>`
  - `flink run -m yarn-cluster -yn 4` → `flink run-application -t yarn-application -Dtaskmanager.numberOfTaskSlots=4`
  - For session mode: `yarn-session.sh` launch unchanged, then `flink run -t yarn-session`

### FLINK_MEMORY_MODEL

- **Cause**: Flink 1.10+ unified memory model replaces legacy heap-based config.
- **Detection**: `IllegalConfigurationException` referencing memory, `TaskManager failed to start`, `OutOfMemoryError` with Flink metaspace.
- **Fix**:

| Legacy (Flink 1.x) | New (Flink 1.10+) |
|-----|--------|
| `taskmanager.heap.mb=4096` | `taskmanager.memory.process.size=4096m` |
| `jobmanager.heap.mb=2048` | `jobmanager.memory.process.size=2048m` |
| (none) | `taskmanager.memory.managed.fraction=0.4` |
| (none) | `taskmanager.memory.network.fraction=0.1` |

### FLINK_STATE_BACKEND

- **Cause**: State backend configuration property renamed in Flink 1.13+.
- **Detection**: `Could not find state backend`, `Unknown state backend`, warnings about deprecated config.
- **Fix**:
  - `state.backend: rocksdb` → `state.backend.type: rocksdb`
  - `state.backend: filesystem` → `state.backend.type: hashmap`
  - Verify RocksDB native library loads on AL2023 (x86_64 and ARM compatible)

### FLINK_CONNECTOR_VERSION

- **Cause**: Flink connectors decoupled from Flink core in 1.15+; must use matching connector version.
- **Detection**: `ClassNotFoundException` for connector classes, `NoSuchMethodError` in Kafka/Kinesis connector code.
- **Fix**:
  - Update Flink-Kafka connector to version matching Flink 1.18 (uses Kafka client 3.4+)
  - Update Flink-Kinesis connector: `flink-connector-kinesis` (not legacy `flink-connector-kinesis-streams`)
  - Download updated connector JARs and place in `/usr/lib/flink/lib/` or submit with `-C` classpath

### FLINK_JAVA_COMPATIBILITY

- **Cause**: Flink 1.18 on EMR 7.x runs Java 17; reflection-heavy serialization may break.
- **Detection**: `InaccessibleObjectException`, `IllegalAccessError` in serialization paths, `java.lang.reflect` errors.
- **Fix**: Add to `flink-conf.yaml`:
  ```
  env.java.opts: "--add-opens java.base/java.util=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED"
  ```

---

## Networking / Security

### IMDSV1_DISABLED

- **Cause**: EMR 7.x enforces IMDSv2.
- **Logs**: `Unable to load credentials`, `401 - Unauthorized` from metadata service
- **Fix**: Use IMDSv2 token flow or AWS SDK (auto-handles IMDSv2).

### TLS_VERSION_MINIMUM

- **Cause**: AL2023 requires TLS 1.2+.
- **Logs**: `SSLHandshakeException`, `protocol_version alert`
- **Fix**: Update clients to TLS 1.2+.

### NO_PROXY_REGIONAL_S3_MISMATCH

- **Cause**: Private subnet clusters using HTTP proxy have `NO_PROXY=*.amazonaws.com` but Java's `DefaultProxySelector` treats `*` as matching a **single DNS label only** — it does NOT match across dot separators. Regional S3 endpoints like `s3.us-east-1.amazonaws.com` have multiple labels before `.amazonaws.com`, so they are routed through the proxy instead of the S3 VPC Gateway Endpoint.
- **Detection**: 
  - Cluster launches succeed but are extremely slow (provisioning takes 10+ minutes instead of ~2 minutes)
  - S3 download speeds ~15 MB/s instead of expected ~80+ MB/s
  - Traceroute in instance-state logs shows 6+ hops to S3 (proxy path) instead of 2 hops (VPC endpoint path)
  - Error code `APP_PROVISIONING_FAILED_TIME_OUT` on larger clusters
- **Fix**: Add explicit regional S3 entries to `NO_PROXY` in bootstrap action or custom AMI:
  ```
  s3.<region>.amazonaws.com
  *.s3.<region>.amazonaws.com
  s3.dualstack.<region>.amazonaws.com
  *.s3.dualstack.<region>.amazonaws.com
  ```
  - **Important**: Do not rely on `*.amazonaws.com` alone — it only matches single-label prefixes in Java
  - Also verify S3 VPC Gateway Endpoint exists and is associated with the EMR subnet route tables
  - Note: `curl` and Python `requests` DO match across dots, so testing with those tools will NOT reproduce the Java behavior

### APP_PROVISIONING_TIMEOUT

- **Cause**: EMR 7.x test cluster nodes fail to provision within the dynamically computed timeout. The timeout is calculated from the EMR release label and installed components (e.g., ~710 seconds for Spark+Hive on EMR 6.9.1, varies by release). Common during migration because customers may use custom AMIs, private subnets with proxy, or bootstrap actions adapted from EMR 5.x that take longer on AL2023.
- **Detection**: Cluster terminates with error code `APP_PROVISIONING_FAILED_TIME_OUT`. Log message: `Instance {id} took longer than {timeout} to provision applications hence marking it as failed`.
- **Fix** (investigate in order):
  1. **Slow S3 downloads** (most common): Check NO_PROXY config (see NO_PROXY_REGIONAL_S3_MISMATCH above), verify S3 VPC Gateway Endpoint, check network bandwidth metrics (`bw_in_allowance_exceeded`)
  2. **Bootstrap actions too slow**: AL2023-adapted bootstrap scripts may download large files or run slow operations. Move heavy operations into custom AMI instead. Keep bootstrap actions lightweight.
  3. **Custom AMI issues**: If using custom AMI from EMR 5.x, ensure it's based on AL2023 (not AL1/AL2). EMR 7.x requires AL2023-based AMIs.
  4. **Resource contention**: Use instance types with sufficient network bandwidth (avoid t2/t3 for provisioning-heavy clusters). Larger instance types have higher network baselines.
  5. **Workaround**: For immediate unblocking, reduce cluster size or use larger instance types while root-causing the slow provisioning.

### PRIVATE_SUBNET_CONNECTIVITY

- **Cause**: EMR 7.x test cluster in private subnet cannot reach required endpoints during provisioning or runtime. Common when migrating from public-subnet EMR 5.x clusters to private-subnet EMR 7.x clusters.
- **Detection**: Cluster fails with `APP_PROVISIONING_FAILED_TIME_OUT` or `BOOTSTRAP_FAILURE`. Instance Controller never checks in. Logs show `Connection timed out` to S3 or yum repos.
- **Fix**:
  - Verify route table has routes to: S3 VPC Gateway Endpoint (vpce-xxx), NAT Gateway (nat-xxx) or NAT Instance (for non-S3 traffic)
  - Verify EMR-managed security groups allow ALL egress (port 0, protocol -1, destination 0.0.0.0/0)
  - Verify DNS resolution works (VPC must have `enableDnsHostnames=true` and `enableDnsSupport=true`)
  - If using VPC endpoints for other services (STS, CloudWatch, etc.), verify they include the correct private DNS settings

---

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
