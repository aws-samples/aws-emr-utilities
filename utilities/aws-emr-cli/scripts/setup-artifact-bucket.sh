#!/bin/bash
#==============================================================================
#!# setup-artifact-bucket.sh - Script to setup a private S3 bucket
#!#
#!#  version         1.1
#!#  author          ripani
#!#  license         MIT license
#!#
#===============================================================================
#?#
#?# usage: ./setup-artifact-bucket.sh <S3_BUCKET>
#?#        ./setup-artifact-bucket.sh mytestbucket
#?#
#?#  S3_BUCKET            S3 bucket name eg: mytestbucket
#?#
#===============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

function usage() {
  [ "$*" ] && echo "$0: $*"
  sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
  exit -1
}

[[ $# -ne 1 ]] && echo "error: missing parameters" && usage

S3_BUCKET="$1"
PREFIX_RELEASE="artifacts/aws-emr-cli"
PREFIX_DEVELOP="artifacts/aws-emr-cli-dev"
BUILD_PATH="/tmp/build"

source $DIR/../functions/config.sh

#===============================================================================
# Prepare build
#===============================================================================
mkdir -p $BUILD_PATH
rsync -av --progress $DIR/../ $BUILD_PATH --exclude .git

# clean old bucket prefixes
aws s3 rm "s3://$S3_BUCKET/$PREFIX_RELEASE" --recursive
aws s3 rm "s3://$S3_BUCKET/$PREFIX_DEVELOP" --recursive

pushd $BUILD_PATH
# prepare config files for updates
echo "UPDATE_BUCKET=\"s3://$S3_BUCKET/$PREFIX_DEVELOP/\"" >> ./functions/config.sh
aws s3 sync "./" "s3://$S3_BUCKET/$PREFIX_DEVELOP" --exclude "*.idea*" --exclude "*.git*" --exclude "*.iml" --exclude "*/target/*"
# create distribution package
zip -r /tmp/emr-cli-${VERSION}.zip ./ -x '*.git*' -x '*.idea*' -x '*.iml*' -x '*emr-ba*' -x '*emr-publish*'
popd
aws s3 cp $DIR/ba-emr_cli.sh "s3://$S3_BUCKET/$PREFIX_RELEASE/"
aws s3 cp /tmp/emr-cli-${VERSION}.zip "s3://$S3_BUCKET/$PREFIX_RELEASE/"
rm /tmp/emr-cli-${VERSION}.zip
rm -rf $BUILD_PATH
exit 0
