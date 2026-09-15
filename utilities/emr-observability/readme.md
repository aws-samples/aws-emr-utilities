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
* Spark Application metrics

**Other AWS Services used for option 2**

*AMP* - Amazon Managed Service for Prometheus is a Prometheus-compatible monitoring and alerting service that makes it easy to monitor containerized applications and infrastructure at scale. AMP has workspace which stores all the prometheus data.
Refer this doc for AMP pricing - https://aws.amazon.com/prometheus/pricing/

*AMG* - Amazon Managed Grafana is a fully managed service for open source Grafana developed in collaboration with Grafana Labs. Grafana is a popular open source analytics platform that enables you to query, visualize, alert on and understand your metrics no matter where they are stored.
Refer this doc for AMG pricing - https://aws.amazon.com/grafana/pricing/

## Choosing a deployment

There are two independent choices to make.

**Where metrics are stored.** Option 1 runs Prometheus and Grafana on your own EC2 instance. Option 2 uses Amazon Managed Prometheus and Amazon Managed Grafana. The same bootstrap scripts serve both; Option 2 additionally passes the AMP workspace ID as an argument so the on-cluster Prometheus remote-writes to it.

**Which workload you are monitoring.** This decides the bootstrap script *and* the EMR configuration classification. They are paired and must not be mixed:

| Workload | Bootstrap action | EMR configuration |
|---|---|---|
| Hadoop + HBase | `install_prometheus_v2.sh` | `conf_files/configuration.json` |
| Hadoop + Spark | `install-telegraf-bootstrap.sh` and `spark-install_prometheus.sh` | `conf_files/emr-application-configuration.json` |

Both cover node/OS, YARN and HDFS metrics, so those dashboards work either way. Neither covers HBase *and* Spark.

Two things to check before launching:

* **JMX agent jar path.** The two configuration files point `-javaagent` at different paths (`/usr/lib/prometheus` and `/etc/prometheus`) to match where each script installs the jar. Using the wrong pairing prevents the NameNode and ResourceManager from starting, because the JVM cannot load a `-javaagent` that is not there.
* **Exporter ports.** The `port:` values in the script's `scrape_configs` must match the ports set by the `-javaagent` flags in your configuration file. `install_prometheus_v2.sh` and `conf_files/configuration.json` currently specify different YARN and HDFS ports; align one to the other before deploying, or those jobs will scrape ports nothing is listening on.

### Run HBase and Spark on separate clusters

This utility instruments HBase or Spark, not both, so a mixed cluster always leaves one of them uninstrumented.

That limitation lines up with the broader EMR guidance: prefer not to co-locate HBase with YARN workloads such as Spark or Hive. HBase RegionServers are long-running daemons outside YARN's control, so they compete with the NodeManager for the same memory and CPU. You have to hold back `yarn.nodemanager.resource.memory-mb` to leave room for the HBase heap and block cache, and Spark shuffle contends with HBase for page cache and disk. Managed scaling and autoscaling compound it, since decommissioning a core or task node running a RegionServer causes region churn.

Prefer a dedicated HBase cluster, using HBase on S3 if you want to separate storage from compute, and separate clusters for YARN applications. If you do co-locate, expect to instrument only one of the two, and cap YARN resources explicitly so the RegionServers are not starved.

# Instructions
### Option - 1
#### How to use
1)Install Prometheus and Grafana on EC2 instance or a single node EMR cluster using the below scripts preferably in same VPC to simplify network access - Network access to inbound TCP ports 22 (SSH), Grafana (3000) and Prometheus UI (9090) needs to be opened
*scripts/install_prometheus_on_EC2.sh*
*scripts/install_grafana_on_EC2.sh*

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

1) Create workspace in AMP - https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-onboard-create-workspace.html (You can igore this if you already have a workspace in AMP)

2) Get 'Workspace ID' from AMP console. 

3) Add policy "AmazonPrometheusRemoteWriteAccess" to EC2 instance profile(Service role for EMR cluster EC2 instances) to provide permission to remote write metrics into all Amazon Managed Service for Prometheus workspaces in the account

4) When initiating the launch of EMR clusters, 
   1) Ensure metric export setup by using the provided bootstrap action script "install_prometheus_v2.sh" and adding the AWS Prometheus workspace ID as an argument.
        ```
        --bootstrap-actions '[{"Path":"s3://<s3_path>/install_prometheus_v2.sh","Args":["ws-537c7364-f10f-4210-a0fa-deedd3ea1935"]}]'
        ```
    1) Use the below EMR configuration classification json
        ```
        ./conf_files/configuration.json
        ```
        This is the configuration that pairs with `install_prometheus_v2.sh` - it points `-javaagent` at `/usr/lib/prometheus`, where that script installs the jar. For Spark metrics use `spark-install_prometheus.sh` with `./conf_files/emr-application-configuration.json` instead; see [Choosing a deployment](#choosing-a-deployment).
5) You can now start visualizing the metrics in AMG

### Setting up Grafana Dashboards

After importing, select your Prometheus or Amazon Managed Service for Prometheus source from the **Datasource** dropdown at the top of the dashboard. Every panel binds to this selection, so no JSON editing is required. Grafana remembers the choice per user.

##### Note for Amazon Managed Grafana 12 and later

Starting in AMG 12, SigV4 authentication was removed from the core Prometheus plugin, and AMP data sources are migrated to the dedicated Amazon Managed Service for Prometheus plugin (`grafana-amazonprometheus-datasource`). Refer to [Use AWS data source configuration to add Amazon Managed Service for Prometheus as a data source](https://docs.aws.amazon.com/grafana/latest/userguide/AMP-adding-AWS-config.html).

The **Datasource** dropdown filters by plugin type, so after that migration an AMP data source will not be listed. Point the variable at the AMP plugin once per dashboard:

*Dashboard settings → Variables → `DS_PROMETHEUS` → Type: Amazon Managed Service for Prometheus*

Or set it in the JSON before importing:

```json
{ "name": "DS_PROMETHEUS", "type": "datasource", "query": "grafana-amazonprometheus-datasource" }
```

This applies only to AMP data sources on AMG 12+. A self-managed Prometheus data source (Option 1) is unaffected and needs no change on any version.

#### Yarn and OS level metrics Dashboards
Import the dashboards from `grafana_dashboards/`:
* `EMRonEC2-Optimization-dashboard.json`
* `OS_Level_Metrics.json`
* `YARN-ResourceManager.json`
* `YARN-NodeManager.json`
* `HDFS-NameNode.json`

#### Hbase
Use this ID "12243" for importing the dashboard into Grafana

#### Spark
Import the Spark-specific dashboards from `grafana_dashboards/`:
* `Spark-Application-Monitoring.json` - Spark application-level monitoring
* `Spark-Metrics-by-Components.json` - Spark metrics broken down by component

For Spark-specific Prometheus setup, use `spark-install_prometheus.sh` as the bootstrap action instead of `install_prometheus_v2.sh` & `install-telegraf-bootstrap.sh` for Telegraf-based metric collection.

## Work in progress
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
![Alt text](images/Spark-Application-Monitoring.png?raw=true "Spark Application Dashboard")
![Alt text](images/Spark-Metrics-By-Components.png?raw=true "Spark Components Dashboard")

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

### Note
1)Prometheus runs on port 9091 in this utility. For example: http://ec2-0-00-000-00.compute-1.amazonaws.com:9091/
