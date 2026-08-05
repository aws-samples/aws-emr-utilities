# Zeppelin Interpreter Migration — EMR 5.x → EMR 7.x

Reference for migrating Zeppelin notebook paragraphs across interpreter changes between EMR versions.

---

## Interpreter Compatibility Matrix

| Interpreter | EMR 5.x (AL1) | EMR 7.x (AL2023) | Migration Action |
|-------------|---------------|-------------------|-----------------|
| `%spark` | Spark 2.4 (Scala) | Spark 3.5 (Scala) | Update deprecated APIs |
| `%pyspark` / `%spark.pyspark` | PySpark 2.4 + Python 2/3 | PySpark 3.5 + Python 3 only | Update APIs + Python 3 syntax |
| `%spark.sql` / `%sql` | Spark SQL 2.4 | Spark SQL 3.5 | Update syntax (ANSI mode) |
| `%pig` | Pig 0.17 | **REMOVED** | Convert to `%pyspark` |
| `%hive` / `%jdbc(hive)` | Hive 2.3 | Hive 3.1 | Update syntax (see Hive 3 fixes) |
| `%sh` | AL1 shell (bash) | **REMOVED** (no JAR, cannot re-enable) | Convert to `%python` with `subprocess` |
| `%md` | Markdown | Markdown | No changes needed |
| `%angular` | Angular display | Angular display | No changes needed |
| `%r` / `%spark.r` | SparkR | SparkR 3.5 | Update deprecated R APIs |
| `%livy` / `%livy.pyspark` | Livy + Spark 2.4 | Livy + Spark 3.5 | Same as `%pyspark` fixes |

---

## Paragraph Migration Rules

### `%pig` → `%pyspark` (Complete Rewrite Required)

The Pig interpreter is removed from EMR 7.x. Every `%pig` paragraph must be converted to `%pyspark`.

#### Interactive Commands

| Pig (in notebook) | PySpark (replacement) |
|---|---|
| `x = LOAD '...' USING ...` | `x = spark.read.csv('...')` / `spark.table('...')` |
| `DUMP x` | `x.show()` or `display(x)` |
| `DESCRIBE x` | `x.printSchema()` |
| `ILLUSTRATE x` | `x.show(5, truncate=False)` |
| `EXPLAIN x` | `x.explain(True)` |
| `x = FILTER y BY ...` | `x = y.filter(...)` |
| `x = FOREACH y GENERATE ...` | `x = y.select(...)` |
| `x = GROUP y BY col` | `x = y.groupBy('col').agg(...)` |
| `x = JOIN a BY k, b BY k` | `x = a.join(b, a.k == b.k)` |
| `x = ORDER y BY col` | `x = y.orderBy('col')` |
| `x = LIMIT y N` | `x = y.limit(N)` |
| `STORE x INTO '...'` | `x.write.mode('overwrite').save('...')` |

#### Variable Continuity

Pig notebooks often define aliases in one paragraph and reference them in later paragraphs. In PySpark, these become DataFrame variables that persist across cells within the same Spark session:

```python
# Cell 1 — was: data = LOAD '...'; DESCRIBE data;
data = spark.table('db.my_table')
data.printSchema()
```

```python
# Cell 2 — was: filtered = FILTER data BY status == 'active'; DUMP filtered;
filtered = data.filter(F.col('status') == 'active')
filtered.show()
```

#### Pig `%declare` / `%default` in Notebooks

```pig
-- Pig notebook cell with parameters
%default run_date '2024-01-01'
data = LOAD 's3://bucket/dt=$run_date/';
```

```python
# PySpark equivalent
run_date = z.input('run_date', '2024-01-01')  # Zeppelin dynamic form
data = spark.read.parquet(f's3://bucket/dt={run_date}/')
```

---

### `%pyspark` — Spark 2.4 → 3.5 Updates

