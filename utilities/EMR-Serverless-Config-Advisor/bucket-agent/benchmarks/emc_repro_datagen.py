#!/usr/bin/env python3
"""Synthetic data generator replicating eg-marketing-entry-clicks inputs.

Targets (from customer event log 00g6ohel8nj11o0b, SQL accumulators):
  clickstream_events : 5.83B rows, ~3.26 TB Iceberg file size -> 26.9K scan splits
  entry_clicks       : 95.4M rows, ~117 GB scan bytes
  pev_domain_events  : ~223 GB file size
  pev_business_events: ~34 GB file size

Schemas carry the same column ROLES (uuid join keys, event timestamps within
a +/-5s matchable window, view ids, fat payload strings) so join/aggregate
selectivities and scan CPU-per-GB land close to the original.

Usage: --output s3://bucket/prefix --scale 1.0 [--tables all|clickstream|...]
"""
import argparse
from pyspark.sql import SparkSession, functions as F

P = argparse.ArgumentParser()
P.add_argument("--output", required=True)
P.add_argument("--scale", type=float, default=1.0)
P.add_argument("--tables", default="all")
args = P.parse_args()

spark = (SparkSession.builder.appName("emc-repro-datagen")
         .config("spark.sql.catalog.repro", "org.apache.iceberg.spark.SparkCatalog")
         .config("spark.sql.catalog.repro.type", "hadoop")
         .config("spark.sql.catalog.repro.warehouse", args.output)
         .getOrCreate())

S = args.scale
CS_ROWS = int(5_830_000_000 * S)
EC_ROWS = int(95_400_000 * S)
PEV_DE_ROWS = int(430_000_000 * S)   # ~223GB at ~520B/row
PEV_BE_ROWS = int(66_000_000 * S)    # ~34GB

# Base epoch for a 1-day window of events (matcher uses +/-5s windows)
T0 = 1751500800  # fixed for determinism

def payload(col_prefix, n, width):
    """Deterministic pseudo-random fat string columns.

    One sha512 per ~128 chars, sliced — hashing dominates datagen CPU."""
    seeds_needed = (n * width + 127) // 128
    seed = F.concat(*[F.sha2(F.concat(F.col("id").cast("string"),
                                      F.lit(f"{col_prefix}{s}")), 512)
                      for s in range(seeds_needed)])
    return [seed.substr(1 + i * width, width).alias(f"{col_prefix}_{i}")
            for i in range(n)]


def real_url(name, prefix, mean_len, max_len, distinct_n, null_frac, seed,
             p99_len=None):
    """URL column calibrated to production column statistics (2026-07-10):
    shared scheme/host prefix (comparators scan it before differing),
    cardinality via modulus token, exponential length (p99 ~= 4.6x mean,
    matching prod's 3.3-6.4x avg->p99 ratios), capped at prod max.

    v6: body is a HASH CHAIN keyed by the cardinality token — sha512(tok||i)
    concatenated — so each distinct URL value has full-entropy, aperiodic
    content. v5's repeat(tok) body was periodic: Parquet dictionary/RLE
    crushed it (table shrank 3.6x) and sort comparators resolved at byte 1
    (write-stage CPU stayed 13 s/GB vs the 168-341 target). Real URLs are
    incompressible mid-string; entropy is the load-bearing property.
    Chain length covers p99; the rare tail beyond p99 truncates — the CPU
    density lives in the bulk, not the extreme tail.
    Returns list of (colname, Column) to apply via withColumn."""
    tok = F.sha2(F.pmod(F.hash(F.col("id"), F.lit(seed)),
                        F.lit(int(distinct_n))).cast("string"), 256)
    body_mean = max(mean_len - len(prefix) - 20, 8)
    cap = p99_len or min(max_len, int(body_mean * 8))
    ln = F.least(F.lit(cap),
                 (F.lit(float(body_mean)) * -F.log(F.rand(seed))).cast("int") + F.lit(1))
    # 128 chars of hex per sha512; chain enough links to cover the cap
    links = (cap + 127) // 128
    chain = F.concat(*[F.sha2(F.concat(F.col(f"_{name}_tok"), F.lit(str(i))), 512)
                       for i in range(links)])
    # v9: URL-SHAPED body — '&key=' separators every ~24 chars + '%2520'
    # double-encoded sequences every ~40 chars. v8 measured the decode chain
    # 49x cheaper per row than prod (66us vs 3.2ms) because hex bodies gave
    # URLDecoder no '%' to decode (x2 passes = 2 no-op scans) and str_to_map/
    # array_sort a single entry. Real query strings carry 10-30 params and
    # (decodeCount=2 in their code) double-encoded values; this makes the
    # per-row work real: ~L/24 map entries to split+sort, ~L/40 escape
    # sequences per decode pass.
    urlish = F.expr(
        f"concat_ws('&', transform(sequence(0, cast(_{name}_len/24 as int)), "
        f"i -> concat('k', i, '=', "
        f"substring(_{name}_chain, i*24+1, 16), '%2520', "
        f"substring(_{name}_chain, i*24+17, 6))))")
    cols = [
        (f"_{name}_tok", tok),
        (f"_{name}_len", ln),
        (f"_{name}_chain", chain),
        (name, F.concat(F.lit(prefix),
                        F.expr(f"substring(_{name}_tok, 1, 12)"),
                        F.lit("&"),
                        F.substring(urlish, 1, 9999).substr(F.lit(1), F.col(f"_{name}_len")))),
    ]
    if null_frac > 0:
        cols.append((name, F.when(F.rand(seed + 1) < null_frac,
                                  F.lit(None).cast("string"))
                            .otherwise(F.col(name))))
    return cols


