# Zeppelin Interpreter Migration — EMR 5.x → EMR 7.x

## Interpreter Compatibility Matrix

| Interpreter | EMR 7.x Status | Migration Action |
|-------------|----------------|-----------------|
| `%spark` / `%pyspark` | Spark 3.5 + Python 3 only | Update APIs + Python 3 syntax |
| `%spark.sql` / `%sql` | Spark SQL 3.5 | Update syntax (ANSI mode stricter) |
| `%pig` | **REMOVED** (no JAR, cannot re-enable) | Convert to `%pyspark` |
| `%hive` / `%jdbc(hive)` | Hive 3.1 | Update syntax (Stage 3B rules) |
| `%sh` | **REMOVED** (no JAR, cannot re-enable) | Convert to `%python` with `subprocess` |
| `%md` / `%angular` | Unchanged | No changes needed |
| `%r` / `%spark.r` | SparkR 3.5 | Update deprecated R APIs |
| `%livy` / `%livy.pyspark` | Livy + Spark 3.5 | Same as `%pyspark` fixes |

---

## `%pig` → `%pyspark` Conversion

Every `%pig` paragraph must be converted to `%pyspark`. Use `references/pig-to-spark-mapping.md` for the full operator mapping.

| Pig (in notebook) | PySpark (replacement) |
|---|---|
| `LOAD '...' USING ...` | `spark.read.csv('...')` / `spark.table('...')` |
| `DUMP x` | `x.show()` |
| `DESCRIBE x` | `x.printSchema()` |
| `ILLUSTRATE x` | `x.show(5, truncate=False)` |
| `EXPLAIN x` | `x.explain(True)` |
| `FILTER y BY ...` | `y.filter(...)` |
| `FOREACH y GENERATE ...` | `y.select(...)` |
| `GROUP y BY col` | `y.groupBy('col').agg(...)` |
| `JOIN a BY k, b BY k` | `a.join(b, a.k == b.k)` |
| `ORDER y BY col` | `y.orderBy('col')` |
| `LIMIT y N` | `y.limit(N)` |
| `STORE x INTO '...'` | `x.write.mode('overwrite').save('...')` |
| `%default param 'val'` | `param = z.input('param', 'val')` |

---

## `%pyspark` — Spark 2.4 → 3.5 Updates

| EMR 5.x Pattern | EMR 7.x Replacement |
|---|---|
| `from pyspark.sql import SQLContext` | Remove — use `spark` directly |
| `sqlContext = SQLContext(sc)` | Remove — `spark` is pre-bound |
| `sqlContext.sql(...)` / `sqlContext.read...` | `spark.sql(...)` / `spark.read...` |
| `df.registerTempTable('name')` | `df.createOrReplaceTempView('name')` |
| `df.unionAll(other)` | `df.union(other)` |
| `from pyspark.mllib.*` | `from pyspark.ml.*` |
| `sc.textFile('s3n://...')` | `sc.textFile('s3://...')` |
| `print x` | `print(x)` |
| `except Exception, e:` | `except Exception as e:` |
| `dict.iteritems()` / `dict.has_key(k)` | `dict.items()` / `k in dict` |
| `unicode(x)` / `xrange(n)` | `str(x)` / `range(n)` |

---

## `%spark.sql` — Spark SQL Updates

| EMR 5.x Pattern | EMR 7.x Replacement |
|---|---|
| Implicit string-to-numeric casts | Add explicit `CAST(col AS type)` |
| `approxCountDistinct(col)` | `approx_count_distinct(col)` |

---

## `%hive` — Hive 2.3 → 3.1 Updates

| EMR 5.x Pattern | EMR 7.x Replacement |
|---|---|
| `CREATE TABLE ...` (unqualified) | `CREATE EXTERNAL TABLE ...` |
| `INSERT OVERWRITE TABLE managed_t` | Convert table to EXTERNAL first |
| Unquoted `date`, `time`, `user` | Backtick-quote: `` `date` `` |
| `SET hive.execution.engine=mr` | Remove |
| `SET hive.create.as.acid=false` | Remove — property doesn't exist on EMR 7.x |
| Implicit type conversions | Add `CAST(col AS type)` |

