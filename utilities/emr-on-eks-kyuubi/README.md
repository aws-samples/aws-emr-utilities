# EMR on EKS - Kyuubi

This project demonstrates the capabilities of [Apache Kyuubi](https://kyuubi.readthedocs.io/en/latest/index.html)
to create a unified SQL query layer that can be used to access different data
sources using Apache Spark as query engine.

In this project we're going to run Apache Kyuubi on EKS leveraging a customized EMR on EKS container image. It allows us to takes the advantage of an optimized EMR Spark runtime, while processing our SQL queries.

Besides, the project also provides scripts to install an OpenLDAP server, and an Apache Ranger Admin server to demonstrate how to integrate AuthN and AuthZ capabilities in Kyuubi.

The fundamental technical architecture of the solution is shown in the following diagram:

![architecture](./images/arch.jpeg)

Kyuubi components running in a k8s environment:

```
meloyang ~/sourcecode/emr-on-eks-kyuubi (main) >> kubectl get all -n kyuubi
NAME           READY   STATUS    RESTARTS   AGE
pod/kyuubi-0   1/1     Running   0          3h18m
pod/kyuubi-1   1/1     Running   0          3h18m

NAME                           TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)                                  AGE
service/kyuubi-headless        ClusterIP   None            <none>        3309/TCP,10099/TCP,10009/TCP,10010/TCP   3h18m
service/kyuubi-rest            ClusterIP   10.100.139.84   <none>        10099/TCP                                3h18m
service/kyuubi-thrift-binary   ClusterIP   10.100.146.98   <none>        10009/TCP                                3h18m

NAME                      READY   AGE
statefulset.apps/kyuubi   2/2     3h18m
```

## Setup environment
If you don't have your own environment to test the solution, run the following commands to setup the infrastructure you need. Change the region if needed.

### Prerequisite
- eksctl >= 0.143.0
- Helm CLI >= 3.2.1 
- kubectl >= 1.28.0 
- AWS Cli >= 2.11.23

To set up the infrastructure environment, run the following scripts in [AWS CloudShell](https://us-east-1.console.aws.amazon.com/cloudshell?region=us-east-1). The default region is `us-east-1`. **Change it on your console if needed**. Alternatively, setup the environment from your local computer.

```bash
# download the project
git clone https://github.com/aws-samples/aws-emr-utilities.git

cd aws-emr-utilities/utilities/emr-on-eks-kyuubi
echo $AWS_REGION
````
Run the script to install required CLI tools: eksctl,helm CLI,kubectl. Skip this step if you have these command tools.
```bash
./scripts/cli_setup.sh
```
Create a new EKS cluster and enable EMR on EKS:
```bash
# param1: EKS CLUSTER NAME, default: 'eks-kyuubi'
# param2: Deployment region, default: 'us-east-1'
./scripts/eks_provision.sh
```
OR
```bash
./scripts/eks_provision.sh  <YOUR_EKS_NAME> <YOUR_AWS_REGION>
```

## Quick Start

### Build a custom EMR on EKS image that contains Kyuubi

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=us-west-2
ECR_URL=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 755674844232.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URL

# create a new repository in your ECR, **ONE-OFF task**
aws ecr create-repository --repository-name $ECR_URL/kyuubi-emr-eks --image-scanning-configuration scanOnPush=true

docker buildx build --platform linux/amd64,linux/arm64 \
-t $ECR_URL/kyuubi-emr-eks:6.10 \
-f dockers/kyuubi/Dockerfile \
--build-arg SPARK_BASE_IMAGE=755674844232.dkr.ecr.us-east-1.amazonaws.com/spark/emr-6.10.0 \
--build-arg KYUUBI_VERSION=1.7.3 --push .
```

### Helm install Kyuubi
1. Edit the chart's values.yaml file. Replace all images URIs by the custom EMR on EKS image `$ECR_URL/kyuubi-emr-eks:6.10`
```bash
vi charts/my-values.yaml
```
2. Install Kyuubi
```bash
helm install kyuubi charts/kyuubi -n kyuubi --create-namespace -f charts/my-kyuubi-values.yaml --debug
# check the installation progress
kubectl get all -n kyuubi
```
Uninstall the chart if needed:
```bash
helm uninstall kyuubi -n kyuubi
```

3. To enable Kyuubi create Spark pods across a different namespace "emr", ensure the "kyuubi" SA bind a role in "emr". 
```bash
kubectl describe rolebinding kyuubi -n emr
```
Expected outcome is:
```yaml
Name: kyuubi
.....
Subjects:
  Kind            Name            Namespace
  ----            ----            ---------
  ServiceAccount  cross-ns-kyuubi  kyuubi
```

### Validation
For a quick start, firstly, login to a Kyuubi server.
```bash
kubectl exec -it pod/kyuubi-0 -n kyuubi -- bash
```

1. submit a Spark job from Kybuui server to EMR on EKS namespace
```bash
spark-submit \
--master k8s://https://kubernetes.default.svc:443 \
--deploy-mode cluster \
--class org.apache.spark.examples.SparkPi \
--conf spark.executor.instances=5 \
 local:///usr/lib/spark/examples/jars/spark-examples.jar 10000
```

```yaml
~/sourcecode/emr-on-eks-kyuubi (main) >> kubectl get po -n emr -w
NAME                                                        READY   STATUS    RESTARTS   AGE
org-apache-spark-examples-sparkpi-1dc1958e8b4c5001-driver   1/1     Running   0          8s
spark-pi-0672778e8b4c63ed-exec-1                            1/1     Running   0          3s
spark-pi-0672778e8b4c63ed-exec-2                            1/1     Running   0          3s
spark-pi-0672778e8b4c63ed-exec-3                            1/1     Running   0          3s
spark-pi-0672778e8b4c63ed-exec-4                            1/1     Running   0          3s
spark-pi-0672778e8b4c63ed-exec-5                            1/1     Running   0          3s
```

2. connect to Kyuubi server via Thrift (HiveServer2 compatible)
```bash
./bin/beeline -u 'jdbc:hive2://kyuubi-0.kyuubi-headless.kyuubi.svc.cluster.local:10009?spark.app.name=testdelta' -n hadoop
```
3. let's create a sample database and table in Delta format, which are mapping to an S3 bucket that you have access to. In the Glue console, you should be able to see the related metadata generated in AWS Glue Data Catalog under the database `kyuubi_delta` with a S3 location.

 ![gdc](./images/glue_db.jpeg)

```yaml
0: jdbc:hive2://kyuubi-0.kyuubi-headless.kyuu> CREATE DATABASE IF NOT EXISTS kyuubi_delta LOCATION 's3://YOUR_S3_BUCKET/delta';
......
0: jdbc:hive2://kyuubi-0.kyuubi-headless.kyuu> USE kyuubi_delta;
......
0: jdbc:hive2://kyuubi-0.kyuubi-headless.kyuu> CREATE TABLE table_with_col USING DELTA AS SELECT col1 as id FROM VALUES 0,1,2,3,4;
......
+---------+
| Result  |
+---------+
+---------+
No rows selected (5.32 seconds)
0: jdbc:hive2://kyuubi-0.kyuubi-headless.kyuu> show tables;
+---------------+-----------------+--------------+
|   namespace   |    tableName    | isTemporary  |
+---------------+-----------------+--------------+
| kyuubi_delta  | table_with_col  | false        |
+---------------+-----------------+--------------+
1 row selected (0.325 seconds)
0: jdbc:hive2://kyuubi-0.kyuubi-headless.kyuu> SELECT * FROM kyuubi_delta.table_with_col;
+-----+
| id  |
+-----+
| 2   |
| 3   |
| 4   |
| 0   |
| 1   |
+-----+
5 rows selected (1.74 seconds)


0: jdbc:hive2://kyuubi-0.kyuubi-headless.kyuu> !quit
```

## Kyuubi Security (WIP)
Securing Kyuubi involves enabling authentication(authn), authorization(authz) in this example. 

### Install OpenLDAP (authentication)
LDAP is commonly used for user authentication against corporate identity servers that are hosted on applications such as Active Directory (AD) and OpenLDAP. In this example, we will use OpenLDAP to test Kyuubi's AuthN capability.

Now let's install the OpenLDAP for authentication, which will be used to provide a strong authN capability while using Kyuubi.
Install the LDAP server:
```bash
helm install ldap charts/openldap -f charts/openldap/values.yaml -n kyuubi --debug
# list all the bjects created by the helm chart
kubectl get all -l "release=ldap" -n kyuubi

# test the connection to the LDAP server
helm test ldap -n kyuubi
```
This Helm Chart installs a LDAP server and a web app 'phpLDAPadmin' administering the LDAP server. To demonstrate how the seucurity works with Kyuubi, we created 3 groups - kafka_test_user,kafka_prod_user,kafka_prod_admin and 3 users - user1,user2,user3 from the LDAP webUI. To login to the admin site http://localhost:8080/, we need to port-forwarding from the local first ( create an ingress in helm chart if you don't want the port-forwarding ):
```bash
kubectl port-forward service/ldap-php-svc 8080:8080 -n kyuubi
# URL: http://localhost:8080/
# Login: cn=admin,dc=ranger,dc=local
# password: admin
```
![ldap](./images/ldap_admin.jpeg)


### Install Ranger Admin Server (authorization)
When row/column-level fine-grained access control is required, we can stronger the data access with the Kyuubi Spark AuthZ Plugin. The plugin provides the fine-grained ACL management for data & metadata while using Spark SQL.

Apache Ranger enables Kyuubi with data and metadata ACL for Spark SQL Engines, including:

- Column-level fine-grained authorization
- ow-level fine-grained authorization, a.k.a. Row-level filtering
- Data masking

Using the same way, we install the Ranger Admin Server via the similar commands. 

```bash
helm install ranger charts/ranger -f charts/ranger/values.yaml -n kyuubi --debug
```

After the Ranger server is started, the ranger-usersync will begin synchronizing users and groups from the LDAP to Ranger. To login to the server and validate, connect to the Ranger Server `http://localhost:6080` and use the Ranger credentials: `username: admin  password: Rangeradmin1!`. You'll be required to use an SSH tunnel to access the interface before access the web interface:
```bash
# get ranger admin server pod name
export pod_name=`kubectl get pods -n kyuubi | awk '/admin/ {print $1;exit}'`
# ssh tunneling
kubectl port-forward $pod_name -n kyuubi 6080:6080
```

![ranger](./images/ranger_admin.png)


## Testing

We have now deployed Kyuubi secured by LDAP and Ranger, so it's time for some testings.

### Create sample datasets
We're going to create a sample dataset on S3 that is secured by Ranger policies. Your kyuubi pods should be able to access to the S3 via EKS's IRSA feature. To validate the permission, find your IAM role name from this command first:
```bash
kubectl describe sa cross-ns-kyuubi -n kyuubi
```
Once confirmed that you have the required access in the IAM role, let's create some sample data against the s3 bucket. 
```bash
# login to one of kyuubi instances in EKS, 
kubectl exec -it pod/kyuubi-0 -n kyuubi -- bash

# after login to the kyuubi, spin up the spark shell
spark-shell --master local --deploy-mode client --conf spark.hadoop.hive.metastore.client.factory.class=com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory

```

The following code snippet creates two sample tables - customer and store_sales, mapping to the S3 bucket that you have access to. In the Glue console, you should be able to see the related table metadata are generated in AWS Glue Data Catalog under the database `aws_kyuubi`. 

Replace the "s3_location" to you own, then run the following commands:
```scala
val s3_location = "s3://YOUR_S3_BUCKET/secure-datalake/"
spark.sql(s"CREATE DATABASE IF NOT EXISTS secure-datalake LOCATION '$s3_location'")

val customer = Seq(
  (1, "Lorenzo", "Dr", "lorenzodr@example.com", 1000),
  (2, "Jeff", "Bezos", "jeff@example.com", 400000000),
  (3, "Tom", "Brady", "tom@example.com", 10000000)
).toDF("id", "first_name","last_name","mail","balance")

val store_sales = Seq(
  (1, 1, 10),
  (1, 2, 1230),
  (1, 2, 5090),
  (1, 3, 498)
).toDF("id", "c_id","price")

// Common Parquet table
customer.write.format("parquet").mode("overwrite").saveAsTable("secure-datalake.customer")
store_sales.write.format("parquet").mode("overwrite").saveAsTable("secure-datalake.store_sales")

// check data
spark.sql("SELECT * FROM secure-datalake.customer").show
spark.sql("SELECT * FROM secure-datalake.store_sales").show
```
The outputs are :
```
scala> spark.sql("SELECT * FROM aws_kyuubi.customer").show
2024-06-16 17:54:49,423 main ERROR Filters contains invalid attributes "onMatch", "onMismatch"
17:54:49.429 INFO org.apache.hadoop.hive.conf.HiveConf: Found configuration file jar:file:/usr/lib/hudi/hudi-utilities-bundle.jar!/hive-site.xml
17:54:50.270 INFO com.amazonaws.glue.catalog.metastore.AWSGlueClientFactory: Using region from ec2 metadata : us-west-2
+---+----------+---------+---------------------+---------+                        
| id|first_name|last_name|               mail  |  balance|
+---+----------+---------+---------------------+---------+
|  1|   Lorenzo|       Dr|lorenzodr@example.com|     1000|
|  2|      Jeff|    Bezos|     jeff@example.com|400000000|
|  3|       Tom|    Brady|      tom@example.com| 10000000|
+---+----------+---------+---------------------+---------+

scala> spark.sql("SELECT * FROM aws_kyuubi.store_sales").show
+---+----+-----+
| id|c_id|price|
+---+----+-----+
|  1|   1|   10|
|  1|   2| 1230|
|  1|   2| 5090|
|  1|   3|  498|
+---+----+-----+
```
### Ranger Policies
Finally we're going to create the Ranger policies to test the access to our tables.
To create the policies, copy the script `scripts/ranger_policies.sh` and launch the
following commands:

```bash
chmod +x ./ranger_policies.sh
./ranger_policies.sh
```

The policies created gives access to our analyst user to the `customer` table
only, and they apply a data mask on the column `mail`.

### Testing
To test everything is properly setup, launch the following commands to submit
some sample queries:

```bash
beeline -u jdbc:hive2://`kubectl -n $EKS_NS get service/kyuubi-balancer -o json | jq -r .status.loadBalancer.ingress[].hostname`:10009 -n analyst -p Password123
```

Once opened the SQL editor type

```sql
-- this will display only one tables as we granted permissions only for the
-- customer table. Ranger filters metadata accordingly to permissions
SHOW TABLES IN aws_kyuubi;

-- This will print the customer data with an hash in the mail column (data mask)
SELECT * FROM aws_kyuubi.customer;

-- This query should fail as we do not have permissions to access the table
-- Permission denied: user [analyst] does not have [select] privilege on ...
SELECT * FROM aws_kyuubi.store_sales;
```
