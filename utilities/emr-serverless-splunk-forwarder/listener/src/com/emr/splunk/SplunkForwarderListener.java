package com.emr.splunk;

import org.apache.spark.scheduler.SparkListener;
import org.apache.spark.scheduler.SparkListenerApplicationStart;

/**
 * Spark listener that starts the Splunk Universal Forwarder when a Spark application begins.
 * This allows log forwarding without modifying application code.
 *
 * Usage: --conf spark.extraListeners=com.emr.splunk.SplunkForwarderListener
 */
public class SplunkForwarderListener extends SparkListener {
    @Override
    public void onApplicationStart(SparkListenerApplicationStart event) {
        try {
            ProcessBuilder pb = new ProcessBuilder("/usr/local/bin/start-splunk.sh");
            pb.inheritIO();
            Process p = pb.start();
            p.waitFor();
        } catch (Exception e) {
            System.err.println("SplunkForwarderListener: Failed to start Splunk UF: " + e.getMessage());
        }
    }
}
