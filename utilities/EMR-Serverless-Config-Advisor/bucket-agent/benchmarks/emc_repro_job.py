#!/usr/bin/env python3
"""Replica of eg-marketing-entry-clicks (customer run 00g6ohel8nj11o0b).

Reproduces the event-log DAG shape and volumes:
  - 10 localCheckpoint rounds alternating two patterns from the customer
    plan (LandingPageMatcher.scala:102):
      MATCH:  scan clickstream (5.83B rows) -> filter to +/-5s window vs
              entry clicks -> groupBy(cs_event_uuid).min(struct) "winner"
              agg (26.9K-task scan stage, ~250M agg output rows) -> inner
              join back on winner uuid
      DEDUP:  left-outer anti-style joins removing already-matched uuids
  - final overwritePartitions: join matched output to entry clicks,
    struct-heavy projection, Repartition(8) coalesce, dynamic-partition
    sort-write to an Iceberg table (the 2,450GB scan / 26.9K task write
    stage in the original, out ~49GB / 43M rows)

Validation targets (customer stage metrics):
  stage 3-alike : in 5.83B rows / 1,269GB, shuffle write 41.9GB, 26,916 tasks
  stage 175-alike: in 2,450GB, shuffle write 211.6GB / 249M rows
  final write   : 43.4M rows, ~49GB output
"""
import argparse
import time

from pyspark.sql import SparkSession, functions as F

P = argparse.ArgumentParser()
P.add_argument("--warehouse", required=True, help="Iceberg warehouse with repro.db tables")
P.add_argument("--rounds", type=int, default=10)
args = P.parse_args()

spark = (SparkSession.builder.appName("eg-marketing-entry-clicks-repro")
         .config("spark.sql.catalog.repro", "org.apache.iceberg.spark.SparkCatalog")
         .config("spark.sql.catalog.repro.type", "hadoop")
         .config("spark.sql.catalog.repro.warehouse", args.warehouse)
         .getOrCreate())

t0 = time.time()
log = lambda m: print(f"[emc-repro +{time.time()-t0:7.1f}s] {m}", flush=True)

cs = spark.table("repro.db.clickstream_events")
ec = spark.table("repro.db.entry_clicks")
pev_de = spark.table("repro.db.pev_domain_events")
pev_be = spark.table("repro.db.pev_business_events")

# Working set: unmatched entry clicks (shrinks each round, like the
# customer's iterative matcher). Real MdeClickstreamJoin narrows to ~13
# candidate columns (cleansed URL only — raw URLs/payloads dropped) BEFORE
# the round loop — the fat full-width checkpoint is what killed full-scale
# attempts 1-2 (executor deaths -> "Checkpoint block not found").
# We keep localCheckpoint (ALL rounds — matched too) rather than the
# customer's persist(DISK_ONLY): lineage truncation is what lets the
# driver release each round's broadcast relations. persist() keeps every
# prior round's broadcasts reachable through un-truncated plans and
# 137-kills the driver around round 8 (verified twice: 00g72r3r59ro9g0b
# all-persist, 00g72rkcm00i280b matched-persist, driver RSS 15.8G/18G).
# With the narrow projection the checkpoint blocks are ~20x smaller,
# removing the executor-death cause from full-scale attempts 1-2.
CANDIDATE_COLS = ["ec_click_id", "ec_click_date_utc", "ec_click_datetime_utc",
                  "ec_view_id", "ec_duaid", "ec_eg_brand_id",
                  "ec_request_url_cleansed"]
work = ec.select(*CANDIDATE_COLS).localCheckpoint()
log(f"entry clicks checkpointed ({len(CANDIDATE_COLS)} cols): {work.count():,}")

