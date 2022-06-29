# EMR Serverless Estimator

EMR Serverless Estimator is a tool to estimate the cost of running Spark jobs on EMR Serverless based on Spark event logs.

**Disclaimer**: This tool is provided for general information purpose only. You should not rely upon the information provided by this tool for making any business, legal or any other decisions. 

## How to use the tool
1. Upload estimator jar file and script to your S3 bucket

In this example:

* upload emr-serverless-estimator.sh in this project to s3://<s3-bucket-name>/eventlog/emr-serverless-estimator.sh
* upload SparkEventLogParser-slim-2.12-1.0.jar to s3://<s3-bucket-name>/eventlog/SparkEventLogParser-slim-2.12-1.0.jar (for EMR 6.x)
* upload SparkEventLogParser-slim-1.0.jar to s3://<s3-bucket-name>/eventlog/SparkEventLogParser-slim-1.0.jar (for EMR 5.x)

2. Copy Spark event logs to S3

You can run s3-dist-cp to copy event logs

```
aws emr add-steps \
--cluster-id <cluster-id> \
--steps 'Name=CopyEventLog,Jar=command-runner.jar,ActionOnFailure=CONTINUE,Type=CUSTOM_JAR,Args=s3-dist-cp,--src,/var/log/spark/apps,--dest,s3://<s3-bucket-name>/eventlog/<cluster-id>/'
```

Spark event log directory is defined at /etc/spark/conf/spark-defaults.conf -> spark.eventLog.dir. Default value is /var/log/spark/apps.

3. run emr-serverless-estimator script 

For an EMR 6.x cluster
```
aws emr add-steps \
--cluster-id <cluster-id> \
--steps 'Name=RunServerlessEstimator,Type=CUSTOM_JAR,ActionOnFailure=CONTINUE,Jar=s3://<cluster-region-e.g. us-east-2>.elasticmapreduce/libs/script-runner/script-runner.jar,Args=[s3://<s3-bucket-name>/eventlog/emr-serverless-estimator.sh,--logroot, s3://<s3-bucket-name>/eventlog/<cluster-id>/,--parserjar, s3://<s3-bucket-name>/eventlog/SparkEventLogParser-slim-2.12-1.0.jar]'
```
The output is saved in stdout log file of the step.

For open source Spark 3.x (Scala 2.12) cluster, you can copy emr-serverless-estimator.sh to the cluster, run the script from the cluster

```
emr-serverless-estimator.sh --logroot s3://ehaowang-bda-test-east2/eventlog/j-1QOH1KLURSK7L/ --parserjar s3://ehaowang-bda-test-east2/eventlog/SparkEventLogParser-slim-2.12-1.0.jar
```
The output is printed on the console.

Note: Change SparkEventLogParser-slim-2.12-1.0.jar file to SparkEventLogParser-slim-1.0.jar for EMR 5.x or Spark 2.x (scala 2.11).

## Command options
emr-serverless-estimator.sh options are:: 

* --logroot  [S3 path to Spark event log]  
* --parserjar [path to event log parser jar file]  
* --vCPU_per_hour_price [EMR serverless vCPU per hour price] optional, default value is 0.052624
* --GB_per_hour_price [EMR serverless per GB per hour price] optional, default value is 0.0057785  

Output is like below
```
app_id,driver_core_seconds,driver_memory_mb_seconds,total_executor_core_seconds,total_executor_memory_mb_seconds
"application_1652718818673_0001",146,299008,2020,4790430
"application_1652718818673_0002",266,544768,1520,3604680
"application_1652718818673_0003",66,135168,1320,3130380
"application_1652718818673_0004",138,282624,1500,3557250
"application_1652718818673_0005",158,323584,1900,4505850
"application_1652718818673_0006",8478,17362944,5078,12042477
"application_1652718818673_0008",60,122880,2400,2457600
"application_1652718818673_0009",60,122880,1380,1413120
"application_1652718818673_0010",60,122880,2400,2457600
"application_1652718818673_0011",60,122880,720,737280
"application_1652718818673_0012",60,122880,1800,1843200
"application_1652718818673_0013",60,122880,2340,2396160
"application_1652718818673_0014",60,122880,2100,2150400
"application_1652718818673_0015",60,122880,2220,2273280
"application_1652718818673_0016",60,122880,780,798720
"application_1652718818673_0017",76,155648,3040,3112960
"application_1652718818673_0018",359,735232,3530,8371395
"application_1652718818673_0019",31330,64163840,2100,2150400
 
total_core_seconds,total_memory_mb_seconds
79705,146901918
 
total_core_hours,total_memory_gb_hours
22.1403,39.8497
 
serverless_vcpu_per_hour_price,serverless_per_GB_per_hour_price
0.052624,0.0057785
 
estimated_serverless_vcpu_cost,estimated_serverless_memory_cost
1.16511,0.230271
```

## Compare the cost with transient EMR EC2 clusters

1. Add below steps as the last 2 steps of a transient EMR 6.x cluster  
```
aws emr add-steps \
--cluster-id <cluster-id> \
--steps 'Name=CopyEventLog,Jar=command-runner.jar,ActionOnFailure=CONTINUE,Type=CUSTOM_JAR,Args=s3-dist-cp,--src,/var/log/spark/apps,--dest,s3://<s3-bucket-name>/eventlog/<cluster-id>/'

aws emr add-steps \
--cluster-id <cluster-id> \
--steps 'Name=RunServerlessEstimator,Type=CUSTOM_JAR,ActionOnFailure=CONTINUE,Jar=s3://<cluster-region-e.g. us-east-2>.elasticmapreduce/libs/script-runner/script-runner.jar,Args=[s3://<s3-bucket-name>/eventlog/emr-serverless-estimator.sh,--logroot, s3://<s3-bucket-name>/eventlog/<cluster-id>/,--parserjar, s3://<s3-bucket-name>/eventlog/SparkEventLogParser-slim-2.12-1.0.jar]'
```
Note: Change SparkEventLogParser-slim-2.12-1.0.jar file to SparkEventLogParser-1.0.jar for EMR 5.x.

The estimated EMR Serverless cost is saved in the stdout log file of the last step

2. install aws-emr-cost-calculator2 

```
pip install -U aws-emr-cost-calculator2
```
Refer to the tool homepage for details:  https://github.com/mauropelucchi/aws-emr-cost-calculator


3. Get the cost of the EMR EC2 transient cluster given the cluster id

```
aws-emr-cost-calculator2 cluster --cluster_id=<cluster-id>
```