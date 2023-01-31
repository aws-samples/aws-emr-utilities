## EMR Observability
 
EMR observability is a utility for monitoring and understanding whats happening on your EMR on EC2 clusters. It helps you to continuosly monitor the clusters and applications, receive alerts, enables you to get actionable insights to optimize the EMR clusters and predict problems before they occur.
 
You have two options to use this.
1) Usign Centralized Prometheus and Grafana Server on EC2 instance - Prometheus server collects metrics from the EMR clusters that are monitored and Grafana queries prometheus server to generate visual dashboards. 
2) Using Amazon Managed Prometheus(AMP) and Amazon Managed Grafana(AMG) - Bootstrap action script installs prometheus on the master node of EMR cluster. This prometheus server will scrape the metrics and the scraped metrics is exported to your Amazon Managed Prometheus workspace via the remote_write endpoint.
 
## Why EMR Observability
 
* To have a single globalized view of metrics across all the EMR clusters in the account
* To have a one-top experience for all the metrics within EMR cluster
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
* Spark metrics when not run concurrently
 
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
 
2)Configure prometheus endpoint 'Endpoint - remote write URL' in remote_write url in script - install_prometheus.sh
 
3)While launching the EMR clusters
Use the script as the bootstrap action
```
--bootstrap-actions '[{"Path":"s3://bucket-name/path/install_prometheus.sh","Name":"Install Prometheus"}]'
```
Optimization-1.pngUse the below EMR configuration classification json
```
./conf_files/configuration.json
```
 
4)You can now start visualizing the metrics in Grafana on port 3000
 
 
### Option - 2
#### How to use
 
1)Create workspace in AMP - https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-onboard-create-workspace.html (You can igore this if you already have a workspace in AMP)
 
2)Get prometheus endpoint 'Endpoint - remote write URL' from AMP console and edit remote_write url in script - install_prometheus.sh
```
  remoteWrite:
    - url: https://aps-workspaces.${AWS_REGION}.amazonaws.com/workspaces/${WORKSPACE_ID}/api/v1/remote_write
      sigv4:
        region: ${AWS_REGION}
      queue_config:
        max_samples_per_send: 1000
        max_shards: 200
        capacity: 2500
```
3)Add policy "AmazonPrometheusRemoteWriteAccess" to EC2 instance profile(Service role for EMR cluster EC2 instances) to provide permission to remote write metrics into all Amazon Managed Service for Prometheus workspaces in the account
 
4)While launching the EMR clusters
Use the script as the bootstrap action
```
--bootstrap-actions '[{"Path":"s3://bucket-name/path/install_prometheus.sh","Name":"Install Prometheus"}]'
```
Use the below EMR configuration classification json
```
./conf_files/configuration.json
```
5)You can now start visualizing the metrics in AMG
 
## Work in progress
* Cloudwatch metrics Integration
* Setup Alerts
 
## Limitations
* Spark jobs fail when run concurrently. 
* Trino/Presto Metrics
* Tez metrics(Hive)
 
### Dashboard examples - EMRonEC2OptimizationDashboard
 
![Alt text](images/Optimization-1.png?raw=true "Optimization Dashboard - OS and Yarn memory utilization comparison for tuning")
 
![Alt text](images/Optimization-2.png?raw=true "Optimization Dashboard - OS and Yarn CPU utilization comparison for tuning")
 
![Alt text](images/Optimization-3.png?raw=true "Optimization Dashboard - IO, Disk and HDFS utilization for tuning")
 
 
 
 
