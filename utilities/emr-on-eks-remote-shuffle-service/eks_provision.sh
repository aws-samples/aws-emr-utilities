#!/bin/bash

# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0

# Define params
# export EKSCLUSTER_NAME=eks-rss
# export AWS_REGION=us-east-1
export EMR_NAMESPACE=emr
export OSS_NAMESPACE=oss
export EKS_VERSION=1.22
export EMRCLUSTER_NAME=emr-on-$EKSCLUSTER_NAME
export ROLE_NAME=${EMRCLUSTER_NAME}-execution-role
export ACCOUNTID=$(aws sts get-caller-identity --query Account --output text)
export S3TEST_BUCKET=${EMRCLUSTER_NAME}-${ACCOUNTID}-${AWS_REGION}

echo "==============================================="
echo "  setup IAM roles ......"
echo "==============================================="

# create S3 bucket for application
if [ $AWS_REGION == "us-east-1" ]; then
  aws s3api create-bucket --bucket $S3TEST_BUCKET --region $AWS_REGION
else
  aws s3api create-bucket --bucket $S3TEST_BUCKET --region $AWS_REGION --create-bucket-configuration LocationConstraint=$AWS_REGION
fi
# update artifact
aws s3 sync example/ s3://$S3TEST_BUCKET/app_code/

# Create a job execution role (https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/creating-job-execution-role.html)
cat >/tmp/job-execution-policy.json <<EOL
{
    "Version": "2012-10-17",
    "Statement": [ 
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject","s3:DeleteObject","s3:GetObject","s3:ListBucket"],
            "Resource": [
              "arn:aws:s3:::${S3TEST_BUCKET}",
              "arn:aws:s3:::${S3TEST_BUCKET}/*"
            ]
        }, 
        {
            "Effect": "Allow",
            "Action": [ "logs:PutLogEvents", "logs:CreateLogStream", "logs:DescribeLogGroups", "logs:DescribeLogStreams", "logs:CreateLogGroup" ],
            "Resource": [ "arn:aws:logs:*:*:*" ]
        }
    ]
}
EOL

cat >/tmp/trust-policy.json <<EOL
{
  "Version": "2012-10-17",
  "Statement": [ {
      "Effect": "Allow",
      "Principal": { "Service": "eks.amazonaws.com" },
      "Action": "sts:AssumeRole"
    } ]
}
EOL

aws iam create-policy --policy-name $ROLE_NAME-policy --policy-document file:///tmp/job-execution-policy.json
aws iam create-role --role-name $ROLE_NAME --assume-role-policy-document file:///tmp/trust-policy.json
aws iam attach-role-policy --role-name $ROLE_NAME --policy-arn arn:aws:iam::$ACCOUNTID:policy/$ROLE_NAME-policy

echo "===================================================="
echo "  Create a Cluster placement group for EKS ......"
echo "===================================================="
aws ec2 create-placement-group --group-name $EKSCLUSTER_NAME-agroup --strategy cluster \
  --tag-specifications 'ResourceType=placement-group,Tags={Key=app,Value=sparktest}'
aws ec2 create-placement-group --group-name $EKSCLUSTER_NAME-bgroup --strategy cluster \
  --tag-specifications 'ResourceType=placement-group,Tags={Key=app,Value=sparktest}'
echo "==============================================="
echo "  Create EKS Cluster ......"
echo "==============================================="

cat <<EOF >/tmp/ekscluster.yaml
---
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: $EKSCLUSTER_NAME
  region: $AWS_REGION
  version: "$EKS_VERSION"
vpc:
  clusterEndpoints:
      publicAccess: true
      privateAccess: true  
availabilityZones: ["${AWS_REGION}a","${AWS_REGION}b"]   
iam:
  withOIDC: true
  serviceAccounts:
  - metadata:
      name: cluster-autoscaler
      namespace: kube-system
      labels: {aws-usage: "cluster-ops"}
    wellKnownPolicies:
      autoScaler: true
    roleName: eksctl-cluster-autoscaler-role
  - metadata:
      name: oss
      namespace: $OSS_NAMESPACE
      labels: {aws-usage: "application"}
    attachPolicyARNs:
    - arn:aws:iam::$ACCOUNTID:policy/$ROLE_NAME-policy
  - metadata:
      name: amp-iamproxy-ingest-service-account
      namespace: prometheus
      labels: {aws-usage: "monitoring"}
    attachPolicyARNs: 
    - "arn:aws:iam::aws:policy/AmazonPrometheusRemoteWriteAccess"
    roleName: $EKSCLUSTER_NAME-prometheus-ingest 
    roleOnly: true    
