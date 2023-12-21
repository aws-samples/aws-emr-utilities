#!/bin/bash

# Update <my-s3-bucket> to be the bucket in your account that holds these bootstrap actions.
sudo aws s3 cp s3://my-s3-bucket/install_system_metrics.sh /home/hadoop/
sudo chmod 777 /home/hadoop/install_system_metrics.sh
sudo nohup bash /home/hadoop/install_system_metrics.sh &>/dev/null &
