#!/usr/bin/env python3
"""Synthetic dataset generator for the UMP shared-clickstream congestion repro.

Reproduces the *shape* of the four source tables consumed by
clickstream_enrichment_lite_template.sql at a configurable fraction of the
production volumes observed in event log 00g6avf7opopio0b (87TB input run).

Fidelity targets (at --scale 1.0 ≈ 1/15 of production):
  - communications.ingestion_clickstream_base  ~200 GB, 1.1B rows, 31 daily partitions
  - user.keychain_eg_v3                        ~180 GB, 60M rows (map columns, 95% NULL guid/havid)
  - metrics_platform.cks_trvlr_visit_msr_v4    ~130 GB, 1.2B rows
  - communications.sms_engagement_base         ~4 GB, 20M rows (single sent_date partition)
  - communications.inbox_engagement_base       ~4 GB, 20M rows

Skew design (drives the window-function and join skew seen in the event log):
  - ~2% of duaids receive ~40% of clickstream events (hot devices)
  - a small set of engagement recipients have very high engagement-row counts

Usage:
  spark-submit datagen.py --output s3://bucket/ump-repro/data --scale 0.001   # smoke
  spark-submit datagen.py --output s3://bucket/ump-repro/data --scale 1.0     # full repro
"""
import argparse
import sys

from pyspark.sql import SparkSession
import pyspark.sql.functions as F

SENT_DATE = "2026-06-01"          # target partition; window = ±15 days
N_DUAID = 60_000_000              # device id space
N_HOT_DUAID = 1_200_000           # 2% hot devices
N_EXPUSER = 40_000_000            # expuserid space (keychain + engagement)

CS_ROWS_PER_DAY = 36_000_000      # x31 days = 1.116B at scale 1.0
KEYCHAIN_ROWS = 60_000_000
CKS_ROWS = 1_200_000_000
ENGAGEMENT_ROWS = 20_000_000      # per channel

FUNNEL_EVENTS = ["search_results.viewed", "product_details.viewed",
                 "booking_form.viewed", "booking_confirmation.viewed"]


