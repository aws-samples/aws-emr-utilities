## EMR Observability

EMR observability is a bootstrap action script which installs prometheus on the master node of EMR cluster. This prometheus server will scrape the metrics and the scraped metrics is exported to your Amazon Managed Prometheus workspace via the remote_write endpoint.

## Why EMR Observability

To have a single globalized view of metrics across all the EMR clusters in the account
To retain the metrics even after the cluster is terminated
To monitor work load and infrastructure in real time.

With the current available solution, there are some limitations. For example,

*EMR Ganglia* 
The metrics are gone once the cluster is terminated.
Metrics can be viewed only per cluster and it becomes difficult to monitor when customers launch many clusters per day.

*Services used*

*AMP* - Amazon Managed Service for Prometheus is a Prometheus-compatible monitoring and alerting service that makes it easy to monitor containerized applications and infrastructure at scale. AMP has workspace which stores all the prometheus data.
*AMG* - Amazon Managed Grafana is a fully managed service for open source Grafana developed in collaboration with Grafana Labs. Grafana is a popular open source analytics platform that enables you to query, visualize, alert on and understand your metrics no matter where they are stored.

# Note

Currently AMP stores data for 150 days which is fixed. There is plans to support custom retention - https://github.com/aws/amazon-managed-service-for-prometheus-roadmap/issues/2


## How to use

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
3)While launching the EMR clusters
Use the script as the bootstrap action
```
--bootstrap-actions '[{"Path":"s3://bucket-name/path/install_prometheus.sh","Name":"Install Prometheus"}]'
```
Use the below EMR configuration classification json 
```
./conf_files/configuration.json
```
4)You can now start visualizing the metrics in AMG
