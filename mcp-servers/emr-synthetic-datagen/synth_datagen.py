#!/usr/bin/env python3
"""
EMR Synthetic Data Generator — core engine.

Reverse-engineers a synthetic dataset specification from:
  1. A Spark SQL query (or query template) — table references, join keys,
     filter columns, exploded map columns, window partition keys
  2. An event-log extract (task_stage_summary JSON from the Config Advisor
     extractor) — per-table volumes, partition counts, skew profile
  3. (Optional) table DDLs — exact column names/types

and emits:
  - a dataset SPEC (JSON, declarative per-column generation rules)
  - a runnable PySpark datagen script generated from the spec
  - Hive/Glue DDL for the generated tables

The goal is replicating a production job's PERFORMANCE SHAPE (volumes,
skew, join hit rates, shuffle profile) in a test EMR environment without
any customer data. Pipeline:

    spec = build_spec(sql_text, event_log_extract, ddls=...)
    script = generate_pyspark_script(spec)
    ddl = generate_ddl(spec, data_root)

Spec format (per table):
{
  "name": "db.table",
  "rows": 1100000000,
  "target_gb": 220.0,
  "partition_col": "event_date",
  "partition_values": ["2026-06-01", ...],   # or null
  "columns": [
     {"name": "duaid", "type": "string",
      "rule": {"kind": "id_pool", "pool": "duaid", "prefix": "duaid-",
               "cardinality": 60000000, "hot_pct": 2, "hot_share": 40}},
     {"name": "event_ts", "type": "timestamp",
      "rule": {"kind": "timestamp_in_partition"}},
     {"name": "lob", "type": "string",
      "rule": {"kind": "categorical", "values": ["Lodging","Air","Car"],
               "null_pct": 10}},
     {"name": "expuserid", "type": "map<string,struct<key_last_visit_date:date>>",
      "rule": {"kind": "map_of_ids", "pool": "expuser", "min_keys": 1,
               "max_keys": 3, "null_pct": 0}},
     ...
  ]
}

ID pools are shared across tables — two columns drawing from pool "duaid"
with overlapping cardinality produce realistic join hit rates.
"""
import json
import re
from collections import defaultdict

# ── SQL analysis ──────────────────────────────────────────────────────

TABLE_RE = re.compile(r'\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)', re.IGNORECASE)
EXPLODE_RE = re.compile(r'LATERAL\s+VIEW\s+EXPLODE\s*\(\s*(\w+)\s*\)', re.IGNORECASE)
JOIN_ON_RE = re.compile(r'ON\s+([\w.]+)\s*=\s*([\w.{}]+)', re.IGNORECASE)
WINDOW_RE = re.compile(r'PARTITION\s+BY\s+([\w.,\s]+?)\s+ORDER\s+BY', re.IGNORECASE)
DATE_FILTER_RE = re.compile(r"(\w+)\s*(?:>=|<=|=)\s*DATE\s*\(", re.IGNORECASE)
COL_USE_RE = re.compile(r'\b([a-z_][\w]*)\b')


def analyze_sql(sql_text):
    """Extract structural signals from the SQL: tables, join keys, exploded
    columns, window keys, date-filter columns."""
    # strip comments
    sql = re.sub(r'--[^\n]*', '', sql_text)
    tables = sorted(set(t.lower() for t in TABLE_RE.findall(sql)))
    exploded = sorted(set(c.lower() for c in EXPLODE_RE.findall(sql)))
    joins = [(a.lower(), b.lower()) for a, b in JOIN_ON_RE.findall(sql)]
    windows = []
    for w in WINDOW_RE.findall(sql):
        windows.append([c.strip().split('.')[-1] for c in w.split(',')])
    date_cols = sorted(set(c.lower() for c in DATE_FILTER_RE.findall(sql)))
    return {
        "tables": tables,
        "exploded_map_columns": exploded,
        "join_conditions": joins,
        "window_partition_keys": windows,
        "date_filter_columns": date_cols,
    }


# ── Event-log analysis ────────────────────────────────────────────────

