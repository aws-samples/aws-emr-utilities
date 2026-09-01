#!/usr/bin/env bash
#
# Cross-account demo bootstrap — PRODUCER side.
# Run this with the PRODUCER account's credentials. It:
#   1. creates an S3 bucket for the producer data (if needed),
#   2. uploads a tiny CSV and registers it as a Hive table in the Glue Data Catalog,
#   3. grants the CONSUMER role read access across the four layers:
#        - Lake Formation (IAM_ALLOWED_PRINCIPALS hybrid mode)
#        - Glue Data Catalog resource policy (incl. database/default)
#        - Amazon S3 bucket policy
#   (KMS: this demo leaves the catalog unencrypted. If your catalog uses a
#    customer-managed key, add kms:Decrypt for the consumer role to that key policy.
#    The AWS-managed aws/glue key CANNOT be shared cross-account.)
#
# Usage:
#   ./bootstrap_producer.sh --consumer-role arn:aws:iam::444455556666:role/<role> \
#       [--region us-east-1] [--bucket <bucket>] [--db salesdb] [--table fulfillment]
set -euo pipefail

REGION="us-east-1" BUCKET="" DB="salesdb" TABLE="fulfillment" CONSUMER_ROLE="" DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --consumer-role) CONSUMER_ROLE="$2"; shift 2;;
    --region)        REGION="$2"; shift 2;;
    --bucket)        BUCKET="$2"; shift 2;;
    --db)            DB="$2"; shift 2;;
    --table)         TABLE="$2"; shift 2;;
    --dry-run)       DRY_RUN=true; shift;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done
[[ -z "$CONSUMER_ROLE" ]] && { echo "missing --consumer-role" >&2; exit 2; }
$DRY_RUN && echo ">> DRY RUN — no resources will be created or modified (read-only calls still run)"

# run a MUTATING command, or just print it under --dry-run
run() { if $DRY_RUN; then echo "  [dry-run] $*"; else "$@"; fi; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
if [[ -z "$ACCOUNT" ]]; then
  $DRY_RUN && ACCOUNT="<producer-account-id>" || { echo "cannot determine caller account (invalid/expired creds?)" >&2; exit 1; }
fi
BUCKET="${BUCKET:-xacct-demo-${ACCOUNT}-${REGION//-/}}"
LOCATION="s3://${BUCKET}/${DB}/${TABLE}"
echo ">> producer account=${ACCOUNT} region=${REGION} bucket=${BUCKET}"
echo ">> table=${DB}.${TABLE} location=${LOCATION}"
echo ">> granting consumer role: ${CONSUMER_ROLE}"

# 1. bucket -------------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [[ "$REGION" == "us-east-1" ]]; then
    run aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    run aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
  echo "   created bucket ${BUCKET}"
fi

# 2. sample data + Glue table -------------------------------------------------
TMP=$(mktemp)
printf '1,prod-1\n2,prod-2\n3,prod-3\n' > "$TMP"
run aws s3 cp "$TMP" "${LOCATION}/data.csv" --region "$REGION"
rm -f "$TMP"

if $DRY_RUN; then
  echo "  [dry-run] aws glue create-database --database-input {\"Name\":\"${DB}\"}"
else
  aws glue create-database --region "$REGION" --database-input "{\"Name\":\"${DB}\"}" 2>/dev/null || true
fi

cat > /tmp/table-input.json <<EOF
{
  "Name": "${TABLE}",
  "TableType": "EXTERNAL_TABLE",
  "Parameters": {"classification": "csv", "EXTERNAL": "TRUE"},
  "StorageDescriptor": {
    "Columns": [{"Name": "id", "Type": "int"}, {"Name": "val", "Type": "string"}],
    "Location": "${LOCATION}",
    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
    "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
    "SerdeInfo": {
      "SerializationLibrary": "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
      "Parameters": {"field.delim": ","}
    }
  }
}
EOF
run aws glue delete-table --database-name "$DB" --name "$TABLE" --region "$REGION" 2>/dev/null || true
run aws glue create-table --database-name "$DB" --region "$REGION" \
  --table-input file:///tmp/table-input.json
echo "   registered ${DB}.${TABLE}"

# 3. Lake Formation grant (IAM_ALLOWED_PRINCIPALS / hybrid mode) --------------
run aws lakeformation grant-permissions --region "$REGION" \
  --principal DataLakePrincipalIdentifier=IAM_ALLOWED_PRINCIPALS \
  --resource "{\"Table\":{\"CatalogId\":\"${ACCOUNT}\",\"DatabaseName\":\"${DB}\",\"Name\":\"${TABLE}\"}}" \
  --permissions SELECT DESCRIBE 2>/dev/null || echo "   (LF table grant skipped/existing)"
run aws lakeformation grant-permissions --region "$REGION" \
  --principal DataLakePrincipalIdentifier=IAM_ALLOWED_PRINCIPALS \
  --resource "{\"Database\":{\"CatalogId\":\"${ACCOUNT}\",\"Name\":\"${DB}\"}}" \
  --permissions DESCRIBE 2>/dev/null || echo "   (LF db grant skipped/existing)"

# 4. Glue resource policy (MUST include database/default) ---------------------
cat > /tmp/glue-resource-policy.json <<EOF
{ "Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Principal": {"AWS": "${CONSUMER_ROLE}"},
  "Action": ["glue:GetCatalog","glue:GetDatabase","glue:GetDatabases",
             "glue:GetTable","glue:GetTables","glue:GetPartition","glue:GetPartitions"],
  "Resource": ["arn:aws:glue:${REGION}:${ACCOUNT}:catalog",
               "arn:aws:glue:${REGION}:${ACCOUNT}:database/*",
               "arn:aws:glue:${REGION}:${ACCOUNT}:table/*/*"]
}]}
EOF
run aws glue put-resource-policy --region "$REGION" \
  --policy-in-json file:///tmp/glue-resource-policy.json --enable-hybrid TRUE
echo "   applied Glue resource policy"

# 5. S3 bucket policy ---------------------------------------------------------
cat > /tmp/s3-bucket-policy.json <<EOF
{ "Version": "2012-10-17", "Statement": [{
  "Sid": "AllowConsumerRead",
  "Effect": "Allow",
  "Principal": {"AWS": "${CONSUMER_ROLE}"},
  "Action": ["s3:GetObject","s3:ListBucket"],
  "Resource": ["arn:aws:s3:::${BUCKET}","arn:aws:s3:::${BUCKET}/*"]
}]}
EOF
run aws s3api put-bucket-policy --bucket "$BUCKET" --region "$REGION" \
  --policy file:///tmp/s3-bucket-policy.json
echo "   applied S3 bucket policy"

echo ""
echo ">> DONE. Producer setup complete."
echo "   Producer account : ${ACCOUNT}"
echo "   Producer db.table: ${DB}.${TABLE}"
echo "   Now run on the CONSUMER side:"
echo "     ./run_demo.sh --phase xacct-autowire --app-id <app> --role-arn <role> \\"
echo "        --bucket <consumer-bucket> --producer-account ${ACCOUNT} \\"
echo "        --producer-db ${DB} --producer-table ${TABLE}"
