#!/usr/bin/env bash
#
# Generic driver for the EMR 8.1 multi-catalog demo. Submits multicatalog_demo.py
# to Amazon EMR (on EC2 or Serverless), waits for completion, and prints the
# driver output (the PASS/FAIL summary and query results).
#
# Target EMR on EC2 (default):
#   ./run_demo.sh --phase <phase> --cluster-id <j-XXX> --bucket <bucket> [--region us-east-1]
#
# Target EMR Serverless:
#   ./run_demo.sh --target serverless --phase <phase> \
#       --app-id <id> --role-arn <arn> --bucket <bucket> [--region us-east-1]
#
# Common optional flags: --producer-account 111122223333 --producer-db salesdb
#   --producer-table fulfillment --named-catalog prod --db salesdb
#
# Phases: setup | multiformat (alias: query) | named-local | producer-setup |
#         xacct-named | xacct-autowire | cleanup | all
#
# Values may also be supplied via a .env file (see env.template) so you do not
# repeat --cluster-id / --app-id / --role-arn / --bucket on every command.
set -euo pipefail

# ---- load .env (if present) ------------------------------------------------
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _envf in "./.env" "${SELF_DIR}/../.env" "${SELF_DIR}/.env"; do
  [[ -f "$_envf" ]] && { set -a; . "$_envf"; set +a; break; }
done

# ---- defaults / parse flags ------------------------------------------------
TARGET="${TARGET:-ec2}"
PHASE="" CLUSTER_ID="${CLUSTER_ID:-}" APP_ID="${APP_ID:-}" ROLE_ARN="${ROLE_ARN:-}"
BUCKET="${BUCKET:-}" REGION="${REGION:-us-east-1}"
DB="salesdb" PRODUCER_ACCOUNT="${PRODUCER_ACCOUNT:-}" PRODUCER_DB="salesdb" PRODUCER_TABLE="fulfillment"
NAMED_CATALOG="prod" ENDPOINT="${ENDPOINT:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)            TARGET="$2"; shift 2;;
    --phase)             PHASE="$2"; shift 2;;
    --cluster-id)        CLUSTER_ID="$2"; shift 2;;
    --app-id)            APP_ID="$2"; shift 2;;
    --role-arn)          ROLE_ARN="$2"; shift 2;;
    --bucket)            BUCKET="$2"; shift 2;;
    --region)            REGION="$2"; shift 2;;
    --db)                DB="$2"; shift 2;;
    --producer-account)  PRODUCER_ACCOUNT="$2"; shift 2;;
    --producer-db)       PRODUCER_DB="$2"; shift 2;;
    --producer-table)    PRODUCER_TABLE="$2"; shift 2;;
    --named-catalog)     NAMED_CATALOG="$2"; shift 2;;
    --endpoint)          ENDPOINT="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

# 'query' is a friendly alias for the multi-format join phase
[[ "$PHASE" == "query" ]] && PHASE="multiformat"

[[ -z "$PHASE" ]]  && { echo "missing --phase" >&2; exit 2; }
[[ -z "$BUCKET" ]] && { echo "missing --bucket (or set BUCKET in .env)" >&2; exit 2; }
case "$TARGET" in
  ec2)        [[ -z "$CLUSTER_ID" ]] && { echo "missing --cluster-id (or CLUSTER_ID in .env)" >&2; exit 2; };;
  serverless) for r in APP_ID ROLE_ARN; do [[ -z "${!r}" ]] && { echo "missing --${r,,} (serverless)" >&2; exit 2; }; done;;
  *) echo "unknown --target: $TARGET (use ec2 or serverless)" >&2; exit 2;;
esac

PREFIX="s3://${BUCKET}/multicatalog"
SCRIPTS="${PREFIX}/scripts"; WAREHOUSE="${PREFIX}/warehouse"; LOGS="${PREFIX}/logs"

# ---- Spark conf ------------------------------------------------------------
RSC="org.apache.spark.sql.connector.catalog.redirecting.RedirectingSessionCatalog"
EXT="org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions,io.delta.sql.DeltaSparkSessionExtension,org.apache.spark.sql.hudi.HoodieSparkSessionExtension"
KRYO="org.apache.spark.serializer.KryoSerializer"
GLUE_FACTORY="com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory"