def with_urls(df, specs):
    for name, col in [c for spec in specs for c in spec]:
        df = df.withColumn(name, col)
    drop = [c for c in df.columns if c.startswith("_") and
            (c.endswith("_tok") or c.endswith("_len") or c.endswith("_chain"))]
    return df.drop(*drop)

def clickstream():
    # 5.83B rows, ~560 B/row on disk. cs_event_uuid ~ 1 winner per ~23 rows;
    # groups keep ~15 matchable members after the null seed, and virtually
    # every group retains >=1 (0.35^23 ~ 0), so the matcher agg output stays
    # at the ~250M-row target.
    # Null seed: prod skew stats (2026-07-08) show 35% of page views carry a
    # null/empty view_id — one 45M-row hot key that lands in a single
    # partition anywhere the job shuffles/groups on cs_view_id (real keys
    # are mild, p9999=109). Modulus 97 (prime) decorrelates the null
    # pattern from the id%EC_ROWS key mapping so every entry-click key
    # loses ~35% of rows instead of 35% of keys losing everything.
    df = (spark.range(0, CS_ROWS, 1, 8000)
        .withColumn("cs_event_uuid",
            F.sha2(F.floor(F.col("id") / 23).cast("string"), 256).substr(1, 36))
        .withColumn("cs_event_timestamp",
            F.timestamp_seconds(T0 + (F.col("id") % 86400) + (F.rand(7) * 4).cast("long")))
        .withColumn("cs_event_origination_timestamp",
            F.timestamp_seconds(T0 + (F.col("id") % 86400)))
        .withColumn("cs_view_id",
            F.when(F.col("id") % 97 < 34, F.lit(""))  # ~35% null/empty hot key
             .otherwise(F.lower(F.sha2((F.col("id") % EC_ROWS).cast("string"),
                                       256).substr(1, 32))))
        .withColumn("eg_site_id", (F.col("id") % 400).cast("int"))
        .withColumn("eg_brand_id", (F.col("id") % 40).cast("int"))
        .withColumn("device_user_agent_id",
            F.sha2((F.col("id") % 40_000_000).cast("string"), 256).substr(1, 36)))
    # URL columns calibrated to prod stats (clickstream_eg_business_event_v2,
    # 2026-07-06, 127.4M page views): request_url avg 191/p99 1211/max 8456
    # distinct 0.247 null 0.018; referrer_url avg 342/p99 1551/max 5019
    # distinct 0.197 null 0.545. These replace 2 of the 5 fixed-width hex
    # payloads — remaining 3 stay hex to hold the ~560 B/row disk target.
    df = with_urls(df, [
        real_url("cs_request_url", "https://www.example.com/search?d=",
                 191, 8456, int(CS_ROWS * 0.247) or 1000, 0.018, 101,
                 p99_len=1211),
        real_url("cs_referrer_url", "https://ref.example.com/p/",
                 342, 5019, int(CS_ROWS * 0.197) or 1000, 0.545, 102,
                 p99_len=1551),
    ]).select("*", *payload("cs_pl", 3, 170))
    # Prod geometry (SHOW TBLPROPERTIES 2026-07-11): clickstream writes 2GB
    # files (explicitly set, 4x Iceberg default), snappy. Few huge files ->
    # Iceberg's 128MB read splits pack into ~27K fat tasks (prod) instead of
    # 305K small ones (our old 128MB files + 4MB open-cost padding).
    # Physical 2GB files require few, fat write tasks (~3.2TB/1600 = 2GB).
    # coalesce (narrow) instead of repartition: shuffling 3.2TB of generated
    # rows through 20G-disk executors ran out of local disk (v9 datagen
    # attempt 1). coalesce collapses the 8000 range splits into 1600
    # generation+write tasks with zero shuffle.
    (df.coalesce(1600)
       .writeTo("repro.db.clickstream_events")
       .using("iceberg")
       .tableProperty("write.target-file-size-bytes", "2147483648")
       .tableProperty("write.parquet.compression-codec", "snappy")
       .createOrReplace())

