#!/bin/bash

export stack_name="${1:-HiveEMRonEKS}"

# 0. Setup AWS environment
echo "==================================="
echo "Make sure you have 'jq' installed"
echo "Setup AWS environment ..."
echo "==================================="

export secret_name=$(aws cloudformation describe-stacks --stack-name $stack_name --query "Stacks[0].Outputs[?OutputKey=='HiveSecretName'].OutputValue" --output text)
export HOST_NAME=$(aws secretsmanager get-secret-value --secret-id $secret_name --query SecretString --output text | jq -r '.host')
export PASSWORD=$(aws secretsmanager get-secret-value --secret-id $secret_name --query SecretString --output text | jq -r '.password')
export DB_NAME=$(aws secretsmanager get-secret-value --secret-id $secret_name --query SecretString --output text | jq -r '.dbname')
export USER_NAME=$(aws secretsmanager get-secret-value --secret-id $secret_name --query SecretString --output text | jq -r '.username')
export VIRTUAL_CLUSTER_ID=$(aws cloudformation describe-stacks --stack-name $stack_name --query "Stacks[0].Outputs[?OutputKey=='VirtualClusterId'].OutputValue" --output text)
export EMR_ROLE_ARN=$(aws cloudformation describe-stacks --stack-name $stack_name --query "Stacks[0].Outputs[?OutputKey=='EMRExecRoleARN'].OutputValue" --output text)
export S3BUCKET=$(aws cloudformation describe-stacks --stack-name $stack_name --query "Stacks[0].Outputs[?OutputKey=='CODEBUCKET'].OutputValue" --output text)

echo "export HOST_NAME=${HOST_NAME}" | tee -a ~/.bash_profile
echo "export PASSWORD=${PASSWORD}" | tee -a ~/.bash_profile
echo "export DB_NAME=${DB_NAME}" | tee -a ~/.bash_profile
echo "export USER_NAME=${USER_NAME}" | tee -a ~/.bash_profile
echo "export VIRTUAL_CLUSTER_ID=${VIRTUAL_CLUSTER_ID}" | tee -a ~/.bash_profile
echo "export EMR_ROLE_ARN=${EMR_ROLE_ARN}" | tee -a ~/.bash_profile
echo "export S3BUCKET=${S3BUCKET}" | tee -a ~/.bash_profile

# 1. connect to the EKS newly created
echo "==================================="
echo "Make sure you have 'kubectl' installed"
echo "Testing EKS connection..."
echo "==================================="
echo `aws cloudformation describe-stacks --stack-name $stack_name --query "Stacks[0].Outputs[?starts_with(OutputKey,'eksclusterEKSConfig')].OutputValue" --output text` | bash
kubectl get svc