matched_parts = []
for rnd in range(args.rounds):
    if rnd % 2 == 0:
        # MATCH round: the 26.9K-task clickstream scan + winner aggregation
        cand = (cs.alias("cs")
            .join(F.broadcast(work.select("ec_view_id", "ec_click_id",
                                          "ec_click_date_utc",
                                          "ec_click_datetime_utc").limit(2_000_000)),
                  F.col("cs.cs_view_id") == F.col("ec_view_id"), "inner")
            .where((F.col("cs.cs_event_timestamp")
                    >= F.col("ec_click_datetime_utc") - F.expr("INTERVAL 5 SECONDS"))
                   & (F.col("cs.cs_event_timestamp")
                      <= F.col("ec_click_datetime_utc") + F.expr("INTERVAL 5 SECONDS"))))
        winners = (cand.groupBy("cs_event_uuid")
            .agg(F.min(F.struct("ec_click_datetime_utc", "cs_event_timestamp",
                                "cs_event_origination_timestamp", "ec_click_id",
                                "ec_click_date_utc")).alias("cs_min")))
        matched = (cand.join(winners, "cs_event_uuid")
            .where((F.col("cs_event_timestamp") == F.col("cs_min.cs_event_timestamp"))
                   & (F.col("ec_click_id") == F.col("cs_min.ec_click_id")))
            .select("cs_event_uuid", "ec_click_id", "ec_click_date_utc",
                    "device_user_agent_id", "eg_brand_id", "cs_pl_0",
                    F.lit(f"match_round_{rnd}").alias("match_method"))
            .localCheckpoint())
        matched_parts.append(matched)
        log(f"round {rnd} MATCH checkpointed")
    else:
        # DEDUP round: strip matched uuids via left-outer + isnull filter
        got = matched_parts[-1]
        work = (work.alias("w")
            .join(F.broadcast(got.select(F.col("ec_click_id").alias("ec_click_id_dup")).distinct()),
                  F.col("w.ec_click_id") == F.col("ec_click_id_dup"), "left_outer")
            .where(F.isnull("ec_click_id_dup"))
            .drop("ec_click_id_dup")
            .localCheckpoint())
        log(f"round {rnd} DEDUP checkpointed")

# The customer's write execution (run-5 plan, exec 10): FULL-WIDTH
# clickstream scan, Union of two filtered passes, Exchange on view_id,
# Sort + WINDOW over 493M wide rows -> filter -> join-back.
# v7 addition — THE CPU CULPRIT (customer code, UDFs.scala:10-24 +
# ClickstreamBusinessEventReader.scala:48-102): request_url_cleansed is
# computed per row via a 4-step chain whose step 2 is a non-codegen Scala
# UDF looping java.net.URLDecoder.decode 2x over the 200-8,000-char query
# string. A Python UDF here would overshoot (pickle serialization); the
# faithful stand-in for "codegen-blocked JVM string churn per row" is the
# same native chain plus decode-equivalent work: parse_url x3, double
# reflect()-based java URLDecoder call, str_to_map/array_sort/transform
# re-assembly. reflect() invokes java.net.URLDecoder.decode via JVM
# reflection — non-codegen, per-row, same class as their closure UDF.
from pyspark.sql.window import Window as W


def cleanse_url(col_name):
    """Customer's request_url_cleansed chain (verbatim shape, 2026-07-11):
    host+path (native parse_url) || decodeUDF(query, 2) || sorted params."""
    lowered = f"lower({col_name})"
    # Strip incomplete trailing escapes before each decode pass — synthetic
    # length truncation can cut a %2520 mid-sequence and java URLDecoder
    # throws IllegalArgumentException on a dangling '%'. (Real URLs don't
    # truncate mid-escape; the guard regex itself adds per-row work, which
    # is faithful — their chain also runs multiple regex/parse passes.)
    q0 = f"regexp_replace(coalesce(parse_url({lowered},'QUERY'),''), '%[0-9a-f]?$', '')"
    d1 = f"regexp_replace(reflect('java.net.URLDecoder','decode', {q0}, 'UTF-8'), '%[0-9a-f]?$', '')"
    decoded = f"reflect('java.net.URLDecoder','decode', {d1}, 'UTF-8')"
    sorted_q = (f"concat_ws('&', transform(array_sort(map_entries("
                f"str_to_map({decoded},'&','='))), "
                f"x -> concat(x.key,'=',x.value)))")
    host_path = (f"concat('https://', coalesce(parse_url({lowered},'HOST'),''), "
                 f"coalesce(parse_url({lowered},'PATH'),''))")
    return F.expr(f"concat_ws('', {host_path}, "
                  f"if(length({sorted_q})>0, concat('?',{sorted_q}), ''))")


# Customer's Readers cleanse AT SCAN TIME (ClickstreamBusinessEventReader
# .scala:48-102 runs the chain in the read projection — against every
# scanned row, before any match filtering). v8 applied it after the union
# filters and Catalyst deferred it past the window (only 65G cleansed,
# 4.6h; customer: full 2,450G, ~224h). Cleansing FIRST, on the raw scan,
# then filtering — matches their dataflow and forces full-width work.
cs_cleansed = (cs
    .withColumn("cs_request_url_cleansed", cleanse_url("cs_request_url"))
    .withColumn("cs_referrer_url_cleansed", cleanse_url("cs_referrer_url"))
    .withColumn("cs_view_id_cleansed",
                F.expr("lower(regexp_replace(cs_view_id,'-',''))")))
# Selectivity matched to customer: union arms 236.8M + 12.5M of 5.5B scan.
cs_pass1 = cs_cleansed.where((F.col("cs_view_id") != "") & (F.col("eg_site_id") < 25))
cs_pass2 = cs_cleansed.where((F.col("cs_view_id") != "") & (F.col("eg_site_id") == 399))
cs_union = cs_pass1.unionByName(cs_pass2)

