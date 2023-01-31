#!/bin/bash -xe

#install Grafana
cd /tmp
wget https://dl.grafana.com/enterprise/release/grafana-enterprise-9.3.6-1.x86_64.rpm
sudo yum -y install grafana-enterprise-9.3.6-1.x86_64.rpm

wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/grafana/grafana_prometheus_datasource.yaml
sudo mkdir -p /etc/grafana/provisioning/datasources
sudo cp /tmp/grafana_prometheus_datasource.yaml /etc/grafana/provisioning/datasources/prometheus.yaml

wget https://aws-bigdata-blog.s3.amazonaws.com/artifacts/aws-blog-emr-prometheus-grafana/grafana/grafana_dashboard.yaml
sudo mkdir -p /etc/grafana/provisioning/dashboards
sudo cp /tmp/grafana_dashboard.yaml /etc/grafana/provisioning/dashboards/default.yaml

sudo mkdir -p /var/lib/grafana/dashboards
cd /var/lib/grafana/dashboards

sudo chown -R grafana:grafana /var/lib/grafana

#configure Grafana as a service
sudo systemctl daemon-reload
sudo systemctl start grafana-server
sudo systemctl status grafana-server
sudo systemctl enable grafana-server
