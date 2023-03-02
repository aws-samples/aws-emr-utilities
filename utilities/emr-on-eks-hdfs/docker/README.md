
## Build and Push to Amazon Elastic Container Registry (ECR)
login to ECR:
```
AWS_REGION=YOUR_REGION
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL
aws ecr create-repository --repository-name hdfs --image-scanning-configuration scanOnPush=true
```
build the image:
```
cd docker
docker build -t $ECR_URL/hdfs -f Dockerfile_emr.yaml .
docker push $ECR_URL/hdfs
```
## Quick test
```
docker-compose -f docker-compose.yaml
```
Access the HDFS NameNode web UI at: http://localhost:9870