def filler(col_prefix, n, width):
    """n pseudo-random hex filler columns of ~width chars to hit target row size."""
    reps = max(1, width // 32)
    return [F.expr(f"repeat(md5(cast(id*{i + 7} as string)), {reps})").alias(f"{col_prefix}_{i}")
            for i in range(n)]


def duaid_expr():
    """Skewed device id: ~40% of events land on the 2% hot duaid range."""
    return F.expr(f"""
      CASE WHEN pmod(hash(id, 17), 100) < 40
           THEN concat('duaid-', pmod(hash(id, 31), {N_HOT_DUAID}))
           ELSE concat('duaid-', {N_HOT_DUAID} + pmod(hash(id, 47), {N_DUAID - N_HOT_DUAID}))
      END""")


def gen_clickstream(spark, out, scale):
    rows_per_day = max(10_000, int(CS_ROWS_PER_DAY * scale))
    days = [F.date_add(F.lit(SENT_DATE), d) for d in range(-15, 16)]
    print(f"[datagen] clickstream: {rows_per_day} rows/day x 31 days", flush=True)
    for d in range(-15, 16):
        df = (spark.range(rows_per_day)
              .withColumn("event_date", F.date_add(F.lit(SENT_DATE), F.lit(d)))
              .withColumn("duaid", duaid_expr())
              # epoch millis within the day; 30-min sessions => visit_id shared by ~6 events
              .withColumn("day_ms", F.unix_timestamp(F.col("event_date")) * 1000)
              .withColumn("event_timestamp",
                          F.expr("day_ms + pmod(hash(id, 13), 86400000)"))
              .withColumn("visit_ts", F.expr("event_timestamp - pmod(event_timestamp, 1800000)"))
              .withColumn("visit_id", F.expr("concat('v', substr(duaid, 7), '_', visit_ts)"))
              .withColumn("event_type",
                          F.expr("CASE WHEN pmod(hash(id, 3), 10) < 6 THEN 'Page View' "
                                 "WHEN pmod(hash(id, 3), 10) < 8 THEN 'Interaction' "
                                 "ELSE 'System' END"))
              .withColumn("event_name",
                          F.expr("CASE pmod(hash(id, 5), 100) "
                                 " WHEN 0 THEN 'booking_confirmation.viewed'"
                                 " WHEN 1 THEN 'booking_form.viewed'"
                                 " WHEN 2 THEN 'booking_form.viewed'"
                                 + "".join(f" WHEN {3 + i} THEN 'product_details.viewed'" for i in range(5))
                                 + "".join(f" WHEN {8 + i} THEN 'search_results.viewed'" for i in range(8))
                                 + " ELSE concat('other.event.', pmod(hash(id, 5), 40)) END"))
              .withColumn("eg_account_id",
                          F.expr(f"CASE WHEN pmod(hash(id, 7), 10) < 6 "
                                 f"THEN concat('egaid:bex:exp', pmod(hash(id, 11), {N_EXPUSER})) END"))
              .withColumn("brand_customer_id",
                          F.expr(f"CASE WHEN pmod(hash(id, 19), 10) < 5 "
                                 f"THEN concat('bcid', pmod(hash(id, 23), {N_EXPUSER})) END"))
              .withColumn("eg_user_id", F.expr("concat('egu', pmod(hash(id, 29), 50000000))"))
              .withColumn("event_ts_pst", F.expr("from_unixtime(event_timestamp / 1000)").cast("timestamp"))
              .withColumn("local_dtm", F.col("event_ts_pst"))
              .withColumn("funnel_brand",
                          F.expr("CASE pmod(hash(id, 37), 10) WHEN 0 THEN NULL WHEN 1 THEN NULL WHEN 2 THEN NULL "
                                 "WHEN 3 THEN 'Hotels.com' WHEN 4 THEN 'Vrbo' WHEN 5 THEN 'CheapTickets' "
                                 "ELSE 'Brand Expedia' END"))
              .withColumn("lob",
                          F.expr("CASE pmod(hash(id, 41), 12) WHEN 0 THEN NULL WHEN 1 THEN 'Lodging' "
                                 "WHEN 2 THEN 'Lodging' WHEN 3 THEN 'Lodging' WHEN 4 THEN 'Air' WHEN 5 THEN 'Air' "
                                 "WHEN 6 THEN 'Car' WHEN 7 THEN 'Activity' WHEN 8 THEN 'Package' "
                                 "WHEN 9 THEN 'Cruise' ELSE 'Lodging' END"))
              .withColumn("device_type", F.expr("CASE pmod(hash(id,43),3) WHEN 0 THEN 'MOBILE' WHEN 1 THEN 'DESKTOP' ELSE 'TABLET' END"))
              .withColumn("device_os", F.expr("CASE pmod(hash(id,53),3) WHEN 0 THEN 'iOS' WHEN 1 THEN 'Android' ELSE 'Other' END"))
              .withColumn("domain", F.expr("concat('www.expedia.', CASE pmod(hash(id,59),3) WHEN 0 THEN 'com' WHEN 1 THEN 'co.uk' ELSE 'ca' END)"))
              .withColumn("site_name", F.expr("upper(substr(domain, instr(domain, '.') + 1))"))
              .withColumn("visit_mktg_code", F.expr("concat('MKT', pmod(hash(id,61),1000))"))
              .withColumn("email_omni_code", F.expr("concat('EMLCID=', pmod(hash(id,67),100000), '&EMLDTL=D', pmod(hash(id,71),100))"))
              .withColumn("experience_type", F.lit("WEB"))
              .withColumn("uis_prime_referrer_id", F.expr("cast(pmod(hash(id,73),1000000) as string)"))
              .select("*", *filler("pad", 2, 32))
              .drop("id", "day_ms", "visit_ts"))
        (df.repartition(max(8, int(48 * scale)))
           .write.mode("overwrite")
           .parquet(f"{out}/communications/ingestion_clickstream_base/event_date={SENT_DATE[:8]}{'%02d' % 1}" if False
                    else f"{out}/communications/ingestion_clickstream_base/event_date=" +
                         str(df.select(F.col('event_date').cast('string')).first()[0])))
        print(f"[datagen] clickstream day {d:+d} done", flush=True)


def gen_keychain(spark, out, scale):
    rows = max(5_000, int(KEYCHAIN_ROWS * scale))
    print(f"[datagen] keychain: {rows} rows", flush=True)
    df = (spark.range(rows)
          .withColumn("keychain_id", F.expr("concat('kc', id)"))
          # expuserid: 1-3 entries; key space matches engagement recipient ids
          .withColumn("expuserid", F.expr(f"""
              map_from_arrays(
                transform(sequence(1, 1 + pmod(hash(id, 3), 3)),
                          i -> concat('exp', pmod(hash(id, 11) + i * 7919, {N_EXPUSER}))),
                transform(sequence(1, 1 + pmod(hash(id, 3), 3)),
                          i -> named_struct('key_last_visit_date',
                                            date_sub(current_date(), pmod(hash(id, 13) + i, 700)))))"""))
          # guid / havid: NULL for ~95% of rows (drives the MAP_CONCAT-null bug shape)
          .withColumn("guid", F.expr(f"""
              CASE WHEN pmod(hash(id, 17), 100) < 5 THEN
                map(concat('g', pmod(hash(id, 19), {N_DUAID})),
                    named_struct('key_last_visit_date', date_sub(current_date(), pmod(hash(id, 23), 700))))
              END"""))
          .withColumn("havid", F.expr(f"""
              CASE WHEN pmod(hash(id, 29), 100) < 5 THEN
                map(concat('h', pmod(hash(id, 31), {N_DUAID})),
                    named_struct('key_last_visit_date', date_sub(current_date(), pmod(hash(id, 37), 700))))
              END"""))
          # device_user_agent_id: 1-4 duaids overlapping the clickstream duaid space
          .withColumn("device_user_agent_id", F.expr(f"""
              map_from_arrays(
                transform(sequence(1, 1 + pmod(hash(id, 41), 4)),
                          i -> CASE WHEN pmod(hash(id, 43) + i, 100) < 40
                                    THEN concat('duaid-', pmod(hash(id, 47) + i * 104729, {N_HOT_DUAID}))
                                    ELSE concat('duaid-', {N_HOT_DUAID} + pmod(hash(id, 53) + i * 104729, {N_DUAID - N_HOT_DUAID}))
                               END),
                transform(sequence(1, 1 + pmod(hash(id, 41), 4)),
                          i -> named_struct('key_last_visit_date',
                                            date_sub(current_date(), pmod(hash(id, 59) + i, 700)))))"""))
          .select("*", *filler("pad", 4, 640))
          .drop("id"))
    df.repartition(max(8, int(180 * scale))).write.mode("overwrite").parquet(f"{out}/user/keychain_eg_v3")
    print("[datagen] keychain done", flush=True)


def gen_cks(spark, out, scale):
    rows = max(20_000, int(CKS_ROWS * scale))
    print(f"[datagen] cks: {rows} rows", flush=True)
    df = (spark.range(rows)
          # 60% of visit_ids reconstruct real clickstream visit ids (same formula)
          .withColumn("day_off", F.expr("pmod(hash(id, 3), 31) - 15"))
          .withColumn("visit_date", F.expr(f"date_add(date('{SENT_DATE}'), day_off)"))
          .withColumn("day_ms", F.unix_timestamp(F.col("visit_date")) * 1000)
          .withColumn("visit_id", F.expr(f"""
              CASE WHEN pmod(hash(id, 5), 10) < 6 THEN
                concat('v',
                  CASE WHEN pmod(hash(id, 17), 100) < 40
                       THEN cast(pmod(hash(id, 31), {N_HOT_DUAID}) as string)
                       ELSE cast({N_HOT_DUAID} + pmod(hash(id, 47), {N_DUAID - N_HOT_DUAID}) as string) END,
                  '_', day_ms + pmod(hash(id, 13), 86400000) - pmod(hash(id, 13), 1800000))
              ELSE concat('x', id) END"""))
          .withColumn("line_of_business",
                      F.expr("CASE pmod(hash(id, 7), 10) WHEN 0 THEN 'AIR' WHEN 1 THEN 'AIR' "
                             "WHEN 2 THEN 'CAR' WHEN 3 THEN 'CRUISE' WHEN 4 THEN 'PACKAGE' "
                             "WHEN 5 THEN 'ALL' ELSE 'LODGING' END"))
          .withColumn("shopping_visit_flag", F.expr("pmod(hash(id, 11), 10) < 7"))
          .withColumn("shopping_cvr_flag", F.expr("pmod(hash(id, 19), 100) < 15"))
          .withColumn("booking_flag", F.expr("pmod(hash(id, 23), 100) < 8"))
          .select("*", *filler("pad", 1, 64))
          .drop("id", "day_off", "day_ms"))
    df.repartition(max(8, int(130 * scale))).write.mode("overwrite") \
      .partitionBy("visit_date").parquet(f"{out}/metrics_platform/cks_trvlr_visit_msr_v4")
    print("[datagen] cks done", flush=True)


def gen_engagement(spark, out, scale, channel):
    rows = max(5_000, int(ENGAGEMENT_ROWS * scale))
    omni = "sms_omni_code" if channel == "sms" else "inbox_omni_code"
    print(f"[datagen] {channel} engagement: {rows} rows", flush=True)
    df = (spark.range(rows)
          # hot recipients: 1% of recipients own ~30% of rows (window-skew driver)
          .withColumn("recipient_id", F.expr(f"""
              CASE WHEN pmod(hash(id, 3), 100) < 30
                   THEN concat('egaid:bex:exp', pmod(hash(id, 7), {N_EXPUSER // 100}))
                   ELSE concat('egaid:bex:exp', pmod(hash(id, 11), {N_EXPUSER}))
              END"""))
          .withColumn(omni, F.expr("concat('OMNI', pmod(hash(id, 13), 500000))"))
          .withColumn("brand", F.expr("CASE pmod(hash(id, 17), 3) WHEN 0 THEN 'BEX' WHEN 1 THEN 'HCOM' ELSE 'VRBO' END"))
          .withColumn("communication_type", F.lit("MKTG" if channel == "sms" else "TRIP"))
          .withColumn("send_ts", F.expr(f"cast(unix_timestamp(date('{SENT_DATE}')) + pmod(hash(id, 19), 86400) as timestamp)"))
          .withColumn("sent_date", F.lit(SENT_DATE).cast("date"))
          .select("*", *filler("pad", 1, 64))
          .drop("id"))
    if channel == "sms":
        df = (df.withColumnRenamed("send_ts", "sfmc_send_timestamp_pst")
                .withColumn("max_timestamp", F.expr("sfmc_send_timestamp_pst + INTERVAL 4 HOURS"))
                .withColumn("eg_account_id", F.expr("substr(recipient_id, 11)")))
    else:
        df = df.withColumnRenamed("send_ts", "sent_time")
    df.repartition(8).write.mode("overwrite").parquet(
        f"{out}/communications/{channel}_engagement_base/sent_date={SENT_DATE}")
    print(f"[datagen] {channel} engagement done", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--scale", type=float, default=0.001)
    p.add_argument("--only", default="all",
                   choices=["all", "clickstream", "keychain", "cks", "engagement"])
    a = p.parse_args()
    out = a.output.rstrip("/")

    spark = (SparkSession.builder.appName(f"ump-repro-datagen-{a.scale}")
             .config("spark.sql.parquet.compression.codec", "snappy")
             .getOrCreate())

    if a.only in ("all", "keychain"):
        gen_keychain(spark, out, a.scale)
    if a.only in ("all", "engagement"):
        gen_engagement(spark, out, a.scale, "sms")
        gen_engagement(spark, out, a.scale, "inbox")
    if a.only in ("all", "cks"):
        gen_cks(spark, out, a.scale)
    if a.only in ("all", "clickstream"):
        gen_clickstream(spark, out, a.scale)
    print("[datagen] ALL DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
