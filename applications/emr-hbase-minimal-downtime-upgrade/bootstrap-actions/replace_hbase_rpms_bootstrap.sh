#!/bin/bash

set -ex

bucket=s3://your-bucket/hbase_patch/rpm/
local_repo=/var/aws/emr/packages/bigtop

sudo mkdir -p $local_repo
sudo aws s3 sync $bucket $local_repo

sudo yum install -y createrepo
sudo createrepo --update --workers 8 -o $local_repo $local_repo
sudo yum clean all

sudo bash -c "cat > /etc/yum.repos.d/emr_replace_rpms.repo" <<EOL
[emr_replace_rpms]
name=emr_replace_rpms_repo
baseurl=file:///var/aws/emr/packages/bigtop
enabled=1
gpgcheck=0
priority=1
EOL

for package in $(find $local_repo -name \*.rpm); do
  app=$(basename $package | sed -r 's#([-a-zA-Z]+)-.*#\1#g')
  if sudo yum list installed $app; then
    sudo yum reinstall $app -y --disablerepo "*" --enablerepo "emr_replace_rpms"
  fi
done