managedNodeGroups: 
  - name: rss
    availabilityZones: ["${AWS_REGION}a"] 
    preBootstrapCommands:
      # - "sudo yum -y install mdadm"
      # - "IDX=0;for DEV in /dev/nvme[1-9]n1; do IDX=\$((\${IDX} + 1)); done; sudo mdadm --create --verbose /dev/md0 --level=0 --name=MY_RAID --chunk=64 --raid-devices=\${IDX} /dev/nvme[1-9]n1"
      # - "sudo mkfs.xfs -L MY_RAID /dev/md0;sudo mkdir -p /local1; sudo echo /dev/md0 /local1 xfs defaults,noatime 1 2 >> /etc/fstab;sudo mount -a"
      # - "sudo chown ec2-user:ec2-user /local1"
      - "IDX=1;for DEV in /dev/nvme[1-9]n1;do sudo mkfs.xfs \${DEV}; sudo mkdir -p /local\${IDX}; sudo echo \${DEV} /local\${IDX} xfs defaults,noatime 1 2 >> /etc/fstab; IDX=\$((\${IDX} + 1)); done"
      - "sudo mount -a"
      - "sudo chown ec2-user:ec2-user /local*"
    instanceType: i3en.6xlarge
    volumeSize: 20
    volumeType: gp3
    minSize: 1
    desiredCapacity: 1
    maxSize: 20
    placement:
      groupName: $EKSCLUSTER_NAME-agroup
    labels:
      app: rss
    tags:
      # required for cluster-autoscaler auto-discovery
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/$EKSCLUSTER_NAME: "owned"  
  - name: css
    availabilityZones: ["${AWS_REGION}b"] 
    preBootstrapCommands:
      - "IDX=1;for DEV in /dev/nvme[1-9]n1;do sudo mkfs.xfs \${DEV}; sudo mkdir -p /local\${IDX}; sudo echo \${DEV} /local\${IDX} xfs defaults,noatime 1 2 >> /etc/fstab; IDX=\$((\${IDX} + 1)); done"
      - "sudo mount -a"
      - "sudo chown ec2-user:ec2-user /local*"
    instanceType: i3en.6xlarge
    volumeSize: 20
    volumeType: gp3
    minSize: 1
    desiredCapacity: 1
    maxSize: 20
    placement:
      groupName: $EKSCLUSTER_NAME-bgroup
    labels:
      app: css
    tags:
      # required for cluster-autoscaler auto-discovery
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/$EKSCLUSTER_NAME: "owned"
  - name: c59a
    availabilityZones: ["${AWS_REGION}a"] 
    instanceType: c5.9xlarge
    preBootstrapCommands:
      - "sudo systemctl restart docker --no-block"
    volumeSize: 50
    volumeType: gp3
    minSize: 1
    desiredCapacity: 1
    maxSize: 50
    placement:
      groupName: $EKSCLUSTER_NAME-agroup
    labels:
      app: sparktest
    tags:
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/$EKSCLUSTER_NAME: "owned" 
  - name: c59b
    availabilityZones: ["${AWS_REGION}b"] 
    instanceType: c5.9xlarge
    preBootstrapCommands:
      - "sudo systemctl restart docker --no-block"
    volumeSize: 50
    volumeType: gp3
    minSize: 1
    desiredCapacity: 1
    maxSize: 50
    placement:
      groupName: $EKSCLUSTER_NAME-bgroup
    labels:
      app: sparktest
    tags:
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/$EKSCLUSTER_NAME: "owned"
  - name: c5d9a
    availabilityZones: ["${AWS_REGION}a"] 
    instanceType: c5d.9xlarge
    preBootstrapCommands:
      - "sudo systemctl restart docker --no-block"
    volumeSize: 20
    volumeType: gp3
    minSize: 1
    desiredCapacity: 1
    maxSize: 6
    placement:
      groupName: $EKSCLUSTER_NAME-agroup
    labels:
      app: sparktest
    tags:
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/$EKSCLUSTER_NAME: "owned"     
# enable all of the control plane logs
cloudWatch:
 clusterLogging:
   enableTypes: ["*"]
