#!/bin/bash
#===============================================================================
#!# script: emr_postinstall.sh
#!# authors: ripani
#!# version: v0.1
#!#
#!# Execute an EMR Bootstrap Action only after all the frameworks (e.g. Spark)
#!# have been installed.
#===============================================================================
#?#
#?# usage: ./emr_postinstall.sh <S3_BOOTSTRAP>
#?#        ./emr_postinstall.sh "s3://BUCKET/PREFIX/script.sh"
#?#
#?#   S3_BOOTSTRAP               S3 prefix where the bootstrap action is located
#!#
#===============================================================================
set -x

# Force the script to run as root
if [ $(id -u) != "0" ]; then
    sudo "$0" "$@"
    exit $?
fi

# Print the usage helper using the header as source
function usage() {
    [ "$*" ] && echo "$0: $*"
    sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
    exit -1
}

[[ $# -ne 1 ]] && echo "error: wrong parameters" && usage

S3_PATH="$1"
LOCAL_PATH="/root/`openssl rand -hex 10`.sh"

aws s3 cp $S3_PATH $LOCAL_PATH
chmod 700 $LOCAL_PATH
sed -i "s|null &|null \&\& bash $LOCAL_PATH >> \$STDOUT_LOG 2>> \$STDERR_LOG 0</dev/null \&|" /usr/share/aws/emr/node-provisioner/bin/provision-node
