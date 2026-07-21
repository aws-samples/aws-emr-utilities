#!/bin/bash

# EMR Bootstrap Action to Install and Configure Telegraf
# For EMR-7.10.0 (Amazon Linux 2023)
# Usage: ./install-telegraf-bootstrap.sh

set -e

# Detect architecture
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    TELEGRAF_RPM="telegraf-1.28.5-1.x86_64.rpm"
elif [ "$ARCH" = "aarch64" ]; then
    TELEGRAF_RPM="telegraf-1.28.5-1.aarch64.rpm"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

# Download and install Telegraf
echo "Installing Telegraf for $ARCH architecture..."
wget -O /tmp/$TELEGRAF_RPM https://dl.influxdata.com/telegraf/releases/$TELEGRAF_RPM
sudo rpm -ivh /tmp/$TELEGRAF_RPM

# Create Telegraf configuration
echo "Creating Telegraf configuration..."
sudo tee /etc/telegraf/telegraf.conf > /dev/null <<'EOF'
# Telegraf Configuration for Amazon Managed Prometheus

[agent]
  interval = "10s"
  round_interval = true
  metric_batch_size = 1000
  metric_buffer_limit = 10000
  collection_jitter = "0s"
  flush_interval = "10s"
  flush_jitter = "0s"
  precision = "0s"

# Prometheus client output
[[outputs.prometheus_client]]
  listen = "0.0.0.0:9273"
  path = "/metrics"

# CPU metrics
[[inputs.cpu]]
  percpu = true
  totalcpu = true
  collect_cpu_time = false
  report_active = false
  core_tags = false

# Memory metrics
[[inputs.mem]]

# Socket listener for Spark metrics
[[inputs.socket_listener]]
  service_address = "tcp://:2003"
  data_format = "graphite"
  separator = "_"
  templates = [
   "my.*.*.*.* .prefix.prefix.prefix.app_name.node.measurement*",
   "my.*.*.*.*.driver.* .prefix.prefix.prefix.app_name.node.measurement*",
   "my.*.*.*.*.driver.*.*.* .prefix.prefix.prefix.app_name.node.measurement.measurement.type_instance",
   "my.*.*.*.*.driver.*.*.*.* .prefix.prefix.prefix.app_name.node.measurement.measurement.measurement.type_instance",
   "my.*.*.*.*.driver.*.StreamingMetrics.streaming.* .prefix.prefix.prefix.app_name.node..measurement.measurement.type_instance"
  ]
EOF

sudo chown telegraf:telegraf /etc/telegraf/telegraf.conf
sudo chmod 644 /etc/telegraf/telegraf.conf

# Enable and start Telegraf service
echo "Starting Telegraf service..."
sudo systemctl enable telegraf
sudo systemctl start telegraf

# Verify installation
echo "Verifying Telegraf installation..."
telegraf --version
sudo systemctl status telegraf --no-pager

echo "Telegraf installation and configuration completed successfully!"
