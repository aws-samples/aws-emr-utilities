#!/bin/bash -xe
set -x
#set up node_exporter for pushing OS level metrics
sudo useradd --no-create-home --shell /bin/false node_exporter
cd /tmp
wget https://github.com/prometheus/node_exporter/releases/download/v1.3.1/node_exporter-1.3.1.linux-amd64.tar.gz
tar -xvzf node_exporter-1.3.1.linux-amd64.tar.gz
cd node_exporter-1.3.1.linux-amd64
sudo cp node_exporter /usr/local/bin/
sudo chown node_exporter:node_exporter /usr/local/bin/node_exporter

cd /tmp
wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/service_files/node_exporter.service
sudo cp node_exporter.service /etc/systemd/system/node_exporter.service
sudo chown node_exporter:node_exporter /etc/systemd/system/node_exporter.service
sudo systemctl daemon-reload && \
sudo systemctl start node_exporter && \
sudo systemctl status node_exporter && \
sudo systemctl enable node_exporter

#set up jmx_exporter for pushing application metrics
wget https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/0.17.0/jmx_prometheus_javaagent-0.17.0.jar
sudo mkdir /etc/prometheus
sudo cp jmx_prometheus_javaagent-0.17.0.jar /etc/prometheus

# configure the jmx_exporter for hadoop
wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/jmx_exporter_yaml/hdfs_jmx_config_namenode.yaml
wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/jmx_exporter_yaml/hdfs_jmx_config_datanode.yaml
wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/jmx_exporter_yaml/yarn_jmx_config_resource_manager.yaml
wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/jmx_exporter_yaml/yarn_jmx_config_node_manager.yaml

HADOOP_CONF='/etc/hadoop/conf.empty'
sudo mkdir -p ${HADOOP_CONF}
sudo cp hdfs_jmx_config_namenode.yaml ${HADOOP_CONF}
sudo cp hdfs_jmx_config_datanode.yaml ${HADOOP_CONF}
sudo cp yarn_jmx_config_resource_manager.yaml ${HADOOP_CONF}
sudo cp yarn_jmx_config_node_manager.yaml ${HADOOP_CONF}

# configure the jmx_exporter for hbase
cat > hbase_jmx_config.yaml <<EOF
lowercaseOutputName: true
lowercaseOutputLabelNames: true
rules:
  - pattern: '.*'
EOF
# we have to manually load the hbase-env changes to allow the jmx_exporter to push on multiple ports 
cat > hbase-env-master.sh <<EOF
export HBASE_OPTS="$HBASE_OPTS -XX:+UseConcMarkSweepGC -XX:+UseParNewGC -XX:CMSInitiatingOccupancyFraction=70 -Dnetworkaddress.cache.ttl=5 -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.13.0.jar=7004:/etc/hbase/conf/hbase_jmx_config.yaml -Dcom.sun.management.jmxremote -Dcom.sun.management.jmxremote.ssl=false -Dcom.sun.management.jmxremote.authenticate=false -Dcom.sun.management.jmxremote.port=50108"
EOF
cat > hbase-env-thrift.sh <<EOF
export HBASE_OPTS="$HBASE_OPTS -XX:+UseConcMarkSweepGC -XX:+UseParNewGC -XX:CMSInitiatingOccupancyFraction=70 -Dnetworkaddress.cache.ttl=5 -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.13.0.jar=7007:/etc/hbase/conf/hbase_jmx_config.yaml -Dcom.sun.management.jmxremote -Dcom.sun.management.jmxremote.ssl=false -Dcom.sun.management.jmxremote.authenticate=false -Dcom.sun.management.jmxremote.port=50110"
EOF
cat > hbase-env-rest.sh <<EOF
export HBASE_OPTS="$HBASE_OPTS -XX:+UseConcMarkSweepGC -XX:+UseParNewGC -XX:CMSInitiatingOccupancyFraction=70 -Dnetworkaddress.cache.ttl=5 -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.13.0.jar=7006:/etc/hbase/conf/hbase_jmx_config.yaml -Dcom.sun.management.jmxremote -Dcom.sun.management.jmxremote.ssl=false -Dcom.sun.management.jmxremote.authenticate=false -Dcom.sun.management.jmxremote.port=50109"
EOF
cat > hbase-env-regionserver.sh <<EOF
export HBASE_OPTS="$HBASE_OPTS -XX:+UseConcMarkSweepGC -XX:+UseParNewGC -XX:CMSInitiatingOccupancyFraction=70 -Dnetworkaddress.cache.ttl=5 -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.13.0.jar=7004:/etc/hbase/conf/hbase_jmx_config.yaml -Dcom.sun.management.jmxremote -Dcom.sun.management.jmxremote.ssl=false -Dcom.sun.management.jmxremote.authenticate=false -Dcom.sun.management.jmxremote.port=50108"
EOF
sudo mkdir -p /etc/hbase/conf
sudo cp hbase_jmx_config.yaml /etc/hbase/conf
sudo cp hbase-env-master.sh /etc/hbase/conf
sudo cp hbase-env-thrift.sh /etc/hbase/conf
sudo cp hbase-env-rest.sh /etc/hbase/conf
sudo cp hbase-env-regionserver.sh /etc/hbase/conf

# configure the jmx_exporter for spark
wget https://github.com/aws-samples/aws-emr-utilities/blob/main/utilities/emr-observability/conf_files/spark_jmx_config.yaml
sudo cp spark_jmx_config.yaml /etc/spark/conf

# on the master node, install and configure prometheus
IS_MASTER=$(cat /mnt/var/lib/info/instance.json | jq -r ".isMaster" | grep "true" || true);
if [ ! -z $IS_MASTER ]; then
#install Prometheus
sudo useradd --no-create-home --shell /bin/false prometheus
sudo mkdir -p /etc/prometheus/conf
sudo chown -R prometheus:prometheus /etc/prometheus
cd /tmp
wget https://github.com/prometheus/prometheus/releases/download/v2.37.0/prometheus-2.37.0.linux-amd64.tar.gz
tar xvf prometheus-2.37.0.linux-amd64.tar.gz
cd prometheus-2.37.0.linux-amd64
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

remote_write:
  -
    url: https://aps-workspaces.${AWS_REGION}.amazonaws.com/workspaces/${WORKSPACE_ID}/api/v1/remote_write
    queue_config:
        max_samples_per_send: 1000
        max_shards: 200
        capacity: 2500
    sigv4:
         region: ${AWS_REGION}
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
