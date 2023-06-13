#!/bin/bash
# SPDX-FileCopyrightText: Copyright 2021 Amazon.com, Inc. or its affiliates.
# SPDX-License-Identifier: MIT-0

# "spark.celeborn.client.spark.shuffle.writer": "SORT",
# "spark.celeborn.client.push.sort.randomizePartitionId.enabled": "true",
# "spark.celeborn.client.push.buffer.max.size": "128k",
# "spark.celeborn.push.unsafeRow.fastWrite.enabled": "false",
# "spark.celeborn.push.data.timeout": "30s",
# "spark.celeborn.client.push.limit.inFlight.timeout": "600s",
# "spark.celeborn.client.fetch.maxRetriesForEachReplica": "5",
# "spark.celeborn.data.io.retryWait": "10s",
# "spark.celeborn.shuffle.chunk.size": "4m",
# "spark.celeborn.push.maxReqsInFlight": "128",

# export EMRCLUSTER_NAME=emr-on-eks-rss
# export AWS_REGION=us-east-1
export ACCOUNTID=$(aws sts get-caller-identity --query Account --output text)
export VIRTUAL_CLUSTER_ID=$(aws emr-containers list-virtual-clusters --query "virtualClusters[?name == '$EMRCLUSTER_NAME' && state == 'RUNNING'].id" --output text)
export EMR_ROLE_ARN=arn:aws:iam::$ACCOUNTID:role/$EMRCLUSTER_NAME-execution-role
export S3BUCKET=${EMRCLUSTER_NAME}-${ACCOUNTID}-${AWS_REGION}
export ECR_URL="$ACCOUNTID.dkr.ecr.$AWS_REGION.amazonaws.com"

aws emr-containers start-job-run \
  --virtual-cluster-id $VIRTUAL_CLUSTER_ID \
  --name em610-test-clb-baseline  \
  --execution-role-arn $EMR_ROLE_ARN \
  --release-label emr-6.10.0-latest \
  --job-driver '{
  "sparkSubmitJobDriver": {
      "entryPoint": "local:///usr/lib/spark/examples/jars/eks-spark-benchmark-assembly-1.0.jar",
      "entryPointArguments":["s3://'$S3BUCKET'/BLOG_TPCDS-TEST-3T-partitioned","s3://'$S3BUCKET'/EMRONEKS_TPCDS-TEST-3T-RESULT","/opt/tpcds-kit/tools","parquet","3000","1","false","q23a-v2.4,q23b-v2.4,q24a-v2.4,q24b-v2.4,q4-v2.4,q5-v2.4,q50-v2.4,q51-v2.4,q67-v2.4,,q78-v2.4,q8-v2.4,q87-v2.4,q93-v2.4,q94-v2.4","true"],
      "sparkSubmitParameters": "--class com.amazonaws.eks.tpcds.BenchmarkSQL --conf spark.driver.cores=3 --conf spark.driver.memory=4g --conf spark.executor.cores=4 --conf spark.executor.memory=7g --conf spark.executor.instances=47"}}' \
  --retry-policy-configuration '{"maxAttempts": 5}' \
  --configuration-overrides '{
    "applicationConfiguration": [
      {
        "classification": "spark-defaults", 
        "properties": {
          "spark.kubernetes.container.image": "'$ECR_URL'/clb-spark-benchmark:emr6.10_clb",
          "spark.network.timeout": "2000s",
          "spark.executor.heartbeatInterval": "300s",
          "spark.driver.memoryOverhead": "1000",
          "spark.executor.memoryOverhead": "2G",
          "spark.kubernetes.executor.podNamePrefix": "emr-clb-3iter",

          "spark.shuffle.service.enabled": "false",
          "spark.sql.adaptive.enabled": "true",
          "spark.sql.adaptive.skewJoin.enabled": "true",
          "spark.sql.adaptive.localShuffleReader.enabled":"false",
          "spark.serializer": "org.apache.spark.serializer.KryoSerializer",

          "spark.celeborn.push.replicate.enabled": "true",
          "spark.celeborn.shuffle.batchHandleChangePartition.enabled": "true",
          "spark.celeborn.shuffle.batchHandleCommitPartition.enabled": "true",
          "spark.celeborn.client.push.blacklist.enabled": "true",
          "spark.celeborn.client.fetch.excludeWorkerOnFailure.enabled": "true",
          "spark.celeborn.client.commitFiles.ignoreExcludedWorker": "true"

          "spark.shuffle.manager": "org.apache.spark.shuffle.celeborn.RssShuffleManager",
          "spark.celeborn.master.endpoints": "celeborn-master-0.celeborn-master-svc.celeborn:9097,celeborn-master-1.celeborn-master-svc.celeborn:9097,celeborn-master-2.celeborn-master-svc.celeborn:9097",
          "spark.sql.optimizedUnsafeRowSerializers.enabled":"false",

          "spark.kubernetes.node.selector.eks.amazonaws.com/nodegroup": "c59b"
      }},
      {
        "classification": "spark-log4j",
        "properties": {
          "rootLogger.level" : "WARN"
        }
      }
    ],
    "monitoringConfiguration": {
      "s3MonitoringConfiguration": {"logUri": "s3://'$S3BUCKET'/elasticmapreduce/emr-containers"}}}'
