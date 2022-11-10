#!/bin/bash
set -x -e

REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone | sed 's/\(.*\)[a-z]/\1/')
echo "$REGION" >/tmp/aws-region

cd /home/hadoop
aws s3 cp s3://<BUCKET_NAME_CHANGE_ME>/trino_cloudwatch.sh .
chmod +x trino_cloudwatch.sh

echo '* * * * * sudo /bin/bash -l -c "/home/hadoop/trino_cloudwatch.sh; sleep 30; /home/hadoop/trino_cloudwatch.sh"' >crontab.txt

crontab crontab.txt
