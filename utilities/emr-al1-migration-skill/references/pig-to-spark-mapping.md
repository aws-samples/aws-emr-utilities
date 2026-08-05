# Pig Latin → PySpark DataFrame API — Complete Mapping

Reference for converting Pig Latin operators, functions, and types to PySpark equivalents.

---

## Relational Operators

| Pig Latin | PySpark | Notes |
|-----------|---------|-------|
| `LOAD 'path' USING PigStorage(',')` | `spark.read.csv('path')` | Use `.option("delimiter", d)` for non-comma |
| `LOAD 'path' USING JsonLoader` | `spark.read.json('path')` | |
| `LOAD 'path' USING ParquetLoader` | `spark.read.parquet('path')` | |
| `LOAD 'path' USING AvroStorage` | `spark.read.format('avro').load('path')` | |
| `LOAD 'path' USING HCatLoader` | `spark.table('db.table')` | Preferred for Hive tables |
| `STORE x INTO 'path'` | `df.write.mode('overwrite').save('path')` | |
| `STORE x INTO 'path' USING PigStorage('\t')` | `df.write.csv('path', sep='\t')` | |
| `DUMP x` | `df.show()` / `display(df)` | Notebook-friendly: `display(df)` |
| `DESCRIBE x` | `df.printSchema()` | |
| `ILLUSTRATE x` | `df.show(5, truncate=False)` | |
| `EXPLAIN x` | `df.explain(True)` | |

---

## Filtering & Projection

| Pig Latin | PySpark | Notes |
|-----------|---------|-------|
| `FILTER x BY condition` | `df.filter(condition)` | |
| `FOREACH x GENERATE col1, col2` | `df.select('col1', 'col2')` | |
| `FOREACH x GENERATE col1 AS alias` | `df.select(F.col('col1').alias('alias'))` | |
| `FOREACH x GENERATE *, expr AS new` | `df.withColumn('new', expr)` | |
| `DISTINCT x` | `df.distinct()` | |
| `LIMIT x N` | `df.limit(N)` | |
| `SAMPLE x 0.1` | `df.sample(0.1)` | |
| `ORDER x BY col ASC` | `df.orderBy('col')` | |
| `ORDER x BY col DESC` | `df.orderBy(F.desc('col'))` | |

---

## Grouping & Aggregation

| Pig Latin | PySpark | Notes |
|-----------|---------|-------|
| `GROUP x BY col` | `df.groupBy('col')` | |
| `GROUP x BY (col1, col2)` | `df.groupBy('col1', 'col2')` | |
| `GROUP x ALL` | `df.agg(...)` | No groupBy — aggregate entire DataFrame |
| `FOREACH grp GENERATE group, COUNT(x)` | `.agg(F.count('*'))` | |
| `FOREACH grp GENERATE group, SUM(x.col)` | `.agg(F.sum('col'))` | |
| `FOREACH grp GENERATE group, AVG(x.col)` | `.agg(F.avg('col'))` | |
| `FOREACH grp GENERATE group, MIN(x.col)` | `.agg(F.min('col'))` | |
| `FOREACH grp GENERATE group, MAX(x.col)` | `.agg(F.max('col'))` | |
| `COGROUP a BY x, b BY y` | See COGROUP section below | |

### COGROUP Conversion

COGROUP groups multiple relations by a common key without flattening. In PySpark:

```python
# Pig: C = COGROUP a BY key, b BY key;
# PySpark equivalent:
a_grouped = a.groupBy('key').agg(F.collect_list(F.struct('*')).alias('a_bag'))
b_grouped = b.groupBy('key').agg(F.collect_list(F.struct('*')).alias('b_bag'))
C = a_grouped.join(b_grouped, 'key', 'full_outer')
```

For simple cases where COGROUP is followed by FLATTEN + aggregation, convert directly to `.join().groupBy().agg()`.

---

## Joins

| Pig Latin | PySpark | Notes |
|-----------|---------|-------|
| `JOIN a BY x, b BY y` | `a.join(b, a.x == b.y, 'inner')` | |
| `JOIN a BY x LEFT OUTER, b BY y` | `a.join(b, a.x == b.y, 'left')` | |
| `JOIN a BY x RIGHT OUTER, b BY y` | `a.join(b, a.x == b.y, 'right')` | |
| `JOIN a BY x FULL OUTER, b BY y` | `a.join(b, a.x == b.y, 'outer')` | |
| `JOIN a BY x, b BY y USING 'replicated'` | `a.join(F.broadcast(b), a.x == b.y)` | Broadcast/map-side join |
| `JOIN a BY x, b BY y USING 'skewed'` | `a.join(b, a.x == b.y)` + AQE | Spark AQE handles skew automatically |
| `CROSS a, b` | `a.crossJoin(b)` | Cartesian product — use sparingly |
| Multi-key: `JOIN a BY (x1,x2), b BY (y1,y2)` | `a.join(b, (a.x1==b.y1) & (a.x2==b.y2))` | |

