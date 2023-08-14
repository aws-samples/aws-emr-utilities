# EMR Observability

EMR observability is a utility designed to enhance your monitoring and understanding of what's happening on your 'EMR on EC2' clusters. It helps you gain continuous insights into your clusters and applications, receive timely alerts, and leverage actionable insights to optimize your EMR clusters and predict and prevent issues before they impact your operations.

##### Two Usage Options:
###### 1) Usign Centralized Prometheus and Grafana on EC2 instance 
Prometheus server collects metrics from the EMR clusters that are monitored and Grafana queries prometheus server to generate visual dashboards.
* Note: In this option, please review and understand the license terms of Grafana, particularly the shift to AGPLv3, and be aware of its implications for your software usage - https://grafana.com/licensing/

###### 2) Using Amazon Managed Prometheus(AMP) and Amazon Managed Grafana(AMG) 
Bootstrap action script installs prometheus on the master node of EMR cluster. This prometheus server will scrape the metrics and the scraped metrics is exported to your Amazon Managed Prometheus workspace via the remote_write endpoint.

## Why EMR Observability

* To have a single globalized view of metrics across all the EMR clusters in the account
* To have a one-stop experience for all the metrics within EMR cluster
* To retain the metrics even after the cluster is terminated
* To monitor work load and infrastructure in real time.

With the current available solutions, there are some limitations. For example,

*EMR Ganglia*
The metrics are gone once the cluster is terminated.
Metrics can be viewed only per cluster and it becomes difficult to monitor when customers launch many clusters per day.

### Supported Features
* Globalized view of metrics across all EMR clusters
* Dashboard to optimize the EMR cluster
* OS level metrics
* Yarn Resource Manager Metrics
* Yarn Node Manager metrics
* HDFS NameNode metrics
* HDFS DataNode metrics
* Hbase metrics

**Other AWS Services used for option 2**

*AMP* - Amazon Managed Service for Prometheus is a Prometheus-compatible monitoring and alerting service that makes it easy to monitor containerized applications and infrastructure at scale. AMP has workspace which stores all the prometheus data.
Refer this doc for AMP pricing - https://aws.amazon.com/prometheus/pricing/

*AMG* - Amazon Managed Grafana is a fully managed service for open source Grafana developed in collaboration with Grafana Labs. Grafana is a popular open source analytics platform that enables you to query, visualize, alert on and understand your metrics no matter where they are stored.
Refer this doc for AMG pricing - https://aws.amazon.com/grafana/pricing/

# Instructions
### Option - 1
#### How to use
1)Install Prometheus and Grafana on EC2 instance or a single node EMR cluster using the below scripts preferably in same VPC to simplify network access - Network access to inbound TCP ports 22 (SSH), Grafana (3000) and Prometheus UI (9090) needs to be opened
*install_prometheus_on_EC2.sh*
*install_grafana_on_EC2.sh*

2)Configure prometheus endpoint 'Endpoint - remote write URL' in remote_write url in script - install_prometheus_v2.sh

3)While initiating the launch of the EMR on EC2 clusters
a)Ensure metric export setup by using the provided bootstrap action script
```
--bootstrap-actions '[{"Path":"s3://bucket-name/path/install_prometheus_v2.sh","Name":"Install Prometheus"}]'
```
b)Use the below EMR configuration classification json
```
./conf_files/configuration.json
```

4)You can now start visualizing the metrics in Grafana on port 3000


### Option - 2
#### How to use

1)Create workspace in AMP - https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-onboard-create-workspace.html (You can igore this if you already have a workspace in AMP)

2)Get 'Workspace ID' from AMP console. 

3)Add policy "AmazonPrometheusRemoteWriteAccess" to EC2 instance profile(Service role for EMR cluster EC2 instances) to provide permission to remote write metrics into all Amazon Managed Service for Prometheus workspaces in the account

4)When initiating the launch of EMR clusters, 
a)Ensure metric export setup by using the provided bootstrap action script "install_prometheus_v2.sh" and adding the AWS Prometheus workspace ID as an argument.
```
--bootstrap-actions '[{"Path":"s3://<s3_path>/install_prometheus_v2.sh","Args":["ws-537c7364-f10f-4210-a0fa-deedd3ea1935"]}]'
```
b)Use the below EMR configuration classification json
```
./conf_files/configuration.json
```
5)You can now start visualizing the metrics in AMG

### Setting up Ganglia Dashboards
#### Yarn and OS level metrics Dashboards
Import the dashboards from utilities/emr-observability/grafana_dashboards
#### Hbase
Use this ID "12243" for importing the dashboard into Grafana
#### Spark
In Progress

## Work in progress
* Spark metrics support
* Setup Alerts

## Limitations
* Trino/Presto Metrics
* Tez metrics(Hive)

### Dashboard examples - EMRonEC2OptimizationDashboard
![Alt text](images/Optimization-1.png?raw=true "Optimization Dashboard - OS and Yarn memory utilization comparison for tuning")

![Alt text](images/Optimization-2.png?raw=true "Optimization Dashboard - OS and Yarn CPU utilization comparison for tuning")

![Alt text](images/Optimization-3.png?raw=true "Optimization Dashboard - IO, Disk and HDFS utilization for tuning")

### Dashboard examples - HbaseDashboard
![Alt text](images/HbaseGrafana-1.png?raw=true "Hbase Dashboard")

### Dashboard examples - SparkDashboard
In Progress

###  Recommended Actions
In the **EMRonEC2OptimizationDashboard**

***1) Check the OS CPU utilization and OS memory utilization.***

a)If both the CPU utilization and memory utilization are at or near 100% capacity, it indicates that the system is experiencing a resource bottleneck. In this case, adding more worker nodes can help to distribute the load and increase the system's capacity to handle the workload. We would also suggest to use EMR managed scaling feature which automatically adjusts the cluster size based on the workload. If you have already configured managed scaling, you can increase the maximum capacity of the cluster to allow for more nodes to be added when needed. This can help to optimize the use of resources and reduce costs by avoiding over-provisioning.

b)If the CPU utilization is at or near 100% but memory utilization is low, it indicates that the bottleneck is likely the CPU rather than the memory. In this case, we suggest to use CPU optimized instance type(c series) which has higher CPU-to-memory ratios and are designed to handle compute-intensive workloads

c)If the memory utilization is at or near 100% in the cluster but the CPU utilization is low, it indicates that the bottleneck is likely the memory, rather than the CPU. In this case, we suggest to use memory optimized instance type(r series) which provide high memory-to-CPU ratios, making them ideal for memory-intensive workloads.

***2)Check the OS Memory utilization and Yarn memory utilization.***

If Yarn memory utilization is at 100% while the OS memory utilization is low, it suggests that resource is over allocated resources to yarn and it needs tuning. Some of the settings to tune here are yarn.nodemanager.resource.memory-mb, yarn.scheduler.minimum-allocation-mb, yarn.scheduler.maximum-allocation-mb.

### Best Practices
Coming soon
