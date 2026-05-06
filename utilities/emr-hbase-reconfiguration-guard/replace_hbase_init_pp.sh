#!/bin/bash
# Author: Suthan Phillips
# Bootstrap action to replace HBase init.pp file to prevent hbase service restarts on re-configuration
# Date: 03/19/2025
# Version 2 - Updated for EMR 7.12+

# Create backup of original file
sudo cp /var/aws/emr/bigtop-deploy/puppet/modules/hadoop_hbase/manifests/init.pp /var/aws/emr/bigtop-deploy/puppet/modules/hadoop_hbase/manifests/init.pp.bak

# Download the modified init.pp from S3
# IMPORTANT: Modify the S3 path below to point to your S3 bucket location containing the init.pp file
sudo aws s3 cp s3://YOUR-BUCKET/stop_hbase_restart/init.pp /var/aws/emr/bigtop-deploy/puppet/modules/hadoop_hbase/manifests/init.pp

# Set correct permissions and ownership
sudo chmod 644 /var/aws/emr/bigtop-deploy/puppet/modules/hadoop_hbase/manifests/init.pp
sudo chown root:root /var/aws/emr/bigtop-deploy/puppet/modules/hadoop_hbase/manifests/init.pp
