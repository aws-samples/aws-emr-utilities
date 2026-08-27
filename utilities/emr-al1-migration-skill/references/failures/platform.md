# Platform-Level Failures (All Applications)

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

- **Cause**: EMR 7.5 on AL2023 enforces IMDSv2 (`HttpTokens=required`). Code that calls the instance metadata service without an IMDSv2 session token receives HTTP 401. **Validated**: bootstrap action testing confirmed IMDSv1 returns HTTP 401 on all EMR 7.5 nodes (primary + core).
- **Logs**: `HTTP 401` from `169.254.169.254`, `Unable to retrieve credentials`, `metadata service returned 401`, custom scripts returning empty/null for instance metadata, bootstrap actions failing silently when metadata calls return empty
- **Detection**: Scan bootstrap actions and custom scripts for: `curl.*169.254.169.254` (without token header), `wget.*169.254.169.254`, `ec2-metadata` (deprecated CLI). Also check application code that reads instance identity for logging/tagging.
- **Fix**:
  - Update shell scripts:
    ```bash
    TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
    curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id
    ```
  - Update AWS SDK: Upgrade to SDK for Java v1 >= 1.11.678, or SDK v2 (both handle IMDSv2 automatically)
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
  - Set `hive.create.as.acid=false` and `hive.create.as.insert.only=false` in the EMR `hive-site` **classification at cluster launch time** — this is safe because EMR injects these into `hive-site.xml` before Hive starts
  - **Do NOT use `SET hive.create.as.acid=false` in HQL scripts** — this fails at runtime with "configuration does not exist" because Hive 3's `SET` command validates property names dynamically and rejects unknown keys. The launch-time classification bypasses this validation.
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
