#!/bin/bash
# SPDX-FileCopyrightText: Copyright 2021 Amazon.com, Inc. or its affiliates.
# SPDX-License-Identifier: MIT-0
# export EMRCLUSTER_NAME=emr-on-eks-rss
# export AWS_REGION=us-east-1
          # "spark.celeborn.shuffle.partitionSplit.threshold": "2g",
          # "spark.celeborn.client.push.sort.pipeline.enabled": "true",
          # "spark.celeborn.client.push.sort.randomizePartitionId.enabled": "true",
          # "spark.celeborn.shuffle.batchHandleChangePartition.enabled": "true",
          # "spark.celeborn.shuffle.batchHandleCommitPartition.enabled": "true",
          # "spark.celeborn.shuffle.batchHandleReleasePartition.enabled": "true",
          # "spark.celeborn.client.push.blacklist.enabled": "true",
          # "spark.celeborn.client.blacklistSlave.enabled": "true",
          # "spark.celeborn.client.shuffle.writer": "SORT",
               
    
        
export ACCOUNTID=$(aws sts get-caller-identity --query Account --output text)
export VIRTUAL_CLUSTER_ID=$(aws emr-containers list-virtual-clusters --query "virtualClusters[?name == '$EMRCLUSTER_NAME' && state == 'RUNNING'].id" --output text)
export EMR_ROLE_ARN=arn:aws:iam::$ACCOUNTID:role/$EMRCLUSTER_NAME-execution-role
export S3BUCKET=${EMRCLUSTER_NAME}-${ACCOUNTID}-${AWS_REGION}
export ECR_URL="$ACCOUNTID.dkr.ecr.$AWS_REGION.amazonaws.com"

aws emr-containers start-job-run \
  --virtual-cluster-id $VIRTUAL_CLUSTER_ID \
  --name em66-clb-adj \
  --execution-role-arn $EMR_ROLE_ARN \
  --release-label emr-6.6.0-latest \
  --job-driver '{
  "sparkSubmitJobDriver": {
      "entryPoint": "local:///usr/lib/spark/examples/jars/eks-spark-benchmark-assembly-1.0.jar",
      "entryPointArguments":["s3://'$S3BUCKET'/BLOG_TPCDS-TEST-3T-partitioned","s3://'$S3BUCKET'/EMRONEKS_TPCDS-TEST-3T-RESULT","/opt/tpcds-kit/tools","parquet","3000","1","false","q15-v2.4","true"],
      "sparkSubmitParameters": "--class com.amazonaws.eks.tpcds.BenchmarkSQL --conf spark.driver.cores=2 --conf spark.driver.memory=3g --conf spark.executor.cores=4 --conf spark.executor.memory=6g --conf spark.executor.instances=47"}}' \
  --configuration-overrides '{
    "applicationConfiguration": [
      {
        "classification": "spark-defaults", 
        "properties": {
          "spark.kubernetes.container.image": "'$ECR_URL'/clb-spark-benchmark:emr6.6",
          "spark.executor.memoryOverhead": "2G",
          "spark.kubernetes.executor.podNamePrefix": "emr-eks-tpcds-clb",
          "spark.kubernetes.node.selector.eks.amazonaws.com/nodegroup": "c59b",
          "spark.network.timeout": "2000s",
          "spark.executor.heartbeatInterval": "300s",

          "spark.celeborn.client.fetch.timeout": "120s",
          "spark.celeborn.client.push.data.timeout": "120s",
          "spark.celeborn.push.limit.inFlight.timeout": "600s",

          "spark.serializer": "org.apache.spark.serializer.KryoSerializer",
          "spark.celeborn.push.replicate.enabled": "false",
          "spark.celeborn.push.maxReqsInFlight": "32",
          "spark.celeborn.shuffle.compression.codec": "zstd",
          "spark.celeborn.rpc.askTimeout": "120s",

          "spark.shuffle.service.enabled": "false",
          "spark.sql.adaptive.enabled": "true",
          "spark.sql.adaptive.skewJoin.enabled": "true",
          "spark.sql.adaptive.localShuffleReader.enabled": "false",
          "spark.shuffle.manager": "org.apache.spark.shuffle.celeborn.RssShuffleManager",
          "spark.celeborn.master.endpoints": "celeborn-master-0.celeborn-master-svc.celeborn:9097,celeborn-master-1.celeborn-master-svc.celeborn:9097,celeborn-master-2.celeborn-master-svc.celeborn:9097"
          
      }},
      {
        "classification": "spark-log4j",
        "properties": {
          "log4j.rootCategory":"WARN, console"
        }
      }
    ],
    "monitoringConfiguration": {
      "s3MonitoringConfiguration": {"logUri": "s3://'$S3BUCKET'/elasticmapreduce/emr-containers"}}}'
