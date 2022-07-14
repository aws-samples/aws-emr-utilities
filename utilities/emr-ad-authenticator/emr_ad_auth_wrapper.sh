#!/bin/bash
sudo aws s3 cp s3://xxxxxxxx/bootstrap-actions/emr-kerberos-ad/emr_ad_auth.sh  .
sudo chmod +x emr_ad_auth.sh
sudo nohup ./emr_ad_auth.sh $1 > /tmp/sssd_BA.log &
