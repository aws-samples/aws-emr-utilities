# EMR AL1 → AL2023 — Failure Catalogue (Index)

Failure categories for classifying errors during EMR 5.x to 7.x migration. Each domain file contains: category ID, cause, log-based identification, and prescribed fix.

## Domain Files

| Domain | File | Categories |
|--------|------|------------|
| Platform (All Apps) | [`failures/platform.md`](failures/platform.md) | JAVA_VERSION_INCOMPATIBLE, JAVA17_REMOVED_PACKAGES, JAVA17_REFLECTION_DENIED, IMDSV2_METADATA_DENIED, LOG4J_CONFIG_SILENTLY_IGNORED, EMRFS_CONSISTENT_VIEW_REMOVED, GLUE_CATALOG_HIVE3_INCOMPATIBILITY, PYTHON2_REMOVED, BOOTSTRAP_AL2023_COMPAT, YUM_PACKAGE_MISSING, SYSTEMD_SERVICE_CHANGES |
| Hadoop / YARN / MR | [`failures/hadoop-mr.md`](failures/hadoop-mr.md) | HADOOP3_API_BREAK, MAPREDUCE_OLD_API, MAPREDUCE_CLASSPATH, MAPREDUCE_STREAMING_PYTHON, YARN_DEPRECATED_CONFIG, S3_SCHEME_DEPRECATED |
| Spark (2.4→3.5) | [`failures/spark.md`](failures/spark.md) | SPARK_SCALA_BINARY, SPARK_SQL_LEGACY, SPARK_PARQUET_TIMESTAMP, SPARK_DATASOURCE_V2, SPARK_SHUFFLE_CHANGES, SPARK_REMOVED_APIS, SPARK_HIVE_METASTORE, SPARK_PYTHON_VERSION, SPARK_DEPENDENCY_CONFLICT |
| Hive (2.3→3.1) | [`failures/hive.md`](failures/hive.md) | HIVE3_ACID_DEFAULT, HIVE3_MANAGED_TABLE, HIVE3_SYNTAX_CHANGES, HIVE3_TYPE_CONVERSION, HIVE3_EXECUTION_ENGINE, HIVE3_MERGE_STATEMENT, HIVE_METASTORE_SCHEMA, HIVE2_ACID_DELTA_FORMAT_INCOMPATIBLE, HIVE3_CTAS_EXTERNAL |
| Flink (1.x→1.18) | [`failures/flink.md`](failures/flink.md) | FLINK_YARN_CHANGES, FLINK_MEMORY_MODEL, FLINK_STATE_BACKEND, FLINK_CONNECTOR_VERSION, FLINK_JAVA_COMPATIBILITY |
| Pig → PySpark | [`failures/pig.md`](failures/pig.md) | PIG_JAVA17_SERIALIZATION_FAILURE, PIG_UDF_UNMAPPED, PIG_SCHEMA_MISMATCH, PIG_COGROUP_COMPLEX, PIG_NESTED_FOREACH, PIG_STREAMING_OPERATOR, PIG_PARAMETER_SUBSTITUTION |
| Infrastructure / Networking / Security | [`failures/infrastructure.md`](failures/infrastructure.md) | EMR7_RPM_REPO_MISSING, SPARK_CLASSPATH_POISON, EMRFS_TO_S3A_COMMITTER, PRESTO_TO_TRINO_RENAME, TRINO_SQL_CHANGES, TRINO_CONNECTOR_CONFIG, HBASE2_API_BREAK, HBASE2_COPROCESSOR, OOZIE_REMOVED, IMDSV1_DISABLED, TLS_VERSION_MINIMUM, NO_PROXY_REGIONAL_S3_MISMATCH, APP_PROVISIONING_TIMEOUT, PRIVATE_SUBNET_CONNECTIVITY, OOM_RESOURCE, TRANSIENT_INFRA, INSTANCE_TYPE_UNAVAILABLE |

## Quick Lookup

To find a failure category, match the error pattern:

- **Java errors** (`NoSuchMethodError`, `InaccessibleObjectException`, `ClassNotFoundException: javax.*`) → `failures/platform.md`
- **Hadoop/S3/YARN errors** (`No FileSystem for scheme: s3n`, `ClassNotFoundException: org.apache.hadoop.*`) → `failures/hadoop-mr.md`
- **Spark errors** (`AnalysisException`, `SparkUpgradeException`, Scala `AbstractMethodError`) → `failures/spark.md`
- **Hive errors** (`SemanticException`, `ParseException`, ACID/transactional issues) → `failures/hive.md`
- **Flink errors** (`Unknown execution target`, memory config, state backend) → `failures/flink.md`
- **Pig errors** (`OperatorKey.hashCode()`, UDF mapping, schema mismatch) → `failures/pig.md`
- **Cluster launch / networking / provisioning** (`BOOTSTRAP_FAILURE`, `APP_PROVISIONING_FAILED_TIME_OUT`, `401 metadata`) → `failures/infrastructure.md`
- **Presto/Trino** (`presto-cli: command not found`, JDBC driver class) → `failures/infrastructure.md`
- **HBase** (`NoSuchMethodError` for `HBaseAdmin`, `HTable`) → `failures/infrastructure.md`
- **Zeppelin** (`Interpreter not found`, Python 2 syntax in notebooks) → [`zeppelin-interpreter-migration.md`](zeppelin-interpreter-migration.md) (Failure Categories section)
