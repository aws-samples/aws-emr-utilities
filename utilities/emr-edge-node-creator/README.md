# EMR Edge Node Creator

A Cloudformation or Docker-based tool to create an EC2 instance that acts as a user portal to communicate with an EMR cluster node. Users leverage the portal as their edge node or jump box to submit tasks to the EMR cluster.

## How to use the CFN tool
1. Deploy the tool via CFN
```
wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-edge-node-creator/create-edge-node-CFN.yml
```
Go to [CloudFormtaion Console](https://console.aws.amazon.com/cloudformation/home) to create the stack.

*Considerations:*
- Create the edge node in the same VPC as the EMR cluster. Use the same IAM role and security group that the EMR primary node uses.
- The docker host machine needs to be able to access EMR application ports.


2. Check the EMR step

Go to [EMR Console](https://us-east-1.console.aws.amazon.com/elasticmapreduce), select the EMR cluster you setup the edge node for. Check if the `CopyEMRConfigsStep` step is completed successfully.


3. Validate Edge Node 

SSH or use Session Manager login to the edge node EC2 instance. Use the Hadoop user:
```
sudo su hadoop
```

```
# test mapreduce
hadoop-mapreduce-examples pi 10 10000

# test hive if your EMR cluster has intalled the Hive app
# you can see databases from Glue Catalog if Hive uses Glue as metastore
hive -e "show databases"

# test Presto if it is installed
presto-cli --catalog hive --schema YOUR_SCHEMA --debug --execute "show tables" --output-format ALIGNED

# test Spark if the app is installed
spark-submit --deploy-mode cluster /usr/lib/spark/examples/src/main/python/pi.py
# The output is saved in stdout log file
yarn logs -log_files stdout -applicationId YOUR_APP_ID
```

## How to use the Docker tool

Some users run edge node in a docker container environment, such as in Amazon EKS or ECS. 

1. run an EMR step to download client dependencies from the EMR cluster to S3 (on EMR console or using CLI)

```
JAR location: command-runner.jar

Arguments : "bash" "-c" "sudo rm -rf /tmp/emr_edge_node && sudo yum install git -y && git clone --depth 1 https://github.com/aws-samples/aws-emr-utilities.git /tmp/emr_edge_node && cd /tmp/emr_edge_node && git filter-branch --prune-empty --subdirectory-filter utilities/emr-edge-node-creator HEAD && chmod +x copy-emr-client-deps.sh && sudo copy-emr-client-deps.sh --s3-folder s3://YOUR_BUCKET/emr-client-deps/"
```

After the step run is completed, the client deps tarball will be available at:

s3://YOUR_ARTIFACT_BUCKET/emr-client-deps/emr-client-deps-YOUR_EMR_CLUSTER_ID.tar.gz

Or use AWS CLI:
```
aws emr add-steps --cluster-id YOUR_EMR_CLUSTER_ID --steps Type=CUSTOM_JAR,Name=CopyEMRConfigsStep,ActionOnFailure=CANCEL_AND_WAIT,Jar=command-runner.jar,Args=["bash","-c","sudo rm -rf /tmp/emr_edge_node && sudo yum install git -y && git clone --depth 1 https://github.com/aws-samples/aws-emr-utilities.git /tmp/emr_edge_node && cd /tmp/emr_edge_node && git filter-branch --prune-empty --subdirectory-filter utilities/emr-edge-node-creator HEAD && bash copy-emr-client-deps.sh --s3-folder s3://YOUR_ARTIFACT_BUCKET/emr-client-deps/"] --region YOUR_REGION
```
2. Build the docker image
```
export AWS_REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name emr-edgenode --image-scanning-configuration scanOnPush=true

docker build -t $ECR_URL/emr-edgenode .
docker push $ECR_URL/emr-edgenode
```

3. Create an EC2 instance as the Dockerized EMR Edge Node (EC2 Console or CLI)

CLI approach:
```
# Choose a Linux AMI contains Docker daemon:
ami_id=$(aws ssm get-parameters --names /aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id --query "Parameters[0].Value" --region $AWS_REGION --output text)

# create the edge node
aws ec2 run-instances \
    --image-id $ami_id \
    --count 1 \
    --instance-type c5.2xlarge \
    --iam-instance-profile Name=EMR_EC2_DefaultRole \
    --security-group-ids YOUR_EMR_MASTER_SG \
    --subnet-id YOUR_EMR_SUBNET \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=emr-edgenode}]'
    --user-data sudo yum install -y aws-cli
```

4. Run the docker container on edge node
Make sure add the following ECR permission to the instance profile IAM role `EMR_EC2_DefaultRole`:
```
{
            "Sid": "AllowAddStep",
            "Effect": "Allow",
            "Action": [
                "ecr:Get*",
                "ecr:BatchGetImage"
            ],
            "Resource": "*"
  }
```

SSH or use Session Manager login to the EMR Edge Node
```
sudo su
export AWS_REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
docker pull $ECR_URL/emr-edgenode
```

```
# run the docker container. the gz file in S3 is generated by step 1
docker run -it -d --name emr-client $ECR_URL/emr-edgenode --emr-client-deps s3://YOUR_BUCKET/emr-client-deps/emr-client-deps-YOUR_EMR_CLUSTER_ID.tar.gz tail -f /dev/null

# check if the emr-client container is up running.
docker ps
# check the setup progress.Usually it takes 2min.
docker logs emr-client
```

5. test apps on the edge node
```
sudo docker exec -it emr-client hadoop-mapreduce-examples pi 10 10000

# test hive if it is installed. It will list databases from Glue Catalog, if we use Glue as metastore
sudo docker exec -it emr-client hive -e "show databases"

# test Presto if installed
sudo docker exec -it emr-client presto-cli --catalog hive --schema YOUR_SCHEMA --debug --execute "show tables" --output-format ALIGNED

# test Spark if installed
sudo docker exec -it emr-client spark-submit --deploy-mode cluster /usr/lib/spark/examples/src/main/python/pi.py
# The output is saved in stdout log file
sudo docker exec -it emr-client yarn logs -log_files stdout -applicationId YOUR_APP_ID
```

