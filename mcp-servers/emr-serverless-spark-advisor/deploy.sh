#!/bin/bash
set -e

REGION=${AWS_REGION:-us-east-1}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO_NAME="spark-config-advisor-mcp"
FUNCTION_NAME=${LAMBDA_FUNCTION_NAME:-mcp-lambda-for-emr-testing}
IMAGE_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO_NAME:latest"

echo "=== Building and deploying to $FUNCTION_NAME ==="

# Create ECR repo if needed
aws ecr describe-repositories --repository-names $REPO_NAME --region $REGION 2>/dev/null || \
  aws ecr create-repository --repository-name $REPO_NAME --region $REGION

# Login, build, push
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
docker build --platform linux/amd64 -t $REPO_NAME .
docker tag $REPO_NAME:latest $IMAGE_URI
docker push $IMAGE_URI

# Update Lambda
aws lambda update-function-code \
  --function-name $FUNCTION_NAME \
  --image-uri $IMAGE_URI \
  --region $REGION

echo "=== Deployed $IMAGE_URI to $FUNCTION_NAME ==="
echo "Configure your MCP client with the Lambda Function URL"