| EMR 5.x Pattern | EMR 7.x Replacement |
|---|---|
| `from pyspark.sql import SQLContext` | Remove — use `spark` directly |
| `sqlContext = SQLContext(sc)` | Remove — `spark` is pre-bound |
| `sqlContext.sql(...)` | `spark.sql(...)` |
| `sqlContext.read...` | `spark.read...` |
| `df.registerTempTable('name')` | `df.createOrReplaceTempView('name')` |
| `df.unionAll(other)` | `df.union(other)` |
| `from pyspark.mllib.clustering import KMeans` | `from pyspark.ml.clustering import KMeans` |
| `from pyspark.mllib.feature import HashingTF` | `from pyspark.ml.feature import HashingTF` |
| `sc.textFile('s3n://...')` | `sc.textFile('s3://...')` |
| `spark.read.load('s3n://...')` | `spark.read.load('s3://...')` |
| `print x` | `print(x)` |
| `except Exception, e:` | `except Exception as e:` |
| `dict.iteritems()` | `dict.items()` |
| `dict.has_key(k)` | `k in dict` |
| `unicode(x)` | `str(x)` |
| `xrange(n)` | `range(n)` |

---

### `%spark.sql` — Spark SQL 2.4 → 3.5 Updates

| EMR 5.x Pattern | EMR 7.x Replacement | Notes |
|---|---|---|
| Implicit string-to-numeric casts | Add explicit `CAST(col AS type)` | ANSI mode stricter |
| `SELECT approxCountDistinct(col)` | `SELECT approx_count_distinct(col)` | Function renamed |
| `INSERT OVERWRITE LOCAL DIRECTORY` | May need `spark.sql.legacy.allowNonEmptyLocationInCTAS=true` | |
| Division returning int (5/2=2) | Returns double (5/2=2.5) | Use `CAST` or `DIV` for integer division |

---

### `%hive` / `%jdbc(hive)` — Hive 2.3 → 3.1 Updates

| EMR 5.x Pattern | EMR 7.x Replacement | Notes |
|---|---|---|
| `CREATE TABLE ...` (unqualified) | `CREATE EXTERNAL TABLE ...` | Hive 3 defaults to ACID managed tables |
| `INSERT OVERWRITE TABLE managed_t` | Convert table to EXTERNAL first | ACID tables need MERGE or EXTERNAL |
| Unquoted `date`, `time`, `user` | Backtick-quote: `` `date` `` | New reserved keywords |
| `SET hive.execution.engine=mr` | Remove (Tez is default and preferred) | |
| `SET hive.create.as.acid=false` | Remove — property doesn't exist on EMR 7.x | |
| Implicit type conversions | Add `CAST(col AS type)` | Stricter type checking |

---

### `%sh` → `%python` with `subprocess` (REMOVED — Complete Conversion Required)

The `%sh` shell interpreter is **completely removed** from Zeppelin 0.11.1 (EMR 7.5+). There is no JAR at `/usr/lib/zeppelin/interpreter/sh/`, no directory, and no registration in `interpreter.json`. It cannot be re-enabled via interpreter settings, API, or bootstrap action. This was validated via testing on EMR 7.5.

**Convert all `%sh` paragraphs to `%python` using `subprocess`:**

```python
%python
import subprocess

# Example: run a shell command
result = subprocess.run(['dnf', 'list', 'installed'], capture_output=True, text=True)
print(result.stdout)
if result.returncode != 0:
    print(f"ERROR: {result.stderr}")
```

**Apply AL2023 command changes within the subprocess calls:**

| EMR 5.x Command | EMR 7.x Equivalent (in subprocess) | Notes |
|---|---|---|
| `yum install -y pkg` | `['dnf', 'install', '-y', 'pkg']` | |
| `yum list installed` | `['dnf', 'list', 'installed']` | |
| `service x start` | `['systemctl', 'start', 'x']` | |
| `service x status` | `['systemctl', 'status', 'x']` | |
| `chkconfig x on` | `['systemctl', 'enable', 'x']` | |
| `python script.py` | `['python3', 'script.py']` | |
| `pip install pkg` | `['pip3', 'install', 'pkg']` | |
| `/usr/lib/jvm/java-1.8.0-openjdk/...` | `/usr/lib/jvm/java-17-amazon-corretto/...` | |
| `amazon-linux-extras install topic` | `['dnf', 'install', 'pkg']` | AL extras removed |
| `curl http://169.254.169.254/...` | Use IMDSv2 token flow (see below) | IMDSv2 enforced (HTTP 401 without token) |