CONF="--conf spark.sql.catalog.spark_catalog=${RSC}"
CONF+=" --conf spark.sql.extensions=${EXT}"
CONF+=" --conf spark.serializer=${KRYO}"
CONF+=" --conf spark.hadoop.hive.metastore.client.factory.class=${GLUE_FACTORY}"
CONF+=" --conf spark.sql.catalogImplementation=hive"

case "$PHASE" in
  named-local)
    LOCAL_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
    CONF+=" --conf spark.sql.catalog.cat2=${RSC}"
    CONF+=" --conf spark.sql.catalog.cat2.metastore.type=glue"
    CONF+=" --conf spark.sql.catalog.cat2.metastore.hadoop.hive.metastore.glue.catalogid=${LOCAL_ACCOUNT}"
    ;;
  xacct-named)
    [[ -z "$PRODUCER_ACCOUNT" ]] && { echo "--producer-account required for xacct-named" >&2; exit 2; }
    CONF+=" --conf spark.sql.catalog.${NAMED_CATALOG}=${RSC}"
    CONF+=" --conf spark.sql.catalog.${NAMED_CATALOG}.metastore.type=glue"
    CONF+=" --conf spark.sql.catalog.${NAMED_CATALOG}.metastore.hadoop.hive.metastore.glue.catalogid=${PRODUCER_ACCOUNT}"
    ;;
  xacct-autowire)
    [[ -z "$PRODUCER_ACCOUNT" ]] && { echo "--producer-account required for xacct-autowire" >&2; exit 2; }
    CONF+=" --conf spark.sql.catalogResolver=com.amazonaws.glue.catalog.redirecting.GlueCatalogResolver"
    CONF+=" --conf spark.sql.catalogResolver.region=${REGION}"
    ;;
  all)
    if [[ -n "$PRODUCER_ACCOUNT" ]]; then
      CONF+=" --conf spark.sql.catalog.${NAMED_CATALOG}=${RSC}"
      CONF+=" --conf spark.sql.catalog.${NAMED_CATALOG}.metastore.type=glue"
      CONF+=" --conf spark.sql.catalog.${NAMED_CATALOG}.metastore.hadoop.hive.metastore.glue.catalogid=${PRODUCER_ACCOUNT}"
      CONF+=" --conf spark.sql.catalogResolver=com.amazonaws.glue.catalog.redirecting.GlueCatalogResolver"
      CONF+=" --conf spark.sql.catalogResolver.region=${REGION}"
    fi
    ;;
esac

# ---- worker arguments ------------------------------------------------------
# space-separated form (EC2 spark-submit CLI)
PYARGS="--phase ${PHASE} --warehouse ${WAREHOUSE} --db ${DB} --producer-db ${PRODUCER_DB} --producer-table ${PRODUCER_TABLE} --named-catalog ${NAMED_CATALOG}"
[[ -n "$PRODUCER_ACCOUNT" ]] && PYARGS+=" --producer-account ${PRODUCER_ACCOUNT}"
# JSON array form (Serverless entryPointArguments)
ARGS="[\"--phase\",\"${PHASE}\",\"--warehouse\",\"${WAREHOUSE}\",\"--db\",\"${DB}\""
ARGS+=",\"--producer-db\",\"${PRODUCER_DB}\",\"--producer-table\",\"${PRODUCER_TABLE}\",\"--named-catalog\",\"${NAMED_CATALOG}\""
[[ -n "$PRODUCER_ACCOUNT" ]] && ARGS+=",\"--producer-account\",\"${PRODUCER_ACCOUNT}\""
ARGS+="]"

# ---- clean warehouse before a fresh setup ----------------------------------
if [[ "$PHASE" == "setup" || "$PHASE" == "all" ]]; then
  echo ">> clearing warehouse for a clean setup: ${WAREHOUSE}"
  aws s3 rm "${WAREHOUSE}" --recursive --region "$REGION" >/dev/null 2>&1 || true
fi

# ---- upload worker ---------------------------------------------------------
echo ">> uploading multicatalog_demo.py to ${SCRIPTS}/"
aws s3 cp "${SELF_DIR}/multicatalog_demo.py" "${SCRIPTS}/multicatalog_demo.py" --region "$REGION" >/dev/null

# ============================================================================
if [[ "$TARGET" == "ec2" ]]; then
  # ---- EMR on EC2: submit a step that spark-submits and tees output to S3 ---
  OUT_KEY="${LOGS}/${PHASE}-out.txt"
  RUNNER_KEY="${SCRIPTS}/_ec2_run_${PHASE}.sh"
  RUNNER="$(mktemp)"
  cat > "$RUNNER" <<EOF
