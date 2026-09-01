#!/usr/bin/env bash
# Run in the PRODUCER account (111122223333). Grants the consumer read access.
# This example uses hybrid access mode (IAM_ALLOWED_PRINCIPALS); on Lake Formation
# cross-account version 3+ you can instead grant directly to the consumer role as a
# named principal.
set -euo pipefail

PRODUCER_CATALOG="111122223333"
DB="salesdb"
TBL="fulfillment"

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=IAM_ALLOWED_PRINCIPALS \
  --resource "{\"Table\":{\"CatalogId\":\"${PRODUCER_CATALOG}\",\"DatabaseName\":\"${DB}\",\"Name\":\"${TBL}\"}}" \
  --permissions SELECT DESCRIBE

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=IAM_ALLOWED_PRINCIPALS \
  --resource "{\"Database\":{\"CatalogId\":\"${PRODUCER_CATALOG}\",\"Name\":\"${DB}\"}}" \
  --permissions DESCRIBE

# Attach the Glue resource policy (must include database/default) and the S3 bucket policy:
aws glue put-resource-policy --policy-in-json file://glue-resource-policy.json --enable-hybrid TRUE
aws s3api put-bucket-policy --bucket producer-data-bucket --policy file://s3-bucket-policy.json
