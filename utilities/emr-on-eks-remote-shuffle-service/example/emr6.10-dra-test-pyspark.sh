#!/bin/bash
# SPDX-FileCopyrightText: Copyright 2021 Amazon.com, Inc. or its affiliates.
# SPDX-License-Identifier: MIT-0
export EMRCLUSTER_NAME=emr-on-eks-rss
export AWS_REGION=us-west-2
export ACCOUNTID=$(aws sts get-caller-identity --query Account --output text)
export VIRTUAL_CLUSTER_ID=$(aws emr-containers list-virtual-clusters --query "virtualClusters[?name == '$EMRCLUSTER_NAME' && state == 'RUNNING'].id" --output text)
export EMR_ROLE_ARN=arn:aws:iam::$ACCOUNTID:role/$EMRCLUSTER_NAME-execution-role
export S3BUCKET=${EMRCLUSTER_NAME}-${ACCOUNTID}-${AWS_REGION}
export ECR_URL="$ACCOUNTID.dkr.ecr.$AWS_REGION.amazonaws.com"

aws emr-containers start-job-run \
  --virtual-cluster-id $VIRTUAL_CLUSTER_ID \
  --name em65-multithreaded-func \
  --execution-role-arn $EMR_ROLE_ARN \
  --release-label emr-6.10.0-latest \
  --job-driver '{
  "sparkSubmitJobDriver": {
      "entryPoint": "s3://emr-mobileye-us-west-2/multithreaded_functions.py",
      "sparkSubmitParameters": "--py-files s3://emr-mobileye-us-west-2/benchmarks.zip --conf spark.driver.cores=1 --conf spark.driver.memory=2g --conf spark.executor.cores=4 --conf spark.executor.memory=5g"}}' \
  --configuration-overrides '{
    "applicationConfiguration": [
      {
        "classification": "spark-defaults", 
        "properties": {
          "spark.kubernetes.driver.podTemplateFile": "s3://'$S3BUCKET'/app_code/pod-template/driver-pod-template.yaml",
          "spark.kubernetes.executor.podTemplateFile": "s3://'$S3BUCKET'/app_code/pod-template/executor-pod-template.yaml",
          "spark.kubernetes.container.image": "'$ECR_URL'/mobieye:emr",

          "spark.dynamicAllocation.enabled":"true",
          "spark.dynamicAllocation.shuffleTracking.enabled":"true",
          "spark.dynamicAllocation.minExecutors":"8",
          "spark.dynamicAllocation.maxExecutors":"80",

          "spark.kubernetes.executor.podNamePrefix": "emr-eks-multithreaded-func",
          "spark.kubernetes.node.selector.eks.amazonaws.com/nodegroup": "c5d9a",
          "spark.kubernetes.driver.annotation.name":"emr-eks-mobileye-test-4"
      }}
    ],
    "monitoringConfiguration": {
      "s3MonitoringConfiguration": {"logUri": "s3://'$S3BUCKET'/elasticmapreduce/emr-containers"}}}'