---

## `%sh` → `%python` with `subprocess`

Convert all `%sh` paragraphs to `%python`:

```python
%python
import subprocess
result = subprocess.run(['dnf', 'list', 'installed'], capture_output=True, text=True)
print(result.stdout)
if result.returncode != 0:
    print(f"ERROR: {result.stderr}")
```

**AL2023 command changes in subprocess:**

| EMR 5.x Command | EMR 7.x Equivalent |
|---|---|
| `yum install -y pkg` | `['dnf', 'install', '-y', 'pkg']` |
| `service x start` / `chkconfig x on` | `['systemctl', 'start', 'x']` / `['systemctl', 'enable', 'x']` |
| `python script.py` / `pip install` | `['python3', 'script.py']` / `['pip3', 'install', 'pkg']` |
| `amazon-linux-extras install topic` | `['dnf', 'install', 'pkg']` |
| `curl http://169.254.169.254/...` | Use IMDSv2 token flow (see Stage 2 in SKILL.md) |

---

## Notebook JSON — Fields to Update

| Field | Change |
|-------|--------|
| `paragraphs[].text` | Update interpreter prefix + code content |
| `config.interpreterBindings` | Remove `pig` and `sh`, verify `spark`/`jdbc`/`python` present |
| `noteParams` | Update any `%default`-style parameters to Zeppelin forms |

---

## Zeppelin Configuration (zeppelin-site classification)

| Property | EMR 7.x Value |
|----------|---------------|
| `zeppelin.pyspark.python` | `python3` (required) |
| `zeppelin.interpreter.list` | Remove `pig` |
| `zeppelin.notebook.s3.bucket` | Verify bucket exists |

---

## Zeppelin REST API

| Action | Endpoint |
|--------|----------|
| List notebooks | `GET /api/notebook` |
| Get notebook | `GET /api/notebook/{id}` |
| Import notebook | `POST /api/notebook/import` |
| Run all paragraphs | `POST /api/notebook/job/{id}` |
| Run single paragraph | `POST /api/notebook/job/{id}/{paragraphId}` |
| Get paragraph status | `GET /api/notebook/job/{id}/{paragraphId}` |

---

## Failure Categories

### ZEPPELIN_PIG_INTERPRETER_REMOVED
- **Detection**: `Interpreter pig not found`, `InterpreterNotFoundException`
- **Fix**: Convert `%pig` paragraphs to `%pyspark` (see conversion table above)

### ZEPPELIN_SHELL_INTERPRETER_REMOVED
- **Detection**: `Interpreter sh not found`, `InterpreterNotFoundException`
- **Fix**: Convert to `%python` with `subprocess` (see section above)

### ZEPPELIN_SPARK_API_DEPRECATED
- **Detection**: `AttributeError`, `NameError: name 'sqlContext' is not defined`
- **Fix**: Apply `%pyspark` updates table above

### ZEPPELIN_PYTHON2_SYNTAX
- **Detection**: `SyntaxError: Missing parentheses in call to 'print'`
- **Fix**: Apply Python 3 fixes from `%pyspark` updates table

### ZEPPELIN_HIVE3_INCOMPATIBLE
- **Detection**: Same errors as HIVE3_* categories in Zeppelin context
- **Fix**: Apply `%hive` updates table above

### ZEPPELIN_INTERPRETER_BINDING
- **Detection**: `InterpreterNotFoundException`, `Interpreter setting not found`
- **Fix**: Remove `pig`/`sh` from `config.interpreterBindings`, verify `spark`/`jdbc`/`python` present

### ZEPPELIN_S3_NOTEBOOK_STORAGE
- **Detection**: Notebooks not visible after cluster creation, `NotebookRepoException`
- **Fix**: Verify `zeppelin.notebook.s3.bucket` in `zeppelin-site` classification
