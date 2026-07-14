#!/bin/bash -xe

# This script installs and configures Prometheus on the master nodes to collect node level and application level
# (Hadoop and HBase) metrics from all cluster nodes. It can also configured to export metrics to
# your AWS Prometheus workspace via the remote_write endpoint. AWS Prometheus workspace id is an optional argument,
# that, if passed, configures the on-cluster Prometheus instance to export metrics to AWS Prometheus
# Usage in BA: --bootstrap-actions '[{"Path":"s3://<s3_path>/install_prometheus.sh","Args":["ws-537c7364-f10f-4210-a0fa-deedd3ea1935"]

# Check if required arguments are provided
if [ $# -ne 1 ]; then
    echo "Usage: $0 <WORKSPACE_ID>"
    echo "Example: $0 ws-30c5ec4b-3289-49e4-8cd7-95009d149bf6"
    exit 1
fi

WORKSPACE_ID=$1
AWS_REGION=$(cat /mnt/var/lib/info/extraInstanceData.json | jq -r ".region")

function install_node_exporter() {
    sudo useradd --no-create-home --shell /bin/false node_exporter
    cd /tmp

    instance_arch=`uname -m`
    echo "instance_arch = $instance_arch"

    if [ "$instance_arch" = "aarch64" ]; then
        wget https://github.com/prometheus/node_exporter/releases/download/v1.3.1/node_exporter-1.3.1.linux-arm64.tar.gz
        tar -xvzf node_exporter-1.3.1.linux-arm64.tar.gz
        cd node_exporter-1.3.1.linux-arm64
    else
        wget https://github.com/prometheus/node_exporter/releases/download/v1.3.1/node_exporter-1.3.1.linux-amd64.tar.gz
        tar -xvzf node_exporter-1.3.1.linux-amd64.tar.gz
        cd node_exporter-1.3.1.linux-amd64
    fi
    
    sudo cp node_exporter /usr/local/bin/
    sudo chown node_exporter:node_exporter /usr/local/bin/node_exporter

    cd /tmp
    wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/service_files/node_exporter.service
    sudo cp node_exporter.service /etc/systemd/system/node_exporter.service
    sudo chown node_exporter:node_exporter /etc/systemd/system/node_exporter.service
    sudo systemctl daemon-reload && \
    sudo systemctl start node_exporter && \
    sudo systemctl status node_exporter && \
    sudo systemctl enable node_exporter
}

function install_jmx_exporter() {
    wget https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/0.17.2/jmx_prometheus_javaagent-0.17.2.jar
    sudo mkdir /etc/prometheus
    sudo cp jmx_prometheus_javaagent-0.17.2.jar /etc/prometheus
}

function setup_jmx_hadoop() {
    wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/hdfs_jmx_config_namenode.yaml
    wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/hdfs_jmx_config_datanode.yaml
    wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/yarn_jmx_config_resource_manager.yaml
    wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/yarn_jmx_config_node_manager.yaml

    HADOOP_CONF='/etc/hadoop/conf.empty'
    sudo mkdir -p ${HADOOP_CONF}
    sudo cp hdfs_jmx_config_namenode.yaml ${HADOOP_CONF}
    sudo cp hdfs_jmx_config_datanode.yaml ${HADOOP_CONF}
    sudo cp yarn_jmx_config_resource_manager.yaml ${HADOOP_CONF}
    sudo cp yarn_jmx_config_node_manager.yaml ${HADOOP_CONF}
}

# configure the jmx_exporter for hbase
function setup_jmx_hbase() {
  cat >hbase_jmx_config.yaml <<EOF
lowercaseOutputName: true
lowercaseOutputLabelNames: true
rules:
  - pattern: '.*'
EOF

  # we have to manually load the hbase-env changes to allow the jmx_exporter to push on multiple ports
  cat >hbase-env-master.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7005:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF

  cat >hbase-env-thrift.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7007:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF

  cat >hbase-env-rest.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7008:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF

  cat >hbase-env-regionserver.sh <<"EOF"
export HBASE_OPTS="$HBASE_OPTS -javaagent:/etc/prometheus/jmx_prometheus_javaagent-0.17.2.jar=7006:/etc/hbase/conf/hbase_jmx_config.yaml"
EOF
  sudo mkdir -p /etc/hbase/conf
  sudo cp hbase_jmx_config.yaml /etc/hbase/conf
  sudo cp hbase-env-master.sh /etc/hbase/conf
  sudo cp hbase-env-thrift.sh /etc/hbase/conf
  sudo cp hbase-env-rest.sh /etc/hbase/conf
  sudo cp hbase-env-regionserver.sh /etc/hbase/conf
}

function setup_jmx_spark() {
    wget https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/main/utilities/emr-observability/conf_files/spark_jmx_config.yaml
    sudo mkdir -p /etc/spark/conf
    sudo cp spark_jmx_config.yaml /etc/spark/conf
}

function install_prometheus() {
    sudo useradd --no-create-home --shell /bin/false prometheus
    sudo mkdir -p /etc/prometheus/conf
    sudo chown -R prometheus:prometheus /etc/prometheus
    cd /tmp

    instance_arch=`uname -m`
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
    - region: ${AWS_REGION}
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
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type   

  - job_name: 'hadoop_hdfs_namenode'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s
    ec2_sd_configs:
    - region: ${AWS_REGION}
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
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type    

  - job_name: 'hadoop_hdfs_datanode'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s
    ec2_sd_configs:
    - region: ${AWS_REGION}
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
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hadoop_yarn_resourcemanager'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s
    ec2_sd_configs:
    - region: ${AWS_REGION}
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
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hadoop_yarn_nodemanager'

    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s
    ec2_sd_configs:
    - region: ${AWS_REGION}
      profile: EMR_EC2_DefaultRole
      port: 7005
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - CORE
        - TASK
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type
      
  - job_name: 'telegraf'
    # Override the global default and scrape targets from this job every 15 seconds.
    scrape_interval: 15s
    ec2_sd_configs:
    - region: ${AWS_REGION}
      profile: EMR_EC2_DefaultRole
      port: 9273
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
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

      - job_name: 'hbase_regionserver'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7006
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - CORE
        - TASK
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_hmaster'
    ec2_sd_configs:
    - region: ${REGION}
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
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_rest'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7008
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

  - job_name: 'hbase_thrift'
    ec2_sd_configs:
    - region: ${REGION}
      profile: EMR_EC2_DefaultRole
      port: 7007
      filters:
      - name: tag:aws:elasticmapreduce:job-flow-id
        values:
        - ${JOBFLOWID}
      - name: tag:aws:elasticmapreduce:instance-group-role
        values:
        - MASTER
    relabel_configs:
    - source_labels: [__meta_ec2_instance_id]
      target_label: instance
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
      target_label: cluster_id
    - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
      target_label: node_type

remote_write:
  - url: https://aps-workspaces.${AWS_REGION}.amazonaws.com/workspaces/${WORKSPACE_ID}/api/v1/remote_write
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
    sudo systemctl enable prometheus
    sudo systemctl start prometheus
    sudo systemctl status prometheus
}

# Main execution
IS_MASTER=$(cat /mnt/var/lib/info/instance.json | jq -r ".isMaster" | grep "true" || true);
COMPONENTS=`curl --retry 10 -s localhost:8321/configuration 2>/dev/null | jq '.componentNames'`

cd /tmp

if [ ! -z $IS_MASTER ]; then
    install_prometheus
fi

install_node_exporter
install_jmx_exporter

setup_jmx_hadoop

if echo $COMPONENTS | grep -q hbase; then
  setup_jmx_hbase
fi
