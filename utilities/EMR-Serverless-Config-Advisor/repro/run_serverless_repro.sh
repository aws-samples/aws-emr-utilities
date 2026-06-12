#!/bin/bash
# UMP shared-clickstream congestion repro — EMR Serverless submissions.
#
# Prereqs:
#   1. Synthetic data in s3://suthan-synthetic-data/expedia/ (datagen.py --scale 1.0)
#   2. Glue tables created (create_tables.sql with ${DATA}=s3://suthan-synthetic-data/expedia)
#   3. mini-clickstream-repro.jar at s3://suthan-synthetic-data/expedia/jars/
#
# Usage:
#   ./run_serverless_repro.sh <application-id> <execution-role-arn> broken|fixed
#
# Scale rationale (1/15 of production, invariants preserved):
#   BROKEN  — replays the NEW-2 failure shape: 8 Large executors serving ~1.1TB
#             shuffle write → ≥0.076 GB/s/host demanded (> 0.057 observed collapse),
#             350 partitions / 128 cores ≈ 2.7-4 waves/stage,
#             fetch connections/host = min(350,128)*5/8 = 80 (== NEW-2's 80).
#   FIXED   — PR #164 sizing at this scale: 24 Large executors (serving
#             ≤0.04 GB/s/host at 20-min target), 256 partitions ≤ 2 waves.

set -euo pipefail
APP_ID=$1; ROLE_ARN=$2; PROFILE=$3
JAR=s3://suthan-synthetic-data/expedia/jars/mini-clickstream-repro.jar
LOGS=s3://suthan-event-logs/ump-repro-serverless-logs/

COMMON_CONF="--conf spark.executor.cores=16 --conf spark.executor.memory=108G
 --conf spark.driver.cores=16 --conf spark.driver.memory=108G
 --conf spark.dynamicAllocation.enabled=true
 --conf spark.emr-serverless.executor.disk=200G
 --conf spark.emr-serverless.executor.disk.type=shuffle_optimized
 --conf spark.hadoop.hive.metastore.client.factory.class=com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory
 --conf spark.sql.catalogImplementation=hive"

if [ "$PROFILE" = "broken" ]; then
  # NEW-2 shape: starved hosts, wave-stacked partitions
  CONF="$COMMON_CONF
 --conf spark.dynamicAllocation.maxExecutors=8
 --conf spark.dynamicAllocation.minExecutors=8
 --conf spark.dynamicAllocation.initialExecutors=8
 --conf spark.sql.shuffle.partitions=350"
  NAME=ump-repro-broken
elif [ "$PROFILE" = "fixed" ]; then
  # PR #164 sizing at 1/15 scale
  CONF="$COMMON_CONF
 --conf spark.dynamicAllocation.maxExecutors=24
 --conf spark.dynamicAllocation.minExecutors=8
 --conf spark.dynamicAllocation.initialExecutors=8
 --conf spark.sql.shuffle.partitions=256
 --conf spark.sql.adaptive.coalescePartitions.parallelismFirst=false
 --conf spark.sql.optimizer.excludedRules=org.apache.spark.sql.catalyst.optimizer.InferWindowGroupLimit"
  NAME=ump-repro-fixed
else
  echo "profile must be broken|fixed"; exit 1
fi

aws emr-serverless start-job-run \
  --application-id "$APP_ID" \
  --execution-role-arn "$ROLE_ARN" \
  --name "$NAME" \
  --job-driver "{
    \"sparkSubmit\": {
      \"entryPoint\": \"$JAR\",
      \"entryPointArguments\": [\"--sent-date\", \"2026-06-01\", \"--channels\", \"sms,inbox\", \"--output-db\", \"communications\"],
      \"sparkSubmitParameters\": \"--class repro.MiniClickstreamEnrichment $(echo $CONF | tr -s ' ')\"
    }
  }" \
  --configuration-overrides "{
    \"monitoringConfiguration\": {
      \"s3MonitoringConfiguration\": {\"logUri\": \"$LOGS\"}
    }
  }" \
  --query "jobRunId" --output text
