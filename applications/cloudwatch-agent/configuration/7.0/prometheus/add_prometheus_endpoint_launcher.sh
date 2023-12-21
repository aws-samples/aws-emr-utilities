#!/bin/bash

# Update <my-s3-bucket> to be the bucket in your account that holds these bootstrap actions.
aws s3 cp s3://my-s3-bucket/add_prometheus_endpoint.sh .
chmod +x add_prometheus_endpoint.sh
nohup ./add_prometheus_endpoint.sh $1 &>/dev/null &