def analyze_event_log(extract):
    """From a task_stage_summary extract (dict), derive volume targets.

    Scan stages (input_gb > 0, shuffle_read == 0) reveal per-table read
    volumes; distinct task-count signatures distinguish tables. Shuffle
    stages reveal the shuffle profile the synthetic job must reproduce."""
    stages = extract.get("stage_summary", {}).get("stages", [])
    seen = {}
    for s in stages:
        sid = s.get("stage_id")
        if sid not in seen:  # first attempt only
            seen[sid] = s
    scans = defaultdict(lambda: {"count": 0, "total_gb": 0.0, "max_gb": 0.0})
    shuffle_peak_r = shuffle_peak_w = 0.0
    for s in seen.values():
        igb = s.get("input_gb") or 0
        srd = s.get("shuffle_read_gb") or 0
        swr = s.get("shuffle_write_gb") or 0
        shuffle_peak_r = max(shuffle_peak_r, srd)
        shuffle_peak_w = max(shuffle_peak_w, swr)
        if igb > 50 and srd == 0:
            sig = s.get("num_tasks") or 0
            scans[sig]["count"] += 1
            scans[sig]["total_gb"] += igb
            scans[sig]["max_gb"] = max(scans[sig]["max_gb"], igb)
    io = extract.get("io_summary", {}).get("application_level", {})
    ex = extract.get("executor_summary", {})
    return {
        "total_input_gb": io.get("total_input_gb", 0),
        "total_shuffle_read_gb": io.get("total_shuffle_read_gb", 0),
        "total_shuffle_write_gb": io.get("total_shuffle_write_gb", 0),
        "peak_stage_shuffle_read_gb": shuffle_peak_r,
        "peak_stage_shuffle_write_gb": shuffle_peak_w,
        "duration_minutes": extract.get("total_run_duration_minutes", 0),
        "executors": ex.get("total_executors", 0),
        "cores": ex.get("total_cores", 0),
        "scan_signatures": [
            {"num_tasks": k, "stage_count": v["count"],
             "total_gb": round(v["total_gb"], 1), "max_stage_gb": round(v["max_gb"], 1)}
            for k, v in sorted(scans.items(), key=lambda kv: -kv[1]["total_gb"])
        ],
    }


# ── DDL analysis ──────────────────────────────────────────────────────

DDL_COL_RE = re.compile(r'^\s*([a-z_][\w]*)\s+((?:map|array|struct)<[^\n]+>|[a-z]+(?:\(\d+(?:,\d+)?\))?)\s*,?\s*$',
                        re.IGNORECASE | re.MULTILINE)
DDL_TABLE_RE = re.compile(r'CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)', re.IGNORECASE)
DDL_PART_RE = re.compile(r'PARTITIONED\s+BY\s*\(\s*(\w+)\s+(\w+)', re.IGNORECASE)


def analyze_ddl(ddl_text):
    """Parse a CREATE TABLE statement into {table, columns, partition_col}."""
    m = DDL_TABLE_RE.search(ddl_text)
    if not m:
        return None
    body = ddl_text[m.end():]
    cols = [(c.lower(), t.lower()) for c, t in DDL_COL_RE.findall(body)]
    pm = DDL_PART_RE.search(ddl_text)
    return {
        "table": m.group(1).lower(),
        "columns": cols,
        "partition_col": pm.group(1).lower() if pm else None,
        "partition_type": pm.group(2).lower() if pm else None,
    }


# ── Spec building ─────────────────────────────────────────────────────

def _default_rule(col, ctype, sql_info, pools):
    """Heuristic column rule from name + type + SQL usage."""
    name = col.lower()
    if ctype.startswith("map<"):
        pool = _pool_for(name, pools)
        return {"kind": "map_of_ids", "pool": pool, "min_keys": 1, "max_keys": 3,
                "null_pct": 95 if name in ("guid", "havid") else 0}
    if ctype.startswith("array<"):
        return {"kind": "array_of_strings", "min_len": 0, "max_len": 3}
    if ctype in ("timestamp",):
        return {"kind": "timestamp_in_partition"}
    if ctype in ("date",):
        return {"kind": "date_in_window"}
    if ctype in ("bigint", "int", "integer", "long"):
        if "timestamp" in name or name.endswith("_ts") or name.endswith("_ms"):
            return {"kind": "epoch_millis_in_partition"}
        return {"kind": "int_uniform", "min": 0, "max": 1000000}
    if ctype in ("boolean",):
        return {"kind": "boolean", "true_pct": 50}
    if ctype in ("double", "float", "decimal"):
        return {"kind": "double_uniform", "min": 0, "max": 1000}
    # strings
    if any(k in name for k in ("id", "guid", "code", "key")):
        pool = _pool_for(name, pools)
        return {"kind": "id_pool", "pool": pool, "prefix": pool + "-",
                "cardinality": pools.get(pool, {}).get("cardinality", 10_000_000)}
    if name in ("brand", "lob", "event_type", "device_type", "communication_type",
                "line_of_business", "status", "channel"):
        return {"kind": "categorical", "values": ["A", "B", "C", "D"], "null_pct": 0}
    return {"kind": "filler_string", "width": 32}


