# Deploying HDFS on AWS EKS

## build custom image
The default hdfs docker image is from the public docker hub, which is built under the Hadoop version 3.2.1. If you need to build a custom one, run the command:

```
# Login to ECR
AWS_REGION=YOUR_REGION
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name hdfs --image-scanning-configuration scanOnPush=true

# EXAMPLE#1: build with the open source Hadoop version 3.3.4
docker build -t $ECR_URL/hdfs:3.3.4 -f docker/Dockerfile_oss.yaml --build-arg HADOOP_VERSION=3.3.4 .
docker push $ECR_URL/hdfs:3.3.4

# EXAMPLE#2: build based on the EMR on EKS image under Hadoop version 3.3.3
docker build -t $ECR_URL/hdfs:emr3.3.3 -f docker/Dockerfile_oss.yaml --build-arg BASE_IMAGE_NAME=895885662937.dkr.ecr.us-west-2.amazonaws.com/spark/emr-6.9.0 .
docker push $ECR_URL/hdfs:emr3.3.3
```
Then configure the Helm Chart to use them:

```bash

  REPO_PREFIX="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/gchq"

  EXTRA_HELM_ARGS=""
  EXTRA_HELM_ARGS+="--set namenode.repository=${ECR_URL}/hdfs"
  EXTRA_HELM_ARGS+="--set datanode.repository=${ECR_URL}/hdfs"
  EXTRA_HELM_ARGS+="--set shell.repository=${ECR_URL}/hdfs"
```

## Deploy Helm chart
```bash
export HADOOP_VERSION=${HADOOP_VERSION:-3.2.1}

helm install hdfs . -f ./values-eks-alb.yaml \
  ${EXTRA_HELM_ARGS} \
  --set hdfs.namenode.tag=${HADOOP_VERSION} \
  --set hdfs.datanode.tag=${HADOOP_VERSION} \
  --set hdfs.shell.tag=${HADOOP_VERSION}

helm test hdfs
```
