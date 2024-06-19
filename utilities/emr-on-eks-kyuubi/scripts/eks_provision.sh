#!/bin/bash

# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0

# Define params
export EKSCLUSTER_NAME="${1:-eks-kyuubi}"
export AWS_REGION="${2:-us-east-1}"

export EMR_NAMESPACE=emr
export KYUUBI_NAMESPACE=kyuubi
export EKS_VERSION=1.28
export EMRCLUSTER_NAME=emr-on-$EKSCLUSTER_NAME
export ROLE_NAME=${EMRCLUSTER_NAME}-execution-role
export ACCOUNTID=$(aws sts get-caller-identity --query Account --output text)
export S3TEST_BUCKET=${EMRCLUSTER_NAME}-${ACCOUNTID}-${AWS_REGION}

echo "==============================================="
echo "  setup IAM roles ......"
echo "==============================================="

# create S3 bucket for application
if [ $AWS_REGION=="us-east-1" ]; then
  aws s3api create-bucket --bucket $S3TEST_BUCKET --region $AWS_REGION 
else
  aws s3api create-bucket --bucket $S3TEST_BUCKET --region $AWS_REGION --create-bucket-configuration LocationConstraint=$AWS_REGION
fi
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
    roleName: $EKSCLUSTER_NAME-$AWS_REGION-cluster-autoscaler-role
  - metadata:
      name: ebs-csi-controller-sa
      namespace: kube-system
    wellKnownPolicies:
      ebsCSIController: true   
    roleName: $EKSCLUSTER_NAME-$AWS_REGION-ebs-csidriver-role  
  - metadata:
      name: cloudwatch-agent-sa
      namespace: kube-system
      labels: {aws-usage: "monitoring"}
    attachPolicyARNs:
    - arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy
    - arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccess
    roleName: $EKSCLUSTER_NAME-$AWS_REGION-cw-containerinsight-role 
  - metadata:
      name: cross-ns-kyuubi
      namespace: $KYUUBI_NAMESPACE
      labels: {aws-usage: "app"}
    attachPolicyARNs:
    - arn:aws:iam::${ACCOUNTID}:policy/$ROLE_NAME-policy
    roleName: $EKSCLUSTER_NAME-$AWS_REGION-kyuubi-role  
managedNodeGroups: 
  - name: $EKSCLUSTER_NAME-spot
    spot: true
    instanceTypes: ["c5.2xlarge","c5a.2xlarge","c5n.2xlarge","c5.4xlarge","c5a.4xlarge","c5n.4xlarge","c5a.8xlarge","c5.9xlarge","c5n.9xlarge"]
    availabilityZones: ["{AWS_REGION}b"] 
    preBootstrapCommands:
      - "IDX=1;for DEV in /dev/nvme[1-9]n1;do sudo mkfs.xfs \${DEV}; sudo mkdir -p /local\${IDX}; sudo echo \${DEV} /local\${IDX} xfs defaults,noatime 1 2 >> /etc/fstab; IDX=\$((\${IDX} + 1)); done"
      - "sudo mount -a"
      - "sudo chown ec2-user:ec2-user /local*"
    volumeSize: 20
    volumeType: gp3
    minSize: 1
    desiredCapacity: 1
    maxSize: 30
    additionalVolumes:
      - volumeName: '/dev/xvdf'
        volumeSize: 200
        volumeType: gp3
        # volumeEncrypted: true
        volumeIOPS: 3000
        volumeThroughput: 125
    tags:
      # required for cluster-autoscaler auto-discovery
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/$EKSCLUSTER_NAME: "owned"  
# enable all of the control plane logs
cloudWatch:
 clusterLogging:
   enableTypes: ["*"]
EOF

# create eks cluster in a single AZ
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

# Create a kyuubi service account in emr namespace
export KYUUBI_SA=emr-kyuubi
eksctl create iamserviceaccount \
 --name $KYUUBI_SA \
 --namespace $EMR_NAMESPACE \
 --cluster $EKSCLUSTER_NAME \
 --attach-policy-arn arn:aws:iam::${ACCOUNTID}:policy/$ROLE_NAME-policy \
 --approve

# Bind the newly created SA "emr-kyuubi"
# to a pre-created EMR on EKS k8s role "emr-containers-role-spark-driver"
cat << EoF > /tmp/kyuubi-rolebinding.yaml
kind: RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: $KYUUBI_SA-rolebinding
  namespace: $EMR_NAMESPACE
subjects:
- kind: ServiceAccount
  name: $KYUUBI_SA
  namespace: $EMR_NAMESPACE
roleRef:
  kind: Role
  name: emr-containers-role-spark-driver
  apiGroup: rbac.authorization.k8s.io
EoF
kubectl apply -f /tmp/kyuubi-rolebinding.yaml

echo "==============================================="
echo "  Configure EKS Cluster ......"
echo "==============================================="
# Install container insight add-on to EKS
aws eks create-addon --addon-name amazon-cloudwatch-observability --cluster-name $EKSCLUSTER_NAME --service-account-role-arn arn:aws:iam::$ACCOUNTID:role/$EKSCLUSTER_NAME-$AWS_REGION-cw-containerinsight-role 
# Install CSI driver add-on
eksctl create addon --name aws-ebs-csi-driver --cluster $EKSCLUSTER_NAME  --service-account-role-arn arn:aws:iam::$ACCOUNTID:role/$EKSCLUSTER_NAME-$AWS_REGION-ebs-csidriver-role --force
# Install k8s metrics server
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# Install Cluster Autoscale that automatically adjusts the number of nodes in EKS
cat <<EOF >/tmp/autoscaler-config.yaml
---
autoDiscovery:
    clusterName: $EKSCLUSTER_NAME
awsRegion: $AWS_REGION
image:
    tag: v1.28.2
nodeSelector:
    app: sparktest    
podAnnotations:
    cluster-autoscaler.kubernetes.io/safe-to-evict: 'false'
extraArgs:
    skip-nodes-with-system-pods: false
    scale-down-unneeded-time: 2m
    scale-down-unready-time: 5m
rbac:
    serviceAccount:
        create: false
        name: cluster-autoscaler
EOF

helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm install nodescaler autoscaler/cluster-autoscaler --namespace kube-system --values /tmp/autoscaler-config.yaml --debug

# echo "============================================================================="
# echo "  Upload project examples to S3 ......"
# echo "============================================================================="
# aws s3 sync examples/ s3://$S3TEST_BUCKET/app_code/

echo "Finished, proceed to submitting a job"