def entry_clicks():
    df = (spark.range(0, EC_ROWS, 1, 400)
        .withColumn("ec_click_id", F.sha2(F.col("id").cast("string"), 256).substr(1, 36))
        .withColumn("ec_click_date_utc", F.lit("2026-07-03"))
        .withColumn("ec_click_datetime_utc", F.timestamp_seconds(T0 + (F.col("id") % 86400)))
        .withColumn("ec_duaid",
            F.sha2((F.col("id") % 40_000_000).cast("string"), 256).substr(1, 36))
        .withColumn("ec_eg_brand_id", (F.col("id") % 40).cast("int"))
        .withColumn("ec_view_id",
            F.lower(F.sha2(F.col("id").cast("string"), 256).substr(1, 32)))
        )
    # URL columns calibrated to prod stats (marketing_platform_entry_click_
    # business_event, 2026-07-06, 22.2M rows): request_url avg 366/p99 1627/
    # max 8861 distinct 0.618 null 0; cleansed avg 46/p99 149/max 647
    # distinct 0.127; referrer avg 37/p99 1512/max 5266 distinct 0.028
    # null 0.601.
    df = with_urls(df, [
        real_url("ec_request_url", "https://www.example.com/Hotel-Search?d=",
                 366, 8861, int(EC_ROWS * 0.618) or 1000, 0.0, 201,
                 p99_len=1627),
        real_url("ec_request_url_cleansed", "example.com/",
                 46, 647, int(EC_ROWS * 0.127) or 1000, 0.0, 202,
                 p99_len=149),
        real_url("ec_referrer_url", "https://ref.example.com/",
                 37, 5266, int(EC_ROWS * 0.028) or 1000, 0.601, 203,
                 p99_len=1512),
    ]).select("*", *payload("ec_pl", 3, 420))
    df.writeTo("repro.db.entry_clicks").using("iceberg").createOrReplace()

def pev_domain():
    df = (spark.range(0, PEV_DE_ROWS, 1, 800)
        .withColumn("pev_de_view_id",
            F.lower(F.sha2((F.col("id") % EC_ROWS).cast("string"), 256).substr(1, 32)))
        .withColumn("pev_be_uuid", F.sha2((F.col("id") % PEV_BE_ROWS).cast("string"), 256).substr(1, 36))
        .withColumn("pev_timestamp", F.timestamp_seconds(T0 + (F.col("id") % 86400)))
        .select("*", *payload("de_pl", 5, 190)))
    df.writeTo("repro.db.pev_domain_events").using("iceberg").createOrReplace()

def pev_business():
    df = (spark.range(0, PEV_BE_ROWS, 1, 200)
        .withColumn("pev_be_uuid", F.sha2(F.col("id").cast("string"), 256).substr(1, 36))
        .withColumn("pev_be_acquisition_date", F.lit("2026-07-03"))
        .withColumn("pev_be_legacy_cleansed_trace", F.sha2(F.col("id").cast("string"), 512))
        .withColumn("pev_timestamp", F.timestamp_seconds(T0 + (F.col("id") % 86400)))
        .select("*", *payload("be_pl", 4, 175)))
    df.writeTo("repro.db.pev_business_events").using("iceberg").createOrReplace()

TABLES = {"clickstream": clickstream, "entry_clicks": entry_clicks,
          "pev_domain": pev_domain, "pev_business": pev_business}
todo = TABLES if args.tables == "all" else {k: TABLES[k] for k in args.tables.split(",")}
for name, fn in todo.items():
    print(f"=== generating {name} (scale {S}) ===")
    fn()
    print(f"=== {name} done ===")
print("DATAGEN COMPLETE")
