#!/bin/bash
#==============================================================================
#!# setup-artifact-bucket.sh - Setup the required templates and scripts
#!# in a private S3 bucket.
#!#
#!#  version         1.1
#!#  author          ripani
#!#  license         MIT license
#!#
#==============================================================================
#?#
#?# usage: ./setup-artifact-bucket.sh <REGIONAL_S3_BUCKET>
#?#        ./setup-artifact-bucket.sh mybucketname
#?#
#?#  REGIONAL_S3_BUCKET            S3 bucket name eg: mybucketname
#?#
#==============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

function usage() {
    [ "$*" ] && echo "$0: $*"
    sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
    exit -1
}

[[ $# -ne 1 ]] && echo "error: missing parameters" && usage

REGIONAL_S3_BUCKET="$1"
S3_PREFIX="artifacts/aws-emr-hbase-monitoring"

# clean and upload
aws s3 rm --recursive s3://${REGIONAL_S3_BUCKET}/${S3_PREFIX}/
aws s3 sync $DIR s3://${REGIONAL_S3_BUCKET}/${S3_PREFIX}/ --exclude '*.git*'
