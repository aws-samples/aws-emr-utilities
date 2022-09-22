#!/bin/bash
#==============================================================================
#!# emr-ba-prometheus_exporter.sh - Setup JMX exporter to prometheus and
#!# prometheus node exporter to collect additional node metrics. The script
#!# should be attached as EMR Bootstrap Action while launching the EMR cluster.
#!#
#!#  version         1.2
#!#  author          ripani
#!#  license         MIT license
#!#
#==============================================================================
#?#
#?# usage: ./emr-ba-prometheus_exporter.sh
#?#
#==============================================================================

if [ $(id -u) != "0" ]; then
    sudo "$0" "$@"
    exit $?
fi

PROM_VERSION="0.15.0"
NODE_EXPORTER_VERSION="1.3.1"
INSTALL_PATH="/opt"

# Install Prometheus Java agent
mkdir -p $INSTALL_PATH/prometheus && cd $INSTALL_PATH/prometheus
wget https://repo1.maven.org/maven2/io/prometheus/jmx/jmx_prometheus_javaagent/$PROM_VERSION/jmx_prometheus_javaagent-${PROM_VERSION}.jar -O $INSTALL_PATH/prometheus/prometheus_javaagent.jar

# HBase JMX configuration parser
cat << 'EOF' > $INSTALL_PATH/prometheus/hbase.yml
lowercaseOutputName: true
lowercaseOutputLabelNames: true
rules:
- pattern: Hadoop<service=HBase, name=RegionServer, sub=Regions><>Namespace_([^\W_]+)_table_([^\W_]+)_region_([^\W_]+)_metric_(\w+)
  name: HBase_metric_$4
  labels:
    namespace: "$1"
    table: "$2"
    region: "$3"
- pattern: Hadoop<service=(\w+), name=(\w+), sub=(\w+)><>([\w._]+)
  name: hadoop_$1_$4
  labels:
    "name": "$2"
    "sub": "$3"
- pattern: .+
EOF

chmod 655 -R $INSTALL_PATH/prometheus

## Install prometheus node exporter
cd $INSTALL_PATH
wget https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-386.tar.gz
tar xf node_exporter-${NODE_EXPORTER_VERSION}.linux-386.tar.gz
rm node_exporter-${NODE_EXPORTER_VERSION}.linux-386.tar.gz
mv node_exporter-${NODE_EXPORTER_VERSION}.linux-386 node_exporter && cd node_exporter && mkdir run

# create node exporter user
useradd -m node_exporter
groupadd node_exporter
usermod -a -G node_exporter node_exporter
chown node_exporter:node_exporter $INSTALL_PATH/node_exporter

# create service definition
cat << EOF > /etc/systemd/system/node_exporter.service
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=$INSTALL_PATH/node_exporter/node_exporter
Restart=always
PIDFile=$INSTALL_PATH/node_exporter/run/node_exporter.pid

[Install]
WantedBy=multi-user.target
EOF

# enable and start node exporter
systemctl enable node_exporter
systemctl start node_exporter
systemctl status node_exporter