def _pool_for(name, pools):
    """Map a column name to a shared ID pool (join-key realism)."""
    for pool in pools:
        if pool in name:
            return pool
    base = re.sub(r'(_id|id)$', '', name) or name
    pools.setdefault(base, {"cardinality": 10_000_000})
    return base


def build_spec(sql_text, event_log_extract, ddls=None, scale=1.0,
               sent_date="2026-06-01", window_days=15):
    """Combine SQL + event-log + optional DDLs into a dataset spec.

    scale: fraction of the production volumes to target (1.0 = full size
    observed in the event log; 0.066 ≈ 1/15 etc.)."""
    sql_info = analyze_sql(sql_text)
    log_info = analyze_event_log(event_log_extract) if event_log_extract else {}
    ddl_map = {}
    for d in (ddls or []):
        parsed = analyze_ddl(d)
        if parsed:
            ddl_map[parsed["table"]] = parsed

    # Distribute observed input volume across referenced tables.
    # Use scan signatures when available: largest repeated signature = the
    # biggest table; otherwise split evenly.
    tables = sql_info["tables"]
    total_gb = (log_info.get("total_input_gb") or 100.0) * scale
    sigs = log_info.get("scan_signatures", [])
    weights = {}
    if sigs and tables:
        # rank tables by name-length heuristic? No — distribute by signature
        # totals, assigning the biggest signatures to the tables in SQL scan
        # order (FROM-clause order approximates plan scan order).
        sig_totals = [s["total_gb"] for s in sigs[:len(tables)]]
        ssum = sum(sig_totals) or 1
        for i, t in enumerate(tables):
            weights[t] = (sig_totals[i] / ssum) if i < len(sig_totals) else 0.05
    else:
        for t in tables:
            weights[t] = 1.0 / max(len(tables), 1)
    wsum = sum(weights.values()) or 1
    weights = {t: w / wsum for t, w in weights.items()}

    pools = {}
    # join-key columns become shared pools
    for a, b in sql_info["join_conditions"]:
        for side in (a, b):
            col = side.split('.')[-1]
            if not col.startswith('{'):
                _pool_for(col, pools)
    for c in sql_info["exploded_map_columns"]:
        _pool_for(c, pools)

    spec = {
        "version": 1,
        "scale": scale,
        "sent_date": sent_date,
        "window_days": window_days,
        "source_profile": log_info,
        "sql_structure": sql_info,
        "id_pools": pools,
        "tables": [],
    }
    BYTES_PER_ROW = 200  # default parquet-compressed estimate; refined per DDL
    for t in tables:
        tgt_gb = round(total_gb * weights[t], 1)
        ddl = ddl_map.get(t)
        columns = []
        if ddl:
            for cname, ctype in ddl["columns"]:
                columns.append({"name": cname, "type": ctype,
                                "rule": _default_rule(cname, ctype, sql_info, pools)})
            part_col = ddl["partition_col"]
        else:
            # minimal inferred schema: join keys + date filter + filler
            inferred = set()
            for a, b in sql_info["join_conditions"]:
                for side in (a, b):
                    col = side.split('.')[-1]
                    if not col.startswith('{'):
                        inferred.add(col)
            for c in sorted(inferred):
                columns.append({"name": c, "type": "string",
                                "rule": _default_rule(c, "string", sql_info, pools)})
            columns.append({"name": "pad_0", "type": "string",
                            "rule": {"kind": "filler_string", "width": 128}})
            part_col = (sql_info["date_filter_columns"][0]
                        if sql_info["date_filter_columns"] else None)
        rows = max(10_000, int(tgt_gb * 1024**3 / BYTES_PER_ROW))
        spec["tables"].append({
            "name": t, "rows": rows, "target_gb": tgt_gb,
            "partition_col": part_col,
            "partition_days": window_days * 2 + 1 if part_col else None,
            "columns": columns,
        })
    return spec


# ── PySpark script generation ─────────────────────────────────────────

