#!/bin/bash
# Deploy EMR Serverless Spark Advisor MCP Server via CloudFormation.
#
# Usage:
#   ./deploy-cfn.sh                          # defaults: us-east-1, stack name spark-advisor
#   ./deploy-cfn.sh --region us-west-2 --stack-name my-advisor
#
# Prerequisites: AWS CLI, pip3, zip
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="spark-advisor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2;;
    --stack-name) STACK_NAME="$2"; shift 2;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ARTIFACT_BUCKET="spark-advisor-artifacts-${ACCOUNT_ID}-${REGION}"
ARTIFACT_PREFIX="spark-advisor-artifacts"

echo "=== EMR Serverless Spark Advisor — CloudFormation Deploy ==="
echo "Region:    $REGION"
echo "Stack:     $STACK_NAME"
echo "Artifacts: s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/"
echo ""

# 1. Create artifact bucket if needed
if ! aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Creating artifact bucket..."
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
fi

# 2. Build Lambda layer (platform-targeted for Lambda x86_64 Linux)
echo "Building Lambda layer..."
LAYER_DIR=$(mktemp -d)
pip3 install --target "$LAYER_DIR/python" -q \
  --platform manylinux_2_17_x86_64 --only-binary=:all: --python-version 3.12 \
  mcp mangum pydantic pandas
find "$LAYER_DIR/python" -name "*darwin*" -delete 2>/dev/null || true
(cd "$LAYER_DIR" && zip -r -q /tmp/mcp-layer.zip python/)
rm -rf "$LAYER_DIR"

# 3. Build Lambda function zip
echo "Building Lambda function..."
(cd "$SCRIPT_DIR" && zip -j -q /tmp/mcp-function.zip lambda_handler.py spark_advisor_mcp.py emr_recommender.py)

# 4. Build zstandard archive for Python 3.9 (EMR 7.x)
echo "Building zstandard archive..."
ZSTD_DIR=$(mktemp -d)
pip3 install --target "$ZSTD_DIR" -q \
  --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.9 \
  zstandard
(cd "$ZSTD_DIR" && zip -r -q /tmp/zstandard.zip .)
rm -rf "$ZSTD_DIR"

# 5. Upload artifacts
echo "Uploading artifacts to S3..."
aws s3 cp /tmp/mcp-layer.zip "s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/mcp-layer.zip" --region "$REGION"
aws s3 cp /tmp/mcp-function.zip "s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/mcp-function.zip" --region "$REGION"
aws s3 cp /tmp/zstandard.zip "s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/zstandard.zip" --region "$REGION"
aws s3 cp "$SCRIPT_DIR/legacy/spark_extractor.py" "s3://${ARTIFACT_BUCKET}/${ARTIFACT_PREFIX}/spark_extractor.py" --region "$REGION"

# 6. Deploy CloudFormation stack
echo "Deploying CloudFormation stack..."
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/cloudformation.yaml" \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
    ArtifactBucket="$ARTIFACT_BUCKET" \
    ArtifactPrefix="$ARTIFACT_PREFIX" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION"

# 7. Print outputs
echo ""
echo "=== Deployment Complete ==="
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

# Cleanup
rm -f /tmp/mcp-layer.zip /tmp/mcp-function.zip /tmp/zstandard.zip
