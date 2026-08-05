# Removed Applications — Migration Paths

Applications removed from EMR 7.x and recommended migration strategies.

---

## Apache Pig (Broken on EMR 7.x — Conversion Required)

Pig 0.17.0 is still installable on EMR 7.x but is **functionally broken** at runtime. While simple single-vertex operations (LOAD, FILTER, GROUP BY + DUMP) may still execute, any script requiring multi-vertex Tez DAGs — including ORDER BY, JOIN, and COGROUP — fails with a fatal Java 17 serialization error:

```
java.io.IOException: Deserialization error: Cannot invoke
"org.apache.pig.impl.plan.OperatorKey.hashCode()" because "this.mKey" is null
    at org.apache.pig.impl.util.ObjectSerializer.deserialize(ObjectSerializer.java:62)
    at org.apache.pig.backend.hadoop.executionengine.tez.runtime.PigProcessor.initialize(PigProcessor.java:174)
```

**This cannot be fixed by modifying the Pig script.** The failure is inside Pig's own engine code (`PigProcessor`/`ObjectSerializer`), not in user-written Pig Latin. Pig 0.17.0 (released June 2017) is the final Apache Pig release — there will be no 0.18.0 with a Java 17 fix. AWS has not patched their bundled version. The only migration path is converting to PySpark.

**This skill handles Pig migration automatically via Stage 3F** using the PigToSparkConversion MCP server (`code.amazon.com/packages/PigToSparkConversion`). The conversion is AST-based with iterative fix loops, test generation, and DataComPy validation.

### Automated Conversion (Stage 3F — Preferred)

When the PigToSparkConversion MCP server is connected, the skill:
1. Extracts Pig scripts from Airflow DAGs automatically
2. Parses each script into an AST with full dependency analysis
3. Generates PySpark classes with proper DataFrame operations
4. Maps Pig UDFs to the `pig_udfs.py` library
5. Generates pytest unit tests for each converted class
6. Produces replacement Airflow DAGs using `SparkLivyBatchOperator`
7. Creates Zeppelin validation notebooks with DataComPy comparison
8. Iterates with fix loops (up to 5) until tests pass

### Manual Conversion Fallback

For cases where automated conversion cannot handle the script (STREAM operator, complex Java UDFs, deeply nested FOREACH), use this quick reference:

| Pig Latin | PySpark |
|-----------|---------|
| `A = LOAD 's3://bucket/path' USING PigStorage(',')` | `df = spark.read.csv('s3://bucket/path')` |
| `B = FILTER A BY col1 > 10` | `df_filtered = df.filter(df.col1 > 10)` |
| `C = GROUP B BY col2` | `df_grouped = df_filtered.groupBy('col2')` |
| `D = FOREACH C GENERATE group, COUNT(B)` | `df_result = df_grouped.agg(count('*'))` |
| `STORE D INTO 's3://output'` | `df_result.write.csv('s3://output')` |
| `E = JOIN A BY id, B BY id` | `df_joined = dfA.join(dfB, 'id')` |
| `F = DISTINCT A` | `df.distinct()` |
| `G = ORDER A BY col1 DESC` | `df.orderBy(desc('col1'))` |
| `H = LIMIT A 100` | `df.limit(100)` |
| `I = UNION A, B` | `dfA.union(dfB)` |
| `J = COGROUP A BY x, B BY y` | `dfA.join(dfB, dfA.x == dfB.y, 'full_outer').groupBy(...)` |
| `SPLIT A INTO B IF col>10, C IF col<=10` | `B = A.filter(col>10); C = A.filter(col<=10)` |
| `FLATTEN(bag)` | `.select(F.explode(col))` |
| `STREAM x THROUGH cmd` | `rdd.pipe(cmd)` or mapPartitions with subprocess |

### Halt conditions for Pig conversion

| Condition | Action |
|-----------|--------|
| STREAM operator | Flag for manual conversion; skip script |
| Custom Java UDF with no Python equivalent | Flag for manual rewrite; continue other scripts |
| DataComPy shows >20% row mismatch | Halt domain, report discrepancies |
| Nested FOREACH >3 levels | Attempt conversion, flag for manual review if fix loop exhausted |

See `references/pig-to-spark-mapping.md` for the complete operator mapping reference.

---

## Apache Oozie (Removed)

Oozie is not available in EMR 7.x. Workflow orchestration must move to a supported alternative.

### Option A: AWS Step Functions (Recommended)

Best for: event-driven workflows, branching logic, error handling, visual debugging.

| Oozie Concept | Step Functions Equivalent |
|---|---|
| Workflow XML | Amazon States Language (ASL) JSON |
| `<action>` (Spark/Hive) | `Task` state with EMR `AddStep` API |
| `<fork>` / `<join>` | `Parallel` state |
| `<decision>` | `Choice` state |
| `<kill>` | `Fail` state |
| Coordinator (scheduled) | EventBridge rule triggering Step Function |
| Bundle | Nested Step Functions or parallel executions |

**Migration approach:**
```bash
# Example: Oozie action → Step Functions EMR step
aws stepfunctions create-state-machine \
  --name "migrated-workflow" \
  --definition file://state-machine.json \
  --role-arn arn:aws:iam::ACCOUNT:role/StepFunctionsEMRRole
```

### Option B: Apache Airflow on Amazon MWAA

Best for: complex DAGs, existing Airflow expertise, Python-native teams.

| Oozie Concept | Airflow Equivalent |
|---|---|
| Workflow XML | DAG Python file |
| `<action>` | Operator (e.g., `EmrAddStepsOperator`) |
| `<fork>` / `<join>` | Parallel task groups |
| `<decision>` | `BranchPythonOperator` |
| Coordinator | DAG schedule interval |
| Bundle | Multiple DAGs or SubDAGs |

### Option C: EMR Steps + EventBridge

Best for: simple sequential workflows with time-based triggers.

```bash
# Scheduled EMR step execution via EventBridge
aws events put-rule \
  --name "daily-spark-job" \
  --schedule-expression "cron(0 6 * * ? *)"

aws events put-targets \
  --rule "daily-spark-job" \
  --targets Id=emr-step,Arn=arn:aws:elasticmapreduce:REGION:ACCOUNT:cluster/CLUSTER_ID,...
```

### Conversion Strategy

1. Inventory all Oozie workflows (workflow.xml, coordinator.xml, bundle.xml).
2. Map each action to equivalent in chosen orchestrator.
3. Identify shared libraries and credentials (Oozie sharelib) → replace with EMR step dependencies.
4. Convert scheduling (coordinators) → EventBridge rules or Airflow schedules.
5. Test end-to-end flow in non-production.

**This skill flags Oozie as a hard blocker requiring manual redesign.** Automated Oozie-to-Step Functions conversion is not reliable for production workflows.

---

## Ganglia (Removed)

Ganglia monitoring is not available in EMR 7.x.

### Replacement: CloudWatch + Prometheus

EMR 7.x publishes metrics natively to CloudWatch. For Ganglia-equivalent granularity:

1. **CloudWatch Metrics** (built-in): YARN, HDFS, Spark metrics published automatically.
2. **Prometheus** (optional): EMR supports Prometheus endpoint scraping for custom metrics.

```bash
# Enable CloudWatch detailed monitoring in EMR configuration
aws emr create-cluster ... \
  --configurations '[{"Classification":"spark-metrics","Properties":{"*.sink.CloudWatch.class":"org.apache.spark.metrics.sink.CloudWatchSink"}}]'
```

No migration action required beyond removing Ganglia from the applications list and updating dashboards.
