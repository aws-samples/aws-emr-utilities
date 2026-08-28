# Spark Failures (2.4 → 3.5)

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
  spark.sql.legacy.sizeOfNull=true
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
- **Fix**: Set `spark.driver.userClassPathFirst=true` and `spark.executor.userClassPathFirst=true` in spark-defaults or via `--conf`. If Log4j: rename `log4j.properties` → `log4j2.properties`, update `org.apache.log4j` imports → `org.apache.logging.log4j`.
