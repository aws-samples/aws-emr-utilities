# Pig → PySpark Conversion Failures

### PIG_JAVA17_SERIALIZATION_FAILURE

- **Cause**: Pig 0.17.0 on EMR 7.x (Java 17) has a fatal serialization bug. ORDER BY, JOIN, and COGROUP operations fail because Pig's internal `OperatorKey` objects cannot be deserialized in downstream Tez vertices. Simple single-vertex operations (LOAD, FILTER, GROUP BY + DUMP) may still work, but any production script with sorting or joins will crash. This is a bug in Pig's engine, not in the user's script — **no modification to the `.pig` file can fix it**. Pig 0.17.0 (2017) is the final Apache Pig release and will never be patched for Java 17.
- **Logs**: `java.io.IOException: Deserialization error: Cannot invoke "org.apache.pig.impl.plan.OperatorKey.hashCode()" because "this.mKey" is null` at `org.apache.pig.impl.util.ObjectSerializer.deserialize(ObjectSerializer.java:62)`, `org.apache.pig.backend.hadoop.executionengine.tez.runtime.PigProcessor.initialize(PigProcessor.java:174)`
- **Fix**: Convert to PySpark. Use Stage 3F to convert Pig scripts to PySpark DataFrame API. See `references/pig-to-spark-mapping.md`. There is no Pig-side workaround.

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
  - Verify FLATTEN behavior: Both Pig FLATTEN and Spark `F.explode()` drop the row when the collection is null or empty (`{}` / `[]`), so those cases are consistent. The real divergence is when Pig implicitly creates an empty bag (e.g., from a failed outer join) that the conversion renders as `null` instead of `[]`. When you need to preserve the parent row in that case, use `F.explode_outer()` instead of `F.explode()`.
  - Iteratively correct the conversion by comparing against the original Pig logic and expected output schema

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
- **Detection**: Conversion encounters STREAM statement; conversion produces placeholder or skips the operation.
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
