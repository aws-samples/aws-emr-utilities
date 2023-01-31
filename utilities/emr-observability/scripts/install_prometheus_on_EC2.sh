#!/bin/bash -xe
set -x
#install Prometheus
sudo useradd --no-create-home --shell /bin/false prometheus
sudo mkdir -p /etc/prometheus/conf
sudo chown -R prometheus:prometheus /etc/prometheus
cd /tmp

if [ "$instance_arch" = "aarch64" ]; then
    wget https://github.com/prometheus/prometheus/releases/download/v2.38.0/prometheus-2.38.0.linux-arm64.tar.gz
    tar -xvzf prometheus-2.38.0.linux-arm64.tar.gz
    cd prometheus-2.38.0.linux-arm64
else
    wget https://github.com/prometheus/prometheus/releases/download/v2.38.0/prometheus-2.38.0.linux-amd64.tar.gz
    tar -xvzf prometheus-2.38.0.linux-amd64.tar.gz
    cd prometheus-2.38.0.linux-amd64
fi

sudo cp prometheus /usr/local/bin/
sudo cp promtool /usr/local/bin/
sudo cp -r consoles "/etc/prometheus"
sudo cp -r console_libraries "/etc/prometheus"
sudo chown prometheus:prometheus /usr/local/bin/prometheus
sudo chown prometheus:prometheus /usr/local/bin/promtool
sudo chown -R prometheus:prometheus /etc/prometheus/consoles
sudo chown -R prometheus:prometheus /etc/prometheus/console_libraries
sudo mkdir -p /etc/prometheus/conf/
JOBFLOWID=$(grep jobFlowId /emr/instance-controller/lib/info/job-flow-state.txt | cut -d\" -f2)

cat > prometheus.yml <<EOF
global:
  # How frequently to scrape targets
  scrape_interval:     15s # By default, scrape targets every 15 seconds.

  # How frequently to evaluate rules
  evaluation_interval: 5s

  # Attach these labels to any time series or alerts when communicating with
  # external systems (federation, remote storage, Alertmanager).
  external_labels:
    monitor: 'emr'

# A scrape configuration containing exactly one endpoint to scrape:
# Here it's Prometheus itself.
scrape_configs:
  # The job name is added as a label `job=<job_name>` to any timeseries scraped from this config.
  - job_name: 'hadoop'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 9100
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}

    relabel_configs:
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

  - job_name: 'hadoop_hdfs_namenode'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 7001
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER

    relabel_configs:
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

  - job_name: 'hadoop_hdfs_datanode'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 7001
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - CORE

    relabel_configs:
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

  - job_name: 'hadoop_yarn_resourcemanager'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 7005
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER

    relabel_configs:
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

  - job_name: 'hadoop_yarn_nodemanager'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 7005
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
    relabel_configs:
      #This job is for monitoring CORE and TASK nodes, so drop MASTER node.
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      regex: MASTER
      action: drop
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

  - job_name: 'spark_metrics_driver'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 7006
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}

    relabel_configs:
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

  - job_name: 'spark_metrics_executor'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s

    ec2_sd_configs:
    - region: us-east-1
      profile: EMR_EC2_DefaultRole
      port: 7007
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}

    relabel_configs:
      #Use instance ID as the instance label instead of private ip:port
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id

#remote_write:
#  -
#    url: https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-8865d501-f1ec-4d87-821f-3d434e2f3c12/api/v1/remote_write
#    queue_config:
#        max_samples_per_send: 1000
#        max_shards: 200
#        capacity: 2500
#    sigv4:
#         region: us-east-1
EOF
sudo cp prometheus.yml /etc/prometheus/conf
sudo chown -R prometheus:prometheus /etc/prometheus/conf

cat > prometheus.service <<EOF
[Unit]
Description=Prometheus
Wants=network-online.target
After=network-online.target

[Service]
User=prometheus
Group=prometheus
Type=simple
ExecStart=/usr/local/bin/prometheus --config.file=/etc/prometheus/conf/prometheus.yml --storage.tsdb.path=/var/lib/prometheus/ --web.console.templates=/etc/prometheus/consoles --web.console.libraries=/etc/prometheus/console_libraries

Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo cp prometheus.service /etc/systemd/system/prometheus.service
sudo chown prometheus:prometheus /etc/systemd/system/prometheus.service
sudo mkdir -p /var/lib/prometheus
sudo chown -R prometheus:prometheus /var/lib/prometheus
sudo systemctl daemon-reload
sudo systemctl start prometheus
sudo systemctl status prometheus
sudo systemctl enable prometheus
fi