#!/bin/bash
set -x
aws s3 cp ${SCRIPTS}/multicatalog_demo.py /tmp/multicatalog_demo.py
spark-submit --deploy-mode client ${CONF} /tmp/multicatalog_demo.py ${PYARGS} > /tmp/mc_out.txt 2>&1
echo "EXIT=\$?" >> /tmp/mc_out.txt
aws s3 cp /tmp/mc_out.txt ${OUT_KEY}
EOF
  aws s3 cp "$RUNNER" "$RUNNER_KEY" --region "$REGION" >/dev/null
  echo ">> submitting step to cluster ${CLUSTER_ID} (phase '${PHASE}')"
  STEP=$(aws emr add-steps --cluster-id "$CLUSTER_ID" --region "$REGION" \
    --steps "Type=CUSTOM_JAR,Name=multicatalog-${PHASE},Jar=command-runner.jar,ActionOnFailure=CONTINUE,Args=[bash,-c,aws s3 cp ${RUNNER_KEY} /tmp/r.sh && bash /tmp/r.sh]" \
    --query 'StepIds[0]' --output text)
  echo ">> stepId=${STEP}"
  while true; do
    ST=$(aws emr describe-step --cluster-id "$CLUSTER_ID" --step-id "$STEP" --region "$REGION" --query 'Step.Status.State' --output text)
    echo "   state=${ST}"
    case "$ST" in COMPLETED|FAILED|CANCELLED) break;; esac
    sleep 15
  done
  echo ">> worker output (${OUT_KEY}):"
  aws s3 cp "$OUT_KEY" - --region "$REGION" 2>/dev/null || echo "   (output not found; check step logs)"
  RC=$(aws s3 cp "$OUT_KEY" - --region "$REGION" 2>/dev/null | grep -oE 'EXIT=[0-9]+' | tail -1 | cut -d= -f2)
  [[ "$ST" == "COMPLETED" && "${RC:-1}" == "0" ]] || { echo ">> phase ${PHASE} did not succeed"; exit 1; }

else
  # ---- EMR Serverless: start a job run and fetch driver stdout --------------
  EP_FLAG=""; [[ -n "$ENDPOINT" ]] && EP_FLAG="--endpoint-url $ENDPOINT"
  echo ">> submitting phase '${PHASE}' to application ${APP_ID}"
  JR=$(aws emr-serverless start-job-run $EP_FLAG --region "$REGION" \
        --application-id "$APP_ID" --execution-role-arn "$ROLE_ARN" \
        --name "multicatalog-demo-${PHASE}" \
        --job-driver "{\"sparkSubmit\":{\"entryPoint\":\"${SCRIPTS}/multicatalog_demo.py\",\"entryPointArguments\":${ARGS},\"sparkSubmitParameters\":\"${CONF}\"}}" \
        --configuration-overrides "{\"monitoringConfiguration\":{\"s3MonitoringConfiguration\":{\"logUri\":\"${LOGS}/\"}}}" \
        --query 'jobRunId' --output text)
  echo ">> jobRunId=${JR}"
  while true; do
    ST=$(aws emr-serverless get-job-run $EP_FLAG --region "$REGION" \
          --application-id "$APP_ID" --job-run-id "$JR" --query 'jobRun.state' --output text)
    echo "   state=${ST}"
    case "$ST" in SUCCESS|FAILED|CANCELLED) break;; esac
    sleep 15
  done
  OUT="${LOGS}/applications/${APP_ID}/jobs/${JR}/SPARK_DRIVER/stdout.gz"
  echo ">> driver stdout (${OUT}):"
  if aws s3 cp "$OUT" - --region "$REGION" 2>/dev/null | gunzip -c 2>/dev/null; then :; else
    echo "   (stdout not found yet; fetch manually from ${OUT})"; fi
  [[ "$ST" == "SUCCESS" ]] || { echo ">> job ${ST}"; exit 1; }
fi

# ---- cleanup phase also wipes the S3 warehouse -----------------------------
if [[ "$PHASE" == "cleanup" ]]; then
  echo ">> wiping ${WAREHOUSE}"
  aws s3 rm "${WAREHOUSE}" --recursive --region "$REGION" >/dev/null 2>&1 || true
fi