# Cleansed URL as a sort key (their UrlMatch dedup windows key on
# ec_request_url_cleansed === cs_request_url_cleansed): forces the
# non-codegen decode chain to evaluate for EVERY union row BEFORE the
# sort (defeats Catalyst's project-deferral that made the v8-smoke
# cleansing free), and makes the sort comparator scan 200-1,500-char
# decoded strings — the two per-row costs their stage actually pays.
w = (W.partitionBy("cs_view_id")
      .orderBy(F.col("cs_event_timestamp").asc(),
               F.col("cs_request_url_cleansed").asc(),
               F.col("cs_event_uuid").asc()))
windowed = (cs_union
    .withColumn("visit_rank", F.row_number().over(w))
    .withColumn("first_ts_in_view", F.first("cs_event_timestamp").over(w))
    .where(F.col("visit_rank") <= 100))                 # keeps ~all (customer: 493M out)

all_matched = (windowed.alias("cs")
    .join(ec.select("ec_view_id", "ec_click_id", "ec_click_date_utc",
                    "ec_click_datetime_utc").alias("e2"),
          F.col("cs.cs_view_id") == F.col("e2.ec_view_id"), "inner")
    .where((F.col("cs.cs_event_timestamp")
            >= F.col("ec_click_datetime_utc") - F.expr("INTERVAL 5 SECONDS"))
           & (F.col("cs.cs_event_timestamp")
              <= F.col("ec_click_datetime_utc") + F.expr("INTERVAL 5 SECONDS"))
           & (F.col("visit_rank") == 1))
    .select("cs_event_uuid", "ec_click_id", "ec_click_date_utc",
            "device_user_agent_id",
            F.col("cs_pl_0").alias("cs_payload_0"),
            F.col("cs_pl_1").alias("cs_payload_1"),
            F.col("cs_request_url").alias("cs_payload_2"),
            F.col("cs_referrer_url").alias("cs_payload_3"),
            F.lit("recomputed_wide").alias("match_method")))

# Final assembly + the big sort-write:
# join matched -> entry clicks (full width incl. fat URLs), pev enrichment,
# struct-heavy projection, Repartition(8, shuffle) then dynamic overwrite
final = (ec.alias("e")
    .join(all_matched.alias("m"), ["ec_click_id", "ec_click_date_utc"], "left_outer")
    .join(pev_de.select(F.col("pev_de_view_id"),
                        F.col("pev_be_uuid").alias("d_pev_be_uuid")).alias("d"),
          F.col("e.ec_view_id") == F.col("d.pev_de_view_id"), "left_outer")
    # entry-click side cleansing (EntryClickReader.scala:14-65 — same chain
    # incl. the double-decode; runs against every entry-click row)
    .withColumn("ec_url_cleansed_live", cleanse_url("e.ec_request_url"))
    .select(
        F.col("e.ec_duaid").alias("device_user_agent_id"),
        F.struct("e.ec_click_datetime_utc", "e.ec_eg_brand_id",
                 "e.ec_request_url", "ec_url_cleansed_live",
                 "e.ec_referrer_url", "e.ec_click_id").alias("entry_clicks"),
        F.struct("d.d_pev_be_uuid", "e.ec_view_id").alias("platform_entry_events"),
        # Customer's clickstream struct carries 22 fields INCLUDING the
        # request/referrer URLs — referencing the payloads here is what
        # keeps them un-pruned through scan->window->join (their scan reads
        # 2,450G full-width; v4-v6 scanned only ~665G because this struct
        # didn't touch the payloads and column pruning removed them from
        # the whole pipeline — the real reason CPU stayed at 13 s/GB).
        F.struct("m.cs_event_uuid", "m.match_method",
                 "m.cs_payload_0", "m.cs_payload_1",
                 "m.cs_payload_2", "m.cs_payload_3").alias("clickstream"),
        F.coalesce(F.concat(F.lit("m"), (F.abs(F.hash("m.cs_event_uuid")) % 6).cast("string")), F.lit("unmatched")).alias("match_method"),
        F.current_timestamp().alias("etl_insert_datetime_utc"),
        F.col("e.ec_click_date_utc").alias("click_date_utc"),
    )
    .repartition(8))

(final.writeTo("repro.db.entry_clicks_matched")
    .using("iceberg")
    .partitionedBy("click_date_utc", "match_method")
    .createOrReplace())
log("overwritePartitions-equivalent write complete")
log(f"final rows: {spark.table('repro.db.entry_clicks_matched').count():,}")
log("REPRO COMPLETE")
