#!/bin/bash
#==============================================================================
#!# emr-step-monitoring_apps.sh - Setup local prometheus and grafana services
#!# using official docker images to monitor an HBase EMR cluster.
#!#
#!#  version         1.2
#!#  author          ripani
#!#  license         MIT license
#!#
#==============================================================================
#?#
#?# usage: ./emr-step-monitoring_apps.sh <ARTIFACTS> <EMR_TAG_NAME>
#?#        ./emr-step-monitoring_apps.sh s3://ripani.dub/artifacts/aws-emr-hbase-monitoring aws-hbase/emr-node
#?#
#?#   ARTIFACTS                  S3 path of the artifacts folder
#?#   EMR_TAG_NAME               AWS Tag Name value for prometheus scraping
#?#
#==============================================================================

# Force the script to run as root
if [ $(id -u) != "0" ]; then
    sudo "$0" "$@"
    exit $?
fi

# Print the usage helper using the header as source
function usage() {
    [ "$*" ] && echo "$0: $*"
    sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
    exit -1
}

[[ $# -ne 2 ]] && echo "error: wrong parameters" && usage

set -x

ARTIFACTS="$1"
EMR_TAG_NAME="$2"

GRAFANA_VER="7.4.0"
PROMETHEUS_VER="v2.24.0"
PUBLIC_HOSTNAME=`curl http://169.254.169.254/latest/meta-data/public-hostname 2>/dev/null`
LOCAL_HOSTNAME=`curl http://169.254.169.254/latest/meta-data/local-hostname 2>/dev/null`

yum install -y docker
systemctl start docker

docker run --name grafana -d -i -p 3000:3000 grafana/grafana:$GRAFANA_VER
docker run --name prometheus -d -p 3001:9090 prom/prometheus:$PROMETHEUS_VER

# configure prometheus targets
cat << EOF > /tmp/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:

  - job_name: 'hbase'
    ec2_sd_configs:
      - port: 9100
        filters:
          - name: tag:Name
            values:
              - $EMR_TAG_NAME
      - port: 7000
        filters:
          - name: tag:Name
            values:
              - $EMR_TAG_NAME
    relabel_configs:
      - source_labels: [__meta_ec2_ami]
        target_label: ami
      - source_labels: [__meta_ec2_architecture]
        target_label: arch
      - source_labels: [__meta_ec2_availability_zone]
        target_label: availability_zone
      - source_labels: [__meta_ec2_instance_id]
        target_label: instance
      - source_labels: [__meta_ec2_instance_state]
        target_label: instance_state
      - source_labels: [__meta_ec2_instance_type]
        target_label: instance_type
      - source_labels: [__meta_ec2_private_dns_name]
        target_label: instance_private_dns_name
      - source_labels: [__meta_ec2_private_ip]
        target_label: instance_private_ip
      - source_labels: [__meta_ec2_vpc_id]
        target_label: instance_vpc
      - source_labels: [__meta_ec2_subnet_id]
        target_label: instance_subnet
      - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_instance_group_role]
        target_label: type
      - source_labels: [__meta_ec2_tag_aws_elasticmapreduce_job_flow_id]
        target_label: cluster
EOF

docker cp /tmp/prometheus.yml prometheus:/etc/prometheus/prometheus.yml
docker stop prometheus && docker start prometheus

sleep 5

# download grafana dashboard
aws s3 cp $ARTIFACTS/resources/grafana-hbase.json /tmp/
# create prometheus datasource in grafana
curl 'http://admin:admin@localhost:3000/api/datasources' -X POST -H 'Content-Type: application/json;charset=UTF-8' --data-binary "{\"name\":\"Prometheus\",\"type\":\"prometheus\",\"url\":\"http://$LOCAL_HOSTNAME:3001\",\"access\":\"direct\",\"isDefault\":true}"
# upload hbase dashboard for grafana
curl 'http://admin:admin@localhost:3000/api/dashboards/db' -X POST -H 'Content-Type: application/json;charset=UTF-8' -d @/tmp/grafana-hbase.json
# clean up
rm /tmp/grafana-hbase.json && rm /tmp/prometheus.yml

exit 0