---

## Set Operations

| Pig Latin | PySpark | Notes |
|-----------|---------|-------|
| `UNION a, b` | `a.union(b)` | Schemas must match |
| `UNION ONSCHEMA a, b` | `a.unionByName(b, allowMissingColumns=True)` | |
| `SPLIT x INTO a IF cond1, b IF cond2` | `a = x.filter(cond1); b = x.filter(cond2)` | |

---

## Nested Operations (FOREACH block)

```pig
-- Pig nested FOREACH
GROUPED = GROUP data BY key;
RESULT = FOREACH GROUPED {
    filtered = FILTER data BY status == 'active';
    sorted = ORDER filtered BY ts DESC;
    top = LIMIT sorted 1;
    GENERATE group, FLATTEN(top);
}
```

```python
# PySpark equivalent using window functions
from pyspark.sql.window import Window

w = Window.partitionBy('key').orderBy(F.desc('ts'))
result = (data
    .filter(F.col('status') == 'active')
    .withColumn('rn', F.row_number().over(w))
    .filter(F.col('rn') == 1)
    .drop('rn'))
```

### Nested Operation Patterns

| Pig Nested Pattern | PySpark Pattern |
|---|---|
| FILTER inside FOREACH | `.filter()` before `.groupBy()`, or window + filter |
| ORDER inside FOREACH | Window with `orderBy` + `row_number` |
| LIMIT inside FOREACH | Window with `row_number().over(w)` + `.filter(rn <= N)` |
| DISTINCT inside FOREACH | `F.collect_set()` in aggregation |
| FLATTEN bag in FOREACH | `F.explode()` on collected array |
| FLATTEN tuple in FOREACH | `df.select('struct_col.*')` |

---

## FLATTEN

| Pig Latin | PySpark | Notes |
|-----------|---------|-------|
| `FLATTEN(bag_col)` | `df.select(F.explode('bag_col').alias('item'))` | One row per bag element |
| `FLATTEN(tuple_col)` | `df.select('tuple_col.*')` | Expands struct to columns |
| `FLATTEN(TOKENIZE(text))` | `df.select(F.explode(F.split('text', ' ')))` | |
| `GENERATE FLATTEN(group)` | `.select('key')` after groupBy | Unpack group key |

---

## Built-in Functions → PySpark

### String Functions

| Pig | PySpark | Import |
|-----|---------|--------|
| `LOWER(s)` | `F.lower(col)` | `from pyspark.sql import functions as F` |
| `UPPER(s)` | `F.upper(col)` | |
| `TRIM(s)` | `F.trim(col)` | |
| `LTRIM(s)` | `F.ltrim(col)` | |
| `RTRIM(s)` | `F.rtrim(col)` | |
| `SUBSTRING(s, start, len)` | `F.substring(col, start, len)` | Pig is 0-indexed; Spark is 1-indexed |
| `INDEXOF(s, search)` | `F.locate(search, col) - 1` | Adjust for 0-index |
| `REPLACE(s, old, new)` | `F.regexp_replace(col, old, new)` | |
| `STRSPLIT(s, regex)` | `F.split(col, regex)` | Returns array |
| `STRSPLIT(s, regex, limit)` | `F.split(col, regex)` | No limit param — use `F.slice` after |
| `CONCAT(a, b)` | `F.concat(col_a, col_b)` | |
| `SPRINTF(fmt, args...)` | `F.format_string(fmt, *args)` | |
| `TOKENIZE(s)` | `F.split(col, '\\s+')` | |
| `REGEX_EXTRACT(s, regex, idx)` | `F.regexp_extract(col, regex, idx)` | |
| `REGEX_EXTRACT_ALL(s, regex)` | UDF or `F.regexp_extract` in loop | No built-in equivalent |
| `SIZE(chararray)` | `F.length(col)` | For strings |

### Numeric Functions

| Pig | PySpark |
|-----|---------|
| `ABS(x)` | `F.abs(col)` |
| `CEIL(x)` | `F.ceil(col)` |
| `FLOOR(x)` | `F.floor(col)` |
| `ROUND(x)` | `F.round(col)` |
| `ROUND(x, n)` | `F.round(col, n)` |
| `RANDOM()` | `F.rand()` |
| `SQRT(x)` | `F.sqrt(col)` |
| `LOG(x)` | `F.log(col)` |
| `EXP(x)` | `F.exp(col)` |

### Date/Time Functions