EOF

# create eks cluster in two AZs
eksctl create cluster -f /tmp/ekscluster.yaml
# if EKS cluster exists, comment out the line above, uncomment this line
# eksctl create nodegroup -f /tmp/ekscluster.yaml
aws eks update-kubeconfig --name $EKSCLUSTER_NAME --region $AWS_REGION

echo "==============================================="
echo "  Enable EMR on EKS ......"
echo "==============================================="

# Create kubernetes namespace for EMR on EKS
kubectl create namespace $EMR_NAMESPACE

# Enable cluster access for Amazon EMR on EKS in the 'emr' namespace
eksctl create iamidentitymapping --cluster $EKSCLUSTER_NAME --namespace $EMR_NAMESPACE --service-name "emr-containers"
aws emr-containers update-role-trust-policy --cluster-name $EKSCLUSTER_NAME --namespace $EMR_NAMESPACE --role-name $ROLE_NAME

# Create emr virtual cluster
aws emr-containers create-virtual-cluster --name $EMRCLUSTER_NAME \
  --container-provider '{
        "id": "'$EKSCLUSTER_NAME'",
        "type": "EKS",
        "info": { "eksInfo": { "namespace": "'$EMR_NAMESPACE'" } }
    }'

echo "==============================================="
echo "  Configure EKS Cluster ......"
echo "==============================================="
# Map the s3 bucket environment variable to EKS cluster
kubectl create -n $OSS_NAMESPACE configmap special-config --from-literal=codeBucket=$S3TEST_BUCKET

# Install k8s metrics server
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# Install Spark-Operator for the OSS Spark test
helm repo add spark-operator https://googlecloudplatform.github.io/spark-on-k8s-operator
helm install -n $OSS_NAMESPACE spark-operator spark-operator/spark-operator --version 1.1.26 \
  --set serviceAccounts.spark.create=false --set metrics.enable=false --set webhook.enable=true --set webhook.port=443 --debug

# Install Cluster Autoscaler that automatically adjusts the number of nodes in EKS
cat <<EOF >/tmp/autoscaler-config.yaml
---
autoDiscovery:
    clusterName: $EKSCLUSTER_NAME
awsRegion: $AWS_REGION
image:
    tag: v1.22.2
podAnnotations:
    cluster-autoscaler.kubernetes.io/safe-to-evict: 'false'
extraArgs:
    skip-nodes-with-system-pods: false
    scale-down-unneeded-time: 1m
    scale-down-unready-time: 2m
rbac:
    serviceAccount:
        create: false
        name: cluster-autoscaler
EOF

helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm install nodescaler autoscaler/cluster-autoscaler --namespace kube-system --values /tmp/autoscaler-config.yaml --debug

# config k8s rbac access to service account 'oss'
cat <<EOF | kubectl apply -f - -n $OSS_NAMESPACE
kind: Role
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: oss-role
  namespace: $OSS_NAMESPACE
rules:
  - apiGroups: ["", "batch","extensions"]
    resources: ["configmaps","serviceaccounts","events","pods","pods/exec","pods/log","pods/portforward","secrets","services","persistentvolumeclaims"]
    verbs: ["create","delete","get","list","patch","update","watch"]
---
kind: RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: oss-rb
  namespace: $OSS_NAMESPACE
subjects:
  - kind: ServiceAccount
    name: oss
    namespace: $OSS_NAMESPACE
roleRef:
  kind: Role
  name: oss-role
  apiGroup: rbac.authorization.k8s.io  
EOF
# echo "==========================================================================================="
# echo "  Patch k8s user permission for PVC only if eksctl version is < 0.111.0 or EMR < 6.8 ......"
# echo "==========================================================================================="
# curl -o rbac_pactch.py https://raw.githubusercontent.com/aws/aws-emr-containers-best-practices/main/tools/pvc-permission/rbac_patch.py
# python3 rbac_pactch.py -n $EMR_NAMESPACE -p

echo "Finished, proceed to submitting a job"
