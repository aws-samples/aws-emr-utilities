#!/bin/bash

sudo aws s3 cp s3://suthan-emr-bda/scripts/new-2/MSdampener.sh /home/hadoop/

while true; do
  sudo sh /home/hadoop/MSdampener.sh InstanceFleet 400 400 60 >> /home/hadoop/MSdampener_log.txt 2>&1
  sleep 240
done &