| Pig | PySpark |
|-----|---------|
| `CurrentTime()` | `F.current_timestamp()` |
| `ToDate(s, fmt)` | `F.to_date(col, fmt)` |
| `ToString(d, fmt)` | `F.date_format(col, fmt)` |
| `GetYear(d)` | `F.year(col)` |
| `GetMonth(d)` | `F.month(col)` |
| `GetDay(d)` | `F.dayofmonth(col)` |
| `GetHour(d)` | `F.hour(col)` |
| `GetMinute(d)` | `F.minute(col)` |
| `GetSecond(d)` | `F.second(col)` |
| `DaysBetween(d1, d2)` | `F.datediff(col1, col2)` |
| `HoursBetween(d1, d2)` | `(F.unix_timestamp(col1) - F.unix_timestamp(col2)) / 3600` |
| `AddDuration(d, 'P1D')` | `F.date_add(col, 1)` |
| `SubtractDuration(d, 'P1D')` | `F.date_sub(col, 1)` |
| `ToUnixTime(d)` | `F.unix_timestamp(col)` |
| `ToMilliSeconds(d)` | `F.unix_timestamp(col) * 1000` |

### Collection Functions

| Pig | PySpark | Notes |
|-----|---------|-------|
| `SIZE(bag)` | `F.size(col)` | For arrays/maps |
| `COUNT(bag)` | `F.count(col)` | In aggregation context |
| `COUNT_STAR(bag)` | `F.count('*')` | Includes nulls |
| `SUM(bag.col)` | `F.sum('col')` | In aggregation context |
| `AVG(bag.col)` | `F.avg('col')` | |
| `MIN(bag.col)` | `F.min('col')` | |
| `MAX(bag.col)` | `F.max('col')` | |
| `FLATTEN(bag)` | `F.explode(col)` | |
| `BagToTuple(bag)` | `F.collect_list(col)` | Within aggregation |
| `BagToString(bag, delim)` | `F.concat_ws(delim, F.collect_list(col))` | |
| `TOBAG(x, y, z)` | `F.array(x, y, z)` | |
| `TOTUPLE(x, y)` | `F.struct(x, y)` | |
| `TOMAP(k, v)` | `F.create_map(k, v)` | |

### Null/Conditional Functions

| Pig | PySpark |
|-----|---------|
| `(condition ? a : b)` | `F.when(condition, a).otherwise(b)` |
| `x is null` | `F.col('x').isNull()` |
| `x is not null` | `F.col('x').isNotNull()` |
| `COALESCE(a, b)` | `F.coalesce(col_a, col_b)` |

### Type Casting

| Pig | PySpark |
|-----|---------|
| `(int)x` | `F.col('x').cast('int')` |
| `(long)x` | `F.col('x').cast('long')` |
| `(float)x` | `F.col('x').cast('float')` |
| `(double)x` | `F.col('x').cast('double')` |
| `(chararray)x` | `F.col('x').cast('string')` |

---

## Data Types

| Pig Type | PySpark Type | Import |
|----------|-------------|--------|
| `int` | `IntegerType()` | `from pyspark.sql.types import *` |
| `long` | `LongType()` | |
| `float` | `FloatType()` | |
| `double` | `DoubleType()` | |
| `chararray` | `StringType()` | |
| `bytearray` | `BinaryType()` | |
| `boolean` | `BooleanType()` | |
| `datetime` | `TimestampType()` | |
| `bigdecimal` | `DecimalType(p, s)` | |
| `bag{tuple}` | `ArrayType(StructType([...]))` | |
| `tuple(fields...)` | `StructType([StructField(...), ...])` | |
| `map[key#value]` | `MapType(StringType(), ValueType())` | |

---

## Parameters & Macros

| Pig | PySpark |
|-----|---------|
| `%default param value` | `param = spark.conf.get('spark.app.param', 'value')` |
| `$param` in script | Variable reference or `spark.conf.get(...)` |
| `DEFINE macro_name(args) RETURNS ret { ... }` | Python function: `def macro_name(df, args): ...` |
| `IMPORT 'macro_file.pig'` | `from module import function` |
| `REGISTER jar_path` | `--jars jar_path` in spark-submit or `spark.jars` config |
| `DEFINE alias class(args)` | `@F.udf(returnType)` decorated function |

---

## Common Pig UDF → PySpark Equivalents

Common UDF mappings provided by the `pig_udfs.py` library (imported as `PU`):

| Pig UDF | pig_udfs.py | Purpose |
|---------|-------------|---------|
| `stringsUDFs.NULLSTR()` | `PU.null_str()` | Null-safe string handling |
| `stringsUDFs.CLEANSTR()` | `PU.clean_str()` | Clean/sanitize strings |
| `stringsUDFs.BALDELIM()` | `PU.balance_delim()` | Balance delimiters |
| `datesUDFs.PIGDATE()` | `PU.pig_date()` | Pig-compatible date parsing |

---

## S3 Path Migration

| EMR 5.x (Pig) | EMR 7.x (PySpark) |
|---|---|
| `s3n://bucket/path` | `s3://bucket/path` |
| `s3a://bucket/path` | `s3://bucket/path` |
| `hdfs:///path` | `s3://bucket/path` (preferred) or `hdfs:///path` (if HDFS used) |

Always replace `s3n://` with `s3://` — the `s3n` scheme is removed in Hadoop 3.