_RULE_EXPRS = {
    "id_pool": lambda r, pools: (
        "concat('{p}', CASE WHEN pmod(hash(id, 17), 100) < {hs} "
        "THEN pmod(hash(id, 31), {hot}) "
        "ELSE {hot} + pmod(hash(id, 47), {rest}) END)".format(
            p=r.get("prefix", r["pool"] + "-"),
            hs=r.get("hot_share", 0),
            hot=max(1, int(r.get("cardinality", 10**7) * r.get("hot_pct", 2) / 100)),
            rest=max(1, int(r.get("cardinality", 10**7) * (100 - r.get("hot_pct", 2)) / 100)))
        if r.get("hot_pct") else
        "concat('{p}', pmod(hash(id, 31), {c}))".format(
            p=r.get("prefix", r["pool"] + "-"), c=r.get("cardinality", 10**7))),
    "categorical": lambda r, pools: (
        "CASE pmod(hash(id, 37), {n}) {whens} END".format(
            n=len(r["values"]) + (1 if r.get("null_pct") else 0),
            whens=" ".join("WHEN %d THEN '%s'" % (i, v)
                           for i, v in enumerate(r["values"])))),
    "timestamp_in_partition": lambda r, pools:
        "cast(unix_timestamp(part_date) + pmod(hash(id, 19), 86400) as timestamp)",
    "epoch_millis_in_partition": lambda r, pools:
        "unix_timestamp(part_date) * 1000 + pmod(hash(id, 13), 86400000)",
    "date_in_window": lambda r, pools:
        "date_sub(current_date(), pmod(hash(id, 23), 700))",
    "int_uniform": lambda r, pools:
        "pmod(hash(id, 29), {span}) + {mn}".format(span=r["max"] - r["min"], mn=r["min"]),
    "double_uniform": lambda r, pools:
        "(pmod(hash(id, 29), 100000) / 100000.0) * {span} + {mn}".format(
            span=r["max"] - r["min"], mn=r["min"]),
    "boolean": lambda r, pools:
        "pmod(hash(id, 11), 100) < {t}".format(t=r.get("true_pct", 50)),
    "filler_string": lambda r, pools:
        "repeat(md5(cast(id as string)), {n})".format(n=max(1, r.get("width", 32) // 32)),
    "map_of_ids": lambda r, pools: (
        "map_from_arrays("
        "transform(sequence(1, {mn} + pmod(hash(id, 41), {span})), "
        "i -> concat('{p}', pmod(hash(id, 47) + i * 104729, {c}))), "
        "transform(sequence(1, {mn} + pmod(hash(id, 41), {span})), "
        "i -> named_struct('key_last_visit_date', "
        "date_sub(current_date(), pmod(hash(id, 59) + i, 700)))))".format(
            mn=r.get("min_keys", 1),
            span=max(1, r.get("max_keys", 3) - r.get("min_keys", 1) + 1),
            p=r["pool"] + "-", c=pools.get(r["pool"], {}).get("cardinality", 10**7))),
    "array_of_strings": lambda r, pools:
        "transform(sequence(1, 1 + pmod(hash(id, 41), {mx})), i -> concat('v', i))".format(
            mx=max(1, r.get("max_len", 3))),
}


def _column_expr(col, pools):
    rule = col["rule"]
    fn = _RULE_EXPRS.get(rule["kind"])
    if fn is None:
        expr = "cast(null as string)"
    else:
        expr = fn(rule, pools)
    null_pct = rule.get("null_pct", 0)
    if null_pct:
        expr = "CASE WHEN pmod(hash(id, 7), 100) < %d THEN NULL ELSE %s END" % (null_pct, expr)
    return expr


def generate_pyspark_script(spec, output_root="hdfs:///synth_data"):
    """Emit a self-contained PySpark script that materializes the spec."""
    lines = [
        "#!/usr/bin/env python3",
        '"""Auto-generated by emr-synthetic-datagen. Spec version %d, scale %s."""' % (
            spec["version"], spec["scale"]),
        "import argparse",
        "from pyspark.sql import SparkSession",
        "import pyspark.sql.functions as F",
        "",
        "p = argparse.ArgumentParser()",
        "p.add_argument('--output', default=%r)" % output_root,
        "p.add_argument('--scale', type=float, default=1.0,",
        "               help='additional multiplier on spec row counts')",
        "a = p.parse_args()",
        "out = a.output.rstrip('/')",
        "spark = (SparkSession.builder.appName('synth-datagen')",
        "         .config('spark.sql.parquet.compression.codec', 'snappy').getOrCreate())",
        "",
    ]
    for t in spec["tables"]:
        safe = t["name"].replace(".", "/")
        var = t["name"].replace(".", "_")
        part_col = t.get("partition_col")
        lines.append("# ── %s: %d rows (~%.1f GB) ──" % (t["name"], t["rows"], t["target_gb"]))
        if part_col and t.get("partition_days"):
            days = t["partition_days"]
            rows_per_day = max(1000, t["rows"] // days)
            lines += [
                "for d in range(-%d, %d):" % (days // 2, days - days // 2),
                "    part_date_str = None",
                "    df = (spark.range(int(%d * a.scale))" % rows_per_day,
                "          .withColumn('part_date', F.date_add(F.lit('%s'), F.lit(d))))" % spec["sent_date"],
            ]
            for c in t["columns"]:
                if c["name"] == part_col:
                    continue
                lines.append("    df = df.withColumn(%r, F.expr(%r))"
                             % (c["name"], _column_expr(c, spec["id_pools"])))
            lines += [
                "    df = df.withColumnRenamed('part_date', %r).drop('id')" % part_col,
                "    pv = df.select(F.col(%r).cast('string')).first()[0]" % part_col,
                "    (df.repartition(8).write.mode('overwrite')",
                "       .parquet(f'{out}/%s/%s={pv}'))" % (safe, part_col),
                "    print('[synth] %s day', d, 'done', flush=True)" % t["name"],
                "",
            ]
        else:
            lines += [
                "df = spark.range(int(%d * a.scale))" % t["rows"],
                "df = df.withColumn('part_date', F.lit('%s').cast('date'))" % spec["sent_date"],
            ]
            for c in t["columns"]:
                lines.append("df = df.withColumn(%r, F.expr(%r))"
                             % (c["name"], _column_expr(c, spec["id_pools"])))
            lines += [
                "df = df.drop('id', 'part_date')",
                "df.repartition(%d).write.mode('overwrite').parquet(f'{out}/%s')" % (
                    max(8, int(t["target_gb"])), safe),
                "print('[synth] %s done', flush=True)" % t["name"],
                "",
            ]
    lines.append("print('[synth] ALL DONE', flush=True)")
    return "\n".join(lines)


def generate_ddl(spec, data_root):
    """Emit CREATE EXTERNAL TABLE statements over the generated parquet."""
    out = []
    dbs = sorted(set(t["name"].split(".")[0] for t in spec["tables"]))
    for db in dbs:
        out.append("CREATE DATABASE IF NOT EXISTS %s;" % db)
    for t in spec["tables"]:
        safe = t["name"].replace(".", "/")
        part_col = t.get("partition_col")
        cols = []
        for c in t["columns"]:
            if c["name"] == part_col:
                continue
            cols.append("  %s %s" % (c["name"], c["type"]))
        ddl = "DROP TABLE IF EXISTS %s;\nCREATE EXTERNAL TABLE %s (\n%s\n)" % (
            t["name"], t["name"], ",\n".join(cols))
        if part_col:
            ptype = "date"
            ddl += "\nPARTITIONED BY (%s %s)" % (part_col, ptype)
        ddl += "\nSTORED AS PARQUET\nLOCATION '%s/%s';" % (data_root.rstrip("/"), safe)
        if part_col:
            ddl += "\nMSCK REPAIR TABLE %s;" % t["name"]
        out.append(ddl)
    return "\n\n".join(out)


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Synthetic dataset spec/script generator")
    ap.add_argument("--sql", required=True, help="Path to SQL file")
    ap.add_argument("--event-log-extract", help="Path to task_stage_summary JSON")
    ap.add_argument("--ddl", action="append", default=[], help="Path(s) to DDL files")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--sent-date", default="2026-06-01")
    ap.add_argument("--output-spec", default="dataset_spec.json")
    ap.add_argument("--output-script", default="generated_datagen.py")
    ap.add_argument("--output-ddl", default="generated_tables.sql")
    ap.add_argument("--data-root", default="s3://CHANGE-ME/synth")
    args = ap.parse_args()

    with open(args.sql) as f:
        sql_text = f.read()
    extract = None
    if args.event_log_extract:
        with open(args.event_log_extract) as f:
            extract = json.load(f)
    ddls = []
    for d in args.ddl:
        with open(d) as f:
            ddls.append(f.read())

    spec = build_spec(sql_text, extract, ddls=ddls, scale=args.scale,
                      sent_date=args.sent_date)
    with open(args.output_spec, "w") as f:
        json.dump(spec, f, indent=1)
    with open(args.output_script, "w") as f:
        f.write(generate_pyspark_script(spec, args.data_root))
    with open(args.output_ddl, "w") as f:
        f.write(generate_ddl(spec, args.data_root))
    print("spec=%s script=%s ddl=%s (%d tables, %.1f GB total)" % (
        args.output_spec, args.output_script, args.output_ddl,
        len(spec["tables"]), sum(t["target_gb"] for t in spec["tables"])))


if __name__ == "__main__":
    main()
