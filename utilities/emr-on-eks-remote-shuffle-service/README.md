# Standalone remote shuffle service with EMR on EKS

Remote Shuffle Service provides the capability for Apache Spark applications to store shuffle data 
on remote servers. See more details on Spark community document: 
[[SPARK-25299][DISCUSSION] Improving Spark Shuffle Reliability](https://docs.google.com/document/d/1uCkzGGVG17oGC6BJ75TpzLAZNorvrAU3FRd2X-rVHSM/edit?ts=5e3c57b8).

The high level design for Uber's Remote Shuffle Service (RSS) can be found [here](https://github.com/uber/RemoteShuffleService/blob/master/docs/server-high-level-design.md), ByteDance's Cloud Shuffle Service (CSS) can be found [here](https://github.com/bytedance/CloudShuffleService), Tecent's Apache Uniffle can be found [here](https://uniffle.apache.org/docs/intro).AliCloud's Apache Celeborn can be found [here](https://github.com/apache/incubator-celeborn). OPPO's Shuttle can be found [here](https://github.com/cubefs/shuttle/blob/master/docs/server-high-level-design.md)

# Setup instructions:
* [1. Install Uber's RSS](#1-install-rss-server-on-eks) 
* [2. Install ByteDance's CSS](#1-install-css-server-on-eks) 
* [3. Install Apache Uniffle (Tencent)](#1-install-uniffle-operator-on-eks)
* [4. Install Apache Celeborn (AliCloud)](#1-install-celeborn-server-on-eks)

## Infrastructure
If you do not have your own environment to run Spark, run the command. Change the region if needed.
```
export EKSCLUSTER_NAME=eks-rss
export AWS_REGION=us-east-1
./eks_provision.sh
```
which provides a one-click experience to create an EMR on EKS environment and OSS Spark Operator on a common EKS cluster. The EKS cluster contains the following managed nodegroups which are located in a single AZ within the same [Cluster placment strategy](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-groups.html) to achieve the low-latency network performance for the intercommunication between apps and shuffle servers:
- 1 - [`rss-i3en`](https://github.com/melodyyangaws/emr-on-eks-remote-shuffle-service/blob/99e7b2efbbd25a72435cc00a8bed6e14e91f415b/eks_provision.sh#L104) that scales i3en.6xlarge instances from 1 to 20. They are labelled as `app=rss` to host the RSS servers. Only 1 out of 2 SSD disks is mounted to these instances for the RSS's rootdir limitation.
- 2 - [`css-i3en`](https://github.com/melodyyangaws/emr-on-eks-remote-shuffle-service/blob/99e7b2efbbd25a72435cc00a8bed6e14e91f415b/eks_provision.sh#L128) that scales i3en.6xlarge instances from 1 to 20. They are labelled as `app=css` to host the CSS and Uniffle clusters.
- 2 - [`c59a` & `c59b`](https://github.com/melodyyangaws/emr-on-eks-remote-shuffle-service/blob/e81ed02da9a470889dd806a7be6ed9f160510563/eks_provision.sh#L111) that can scale c5.9xlarge instances from 1 to 50 at AZ-a and AZ-b respectively. They are labelled as `app=sparktest` to run multiple EMR on EKS jobs or OSS Spark tests in parallel. Additionally, the node groups can be used to run TPCDS source data generation job if needed.

## Quick Start: Run rmeote shuffle server in EMR
```bash
git clone https://github.com/aws-samples/aws-emr-utilities.git
cd aws-emr-utilities/utilities/emr-on-eks-remote-shuffle-service
```
## **UBER's RSS option**
### 1. Install RSS server on EKS
```bash
helm install rss ./charts/remote-shuffle-service -n remote-shuffle-service --create-namespace
# check progress
kubectl get all -n remote-shuffle-service
```
```
# OPTIONAL: scale up or scale down the Shuffle server
kubectl scale statefulsets rss -n remote-shuffle-service  --replicas=0
kubectl scale statefulsets rss -n remote-shuffle-service  --replicas=3
```
```bash
# uninstall
helm uninstall rss -n remote-shuffle-service
kubectl delete namespace remote-shuffle-service
```
Before the installation, take a look at the [charts/remote-shuffle-service/values.yaml](./charts/remote-shuffle-service/values.yaml). There are few configurations need to pay attention to: 

#### Node selector
```yaml
nodeSelector:
    app: rss
```    
It means the RSS Server will only be installed on EC2 instances that have the label `app=rss`. By doing this, we can assign RSS service to a specific instance type with SSD disk mounted, [`i3en.6xlarge`](https://github.com/melodyyangaws/emr-on-eks-remote-shuffle-service/blob/de77e588a2c89080e448f75321f4174a51c77799/eks_provision.sh#L98) in this case. Change the label name based on your EKS setup or simply remove these two lines to run RSS on any instances.

#### Access control to RSS data storage 
At RSS client (Spark applications), we use `Hadoop` to run jobs. They are also the user to write to the shuffle service disks on the server. For EMR on EKS, you should run the RSS server under 999:1000 permission.
```bash
# configure the shuffle service volume owner as Hadoop user (EMR on EKS is 999:1000, OSS Spark is 1000:1000)
volumeUser: 999
volumeGroup: 1000

```
#### Mount a high performant disk
Currently, RSS only supports a single disk mount as the shuffle storage. 
Without specify the `rootdir`, by default, RSS server uses a local EBS root volume to store the shuffle data. However, it is normally too small to handle a large volume of shuffling data. It is recommended to mount a larger size and high performant disk, such as a local nvme SSD disk or [FSx for Lustre storage](https://aws.amazon.com/blogs/big-data/run-apache-spark-with-amazon-emr-on-eks-backed-by-amazon-fsx-for-lustre-storage/).
```bash
volumeMounts:
  - name: spark-local-dir-1
    mountPath: /rss1
volumes:
  - name: spark-local-dir-1
    hostPath:
      path: /local1
command:
  jvmHeapSizeOption: "-Xmx800M"
  # point to a nmve SSD volume as the shuffle data storage, not use root volume.
  # the RSS only supports a single disk so far. Use storage optmized EC2 instance type.
  rootdir: "/rss1"
```

### 2. Build a custom image for RSS client
Build a custom docker image to include the [Spark benchmark utility](https://github.com/aws-samples/emr-on-eks-benchmark#spark-on-kubernetes-benchmark-utility) tool and a Remote Shuffle Service client

Login to ECR in your account and create a repository called `rss-spark-benchmark`:
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name rss-spark-benchmark --image-scanning-configuration scanOnPush=true
```

Build EMR om EKS image:
```bash
# The custom image includes Spark Benchmark Untility and RSS client. We use EMR 6.6 (Spark 3.2.0) as the base image
export SRC_ECR_URL=755674844232.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $SRC_ECR_URL
docker pull $SRC_ECR_URL/spark/emr-6.6.0:latest

docker build -t $ECR_URL/rss-spark-benchmark:emr6.6 -f docker/rss-emr-client/Dockerfile --build-arg SPARK_BASE_IMAGE=$SRC_ECR_URL/spark/emr-6.6.0:latest .
docker push $ECR_URL/rss-spark-benchmark:emr6.6
```

Build an OSS Spark docker image (OPTIONAL):
```bash
docker build -t $ECR_URL/rss-spark-benchmark:3.2.0 -f docker/rss-oss-client/Dockerfile
docker push $ECR_URL/rss-spark-benchmark:3.2.0
```

## **ByteDance's CSS option**

### 1. Install CSS server on EKS
#### Install Zookeeper via Helm Chart
```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm install zookeeper bitnami/zookeeper -n zk -f charts/zookeeper/values.yaml --create-namespace
```
```
# uninstall zookeeper
helm uninstall zookeeper -n zk
kubectl delete pvc --all -n zk
```
#### Helm install CSS server
Before the installation, take a look at the configuration [charts/cloud-shuffle-service/values.yaml](./charts/cloud-shuffle-service/values.yaml) and modify it based on your EKS setup.

```bash
helm install css ./charts/cloud-shuffle-service -n css --create-namespace
# check progress
kubectl get all -n css
```
```
# OPTIONAL: scale up or scale down the Shuffle server
kubectl scale statefulsets css -n css  --replicas=0
kubectl scale statefulsets css -n css  --replicas=3
```

```bash
# uninstall
helm uninstall css
kubectl delete namespace css
```

### 2. Build a custom image for CSS client
Login to ECR and create a repository called `css-spark-benchmark`:
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name css-spark-benchmark --image-scanning-configuration scanOnPush=true
```

Build EMR on EKS image
```bash
# The custom image includes Spark Benchmark Untility and CSS client. We use EMR 6.6 (Spark 3.2.0) as the base image
export SRC_ECR_URL=755674844232.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $SRC_ECR_URL
docker pull $SRC_ECR_URL/spark/emr-6.6.0:latest

docker build -t $ECR_URL/css-spark-benchmark:emr6.6 -f docker/css-emr-client/Dockerfile --build-arg SPARK_BASE_IMAGE=$SRC_ECR_URL/spark/emr-6.6.0:latest .
docker push $ECR_URL/css-spark-benchmark:emr6.6
```
## **Apache Uniffle RSS option**

### 1. Install Uniffle Operator on EKS
Ensure you have [`wget`](https://formulae.brew.sh/formula/wget) and [`go 1.16`](https://formulae.brew.sh/formula/go@1.16) installed.

#### Build Coordinator and Server docker image
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL

cd docker/uniffle-server
export UNIFFLE_VERSION="0.7.0-snapshot"
sh build.sh --hadoop-version 3.2.1 --registry $ECR_URL
```
#### Make Operator's Webhook & Controller docker images:
```bash
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name rss-webhook --image-scanning-configuration scanOnPush=true
aws ecr create-repository --repository-name rss-controller --image-scanning-configuration scanOnPush=true

cd ../../charts/uniffle-operator
export VERSION="0.7.0-snapshot"
make REGISTRY=$ECR_URL docker-build docker-push -f Makefile
```
#### Run Uniffle Operator in EKS 
**TODO: build helm chart for a single-command deployment**
Before start, update the `example/configmap.yaml` file to config Uniffle operator.Replace docker image URLs by your images in the definition files:`example/uniffle-webhook.yaml`, `example/uniffle-webhook.yaml`, `example/uniffle-operator.yaml`.

Note: server's key configs are `xmxSize=0.75 X server pod memory`, `rss.server.buffer.capacity=0.6 X xmxSize` and  `rss.server.read.buffer.capacity=0.2 X xmxSize`

```bash
# Create a new namespace for Apache Uniffle
kubectl create namespace uniffle

# Create uniffle CRD
kubectl apply -f example/uniffle-crd.yaml 
# Update docker image name and tag, then create webhook
kubectl apply -f example/uniffle-webhook.yaml
# Update docker image name and tag, then create controller
kubectl apply -f example/uniffle-controller.yaml
# Config coordinator and server
kubectl apply -f example/configmap.yaml 
# Update docker image name and tag, then start server and coordinators
kubectl apply -f example/uniffle-operator.yaml
# validate
kubectl get all -n uniffle
```
### 2. Build a custom image for Uniffle client
Login to ECR and create a repository called `uniffle-spark-benchmark`:
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name uniffle-spark-benchmark --image-scanning-configuration scanOnPush=true
```

Build EMR on EKS image
```bash
# The custom image includes Spark Benchmark Untility and uniffle client. We use EMR 6.6 (Spark 3.2.0) as the base image
export SRC_ECR_URL=755674844232.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $SRC_ECR_URL
docker pull $SRC_ECR_URL/spark/emr-6.6.0:latest

docker build -t $ECR_URL/uniffle-spark-benchmark:emr6.6 -f docker/uniffle-emr-client/Dockerfile --build-arg SPARK_BASE_IMAGE=$SRC_ECR_URL/spark/emr-6.6.0:latest .
docker push $ECR_URL/uniffle-spark-benchmark:emr6.6
```

## **Apache Celeborn RSS option**

### 1. Install Celeborn server on EKS

#### Create docker container repository
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
# create a new ECR as an one-off task
aws ecr create-repository --repository-name celeborn-server \
  --image-scanning-configuration scanOnPush=true
aws ecr create-repository --repository-name clb-spark-benchmark \
  --image-scanning-configuration scanOnPush=true
```  
#### Build & push server & client docker images
```
SPARK_VERSION=3.2
# build server
docker build -t $ECR_URL/celeborn-server:spark${SPARK_VERSION} \
  --build-arg SPARK_VERSION=${SPARK_VERSION} \
  -f docker/celeborn-server/Dockerfile .
# push the image to ECR
docker push $ECR_URL/celeborn-server:spark${SPARK_VERSION}

# build client with benchmark tool
docker build -t $ECR_URL/clb-spark-benchmark:emr6.6 \
  --build-arg SPARK_VERSION=${SPARK_VERSION} \
  --build-arg SPARK_BASE_IMAGE=$SRC_ECR_URL/spark/emr-6.6.0:latest \
  -f docker/celeborn-emr-client/Dockerfile .
docker push $ECR_URL/clb-spark-benchmark:emr6.6
```
#### Run Celeborn shuffle service in EKS
```bash
# config celeborn environment variables and docker image
vi charts/celeborn-shuffle-service/values.yaml
# install
helm install celeborn ./charts/celeborn-shuffle-service  -n celeborn --create-namespace
# check progress
kubectl get all -n celeborn
```

## Run Benchmark

### OPTIONAL: generate the TCP-DS source data
The job will generate TPCDS source data at 3TB scale to your S3 bucket `s3://'$S3BUCKET'/BLOG_TPCDS-TEST-3T-partitioned/`. Alternatively, directly copy the source data from `s3://blogpost-sparkoneks-us-east-1/blog/BLOG_TPCDS-TEST-3T-partitioned` to your S3.
```bash
kubectl apply -f examples/tpcds-data-gen.yaml
```

### Run EMR on EKS Spark benchmark test:
All of benchmark jobs will run in the single namespace `emr`.
Update the docker image name to your ECR URL in the following file, then run:
```bash
# go to the project root directory
cd emr-on-eks-remote-shuffle-service
export EMRCLUSTER_NAME=emr-on-eks-rss
export AWS_REGION=<YOUR_REGION>
# run the performance test with Uber's RSS
./example/emr6.6-benchmark-rss.sh
# Or Bytedance's CSS
./example/emr6.6-benchmark-css.sh
# Or Tecent's Apache Uniffle
./example/emr6.6-benchmark-uniffle.sh
# Or Aliyun's Apache Celeborn
./example/emr6.6-benchmark-celeborn.sh
# check job progress
kubectl get po -n emr
kubectl logs <DRIVER_POD_NAME> -n emr spark-kubernetes-driver
```

**NOTE**: in Uber's RSS benchmark test, keep the server string like `rss-%s` for the config `spark.shuffle.rss.serverSequence.connectionString`, This is intended because `RssShuffleManager` can use it to format the connection string dynamically. In the following example, our Spark job will connect to 3 RSS servers:
```bash
"spark.shuffle.manager": "org.apache.spark.shuffle.RssShuffleManager",
"spark.shuffle.rss.serviceRegistry.type": "serverSequence",
"spark.shuffle.rss.serverSequence.connectionString": "rss-%s.rss.remote-shuffle-service.svc.cluster.local:9338",
"spark.shuffle.rss.serverSequence.startIndex": "0",
"spark.shuffle.rss.serverSequence.endIndex": "2",
```
The setting`"spark.shuffle.rss.serviceRegistry.type": "serverSequence"` means the metadata will be stored in a cluster of standalone RSS servers.


### OPTIONAL: Run OSS Spark benchmark
NOTE: some queries may not be able to complete, due to the limited resources alloated to run such a large scale test. Update the docker image to your image repository URL, then test the performance for different remote shuffle service options, For example:
```bash
kubectl apply -f oss-benchmark-uniffle.yaml
# or 
kubectl apply -f oss-benchmark-rss.yaml
```
```bash
# check job progress
kubectl get pod -n oss
# check application logs
kubectl logs uniffle-benchmark-driver -n oss
```