**IMDSv2 in Python:**
```python
%python
import urllib.request
# Get token
req = urllib.request.Request(
    'http://169.254.169.254/latest/api/token',
    method='PUT',
    headers={'X-aws-ec2-metadata-token-ttl-seconds': '21600'})
token = urllib.request.urlopen(req).read().decode()

# Use token for metadata
req = urllib.request.Request(
    'http://169.254.169.254/latest/meta-data/instance-id',
    headers={'X-aws-ec2-metadata-token': token})
instance_id = urllib.request.urlopen(req).read().decode()
print(f"Instance ID: {instance_id}")
```

---

## Notebook JSON Structure

Zeppelin notebooks are stored as JSON. Key fields to modify during migration:

```json
{
  "paragraphs": [
    {
      "text": "%pig\ndata = LOAD ...",     // ← Change interpreter + content
      "config": { ... },
      "settings": { "params": {}, "forms": {} }
    }
  ],
  "name": "notebook_name",
  "id": "2XXXXX",
  "noteParams": {},
  "noteForms": {},
  "angularObjects": {},
  "config": {
    "isZeppelinNotebookCronEnable": false
  },
  "info": {}
}
```

### Fields to Update

| Field | Change |
|-------|--------|
| `paragraphs[].text` | Update interpreter prefix + code content |
| `config.interpreterBindings` | Remove `pig` interpreter, verify `spark`/`jdbc`/`sh` present |
| `noteParams` | Update any `%default`-style parameters |

---

## Zeppelin Configuration (zeppelin-site classification)

Properties to set/verify in the EMR `zeppelin-site` configuration classification:

| Property | EMR 5.x Value | EMR 7.x Value |
|----------|---------------|---------------|
| `zeppelin.pyspark.python` | `python` or `python3` | `python3` (required) |
| `zeppelin.spark.enableSupportedVersionCheck` | `true` | `true` |
| `zeppelin.interpreter.list` | Includes `pig` | Remove `pig` |
| `zeppelin.notebook.s3.bucket` | Same | Same (verify bucket exists) |
| `zeppelin.notebook.storage` | `org.apache.zeppelin.notebook.repo.S3NotebookRepo` | Same (verify class still exists) |

---

## Zeppelin REST API — Useful Endpoints

| Action | Method + Endpoint |
|--------|------------------|
| List notebooks | `GET /api/notebook` |
| Get notebook | `GET /api/notebook/{id}` |
| Create notebook | `POST /api/notebook` (body: JSON) |
| Import notebook | `POST /api/notebook/import` (body: notebook JSON) |
| Run all paragraphs | `POST /api/notebook/job/{id}` |
| Run single paragraph | `POST /api/notebook/job/{id}/{paragraphId}` |
| Get paragraph status | `GET /api/notebook/job/{id}/{paragraphId}` |
| Delete notebook | `DELETE /api/notebook/{id}` |
| Clone notebook | `POST /api/notebook/{id}` (body: `{"name":"new_name"}`) |

---

## Migration Checklist

For each notebook:

- [ ] Export notebook JSON from source EMR 5.x cluster
- [ ] Identify all `%pig` paragraphs → mark for conversion
- [ ] Identify all `%pyspark` paragraphs → check for Spark 2.x / Python 2 patterns
- [ ] Identify all `%hive` paragraphs → check for Hive 2.x patterns
- [ ] Identify all `%sh` paragraphs → mark for conversion (interpreter fully removed)
- [ ] Convert `%pig` paragraphs to `%pyspark` (use Stage 3F conversion rules)
- [ ] Convert `%sh` paragraphs to `%python` with `subprocess` (interpreter fully removed, cannot re-enable)
- [ ] Update `%pyspark` paragraphs (Spark 3.5 API + Python 3)
- [ ] Update `%hive` paragraphs (Hive 3.1 syntax)
- [ ] Remove Pig interpreter from bindings
- [ ] Upload to target EMR 7.x Zeppelin
- [ ] Execute notebook — verify no ERROR paragraphs
- [ ] Validate output data matches expected results
