package repro;

import org.apache.spark.sql.Dataset;
import org.apache.spark.sql.Row;
import org.apache.spark.sql.SparkSession;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;

/**
 * Clean-room reproduction driver for the UMP shared-clickstream enrichment job.
 *
 * NOT the customer's code. Re-implements only the orchestration behavior observed
 * in the event log of job 00g6avf7opopio0b and the public strings of the customer
 * JAR's SharedClickstreamEnrichment driver:
 *   - per-channel cached engagement temp view filtered on sent_date
 *   - channel SQL = clickstream_enrichment_lite_template.sql (bundled byte-identical)
 *     with channel parameters injected
 *   - channels executed CONCURRENTLY on one SparkSession (the original uses Futures)
 *   - result written via saveAsTable overwrite (HiveWriter.fullRefresh equivalent)
 *
 * Tables read (synthetic, created by datagen.py + create_tables.sql):
 *   communications.{sms,inbox}_engagement_base
 *   communications.ingestion_clickstream_base
 *   user.keychain_eg_v3
 *   metrics_platform.cks_trvlr_visit_msr_v4
 *
 * Usage:
 *   spark-submit --class repro.MiniClickstreamEnrichment mini-clickstream-repro.jar \
 *     --sent-date 2026-06-01 --channels sms,inbox --output-db communications
 */
public final class MiniClickstreamEnrichment {

    private static final String TEMPLATE = "/sql/clickstream_enrichment_lite_template.sql";

    private static final class Channel {
        final String name, omniCol, sendTsCol, timeDeltaCol, joinKey;
        Channel(String name, String omniCol, String sendTsCol, String timeDeltaCol, String joinKey) {
            this.name = name; this.omniCol = omniCol; this.sendTsCol = sendTsCol;
            this.timeDeltaCol = timeDeltaCol; this.joinKey = joinKey;
        }
    }

    // Parameter values per the template header comments (lines 15-25).
    private static Channel channel(String name) {
        switch (name) {
            case "sms":
                return new Channel("sms", "sms_omni_code", "sfmc_send_timestamp_pst", "max_timestamp",
                        "COALESCE(CONCAT('egaid:bex:', c.brand_customer_id), c.eg_account_id)");
            case "inbox":
                return new Channel("inbox", "inbox_omni_code", "sent_time", "sent_time",
                        "COALESCE(c.eg_account_id, CONCAT('egaid:bex:', c.brand_customer_id))");
            default:
                throw new IllegalArgumentException("unknown channel: " + name);
        }
    }

    public static void main(String[] args) throws Exception {
        String sentDate = "2026-06-01";
        String channels = "sms,inbox";
        String outputDb = "communications";
        for (int i = 0; i < args.length - 1; i++) {
            if ("--sent-date".equals(args[i])) sentDate = args[i + 1];
            if ("--channels".equals(args[i])) channels = args[i + 1];
            if ("--output-db".equals(args[i])) outputDb = args[i + 1];
        }
        final String startDate = LocalDate.parse(sentDate).minusDays(15).toString();
        final String endDate = LocalDate.parse(sentDate).plusDays(15).toString();

        final SparkSession spark = SparkSession.builder()
                .appName("ump-analytics-pipelines")   // same app name => same recommender grouping
                .enableHiveSupport()
                .getOrCreate();

        final String template = readResource(TEMPLATE);
        final String finalSentDate = sentDate, finalOutputDb = outputDb;

        // Per-channel cached engagement views (driver registers these before the template runs).
        List<Thread> threads = new ArrayList<>();
        List<Throwable> failures = new ArrayList<>();
        for (String chName : channels.split(",")) {
            final Channel ch = channel(chName.trim());
            String cachedView = ch.name + "_engagement_cached";
            Dataset<Row> eng = spark.sql(
                    "SELECT * FROM communications." + ch.name + "_engagement_base" +
                    " WHERE sent_date >= DATE('" + sentDate + "') AND sent_date <= DATE('" + sentDate + "')");
            eng.createOrReplaceTempView(cachedView);
            spark.sql("CACHE TABLE " + cachedView);
            System.out.println("[repro] " + cachedView + " cached for " + sentDate);

            final String sql = template
                    .replace("{engagement_cached_view}", cachedView)
                    .replace("{engagement_base}", ch.name + "_engagement_base")
                    .replace("{omni_code_col}", ch.omniCol)
                    .replace("{send_timestamp_col}", ch.sendTsCol)
                    .replace("{time_delta_order_col}", ch.timeDeltaCol)
                    .replace("{cs_shopping_join_key}", ch.joinKey)
                    .replace("{max_clicks_expr}", "0")
                    .replace("{sent_date}", finalSentDate)
                    .replace("{start_date}", startDate)
                    .replace("{end_date}", endDate);

            Thread t = new Thread(() -> {
                try {
                    System.out.println("[repro] Processing clickstream enrichment for " + ch.name);
                    String outTable = finalOutputDb + "." + ch.name + "_clickstream_enrichment_repro";
                    spark.sql(sql).write().mode("overwrite").format("parquet").saveAsTable(outTable);
                    long n = spark.table(outTable).count();
                    System.out.println("[repro] Written " + n + " rows to " + outTable);
                } catch (Throwable e) {
                    synchronized (failures) { failures.add(e); }
                    System.err.println("[repro] Clickstream enrichment failed for " + ch.name + ": " + e);
                }
            }, "channel-" + ch.name);
            threads.add(t);
        }
        for (Thread t : threads) t.start();
        for (Thread t : threads) t.join();

        if (!failures.isEmpty()) {
            failures.forEach(Throwable::printStackTrace);
            spark.stop();
            System.exit(1);
        }
        System.out.println("[repro] SharedClickstreamEnrichment repro complete");
        spark.stop();
    }

    private static String readResource(String path) throws Exception {
        try (InputStream in = MiniClickstreamEnrichment.class.getResourceAsStream(path)) {
            if (in == null) throw new IllegalStateException("resource not found: " + path);
            try (BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
                return r.lines().collect(Collectors.joining("\n"));
            }
        }
    }
}
