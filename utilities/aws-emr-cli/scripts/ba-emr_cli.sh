#!/bin/bash

if [ $(id -u) != "0" ]
then
    sudo "$0" "$@"
    exit $?
fi

[[ -z $1 ]] && echo "you must select a valid s3 location" && exit 2


INSTALL_PATH="/opt/aws-emr-cli"
INSTALL_BUCKET=${1}

mkdir -p $INSTALL_PATH
aws s3 cp "$INSTALL_BUCKET" "/tmp/emr-cli.zip"
unzip /tmp/emr-cli.zip -d $INSTALL_PATH
chmod +x "$INSTALL_PATH/bin/emr"
rm "/tmp/emr-cli.zip"

# Add emr to the app path
cat > /etc/profile.d/emr-cli.sh << EOF
  PATH=\$PATH:$INSTALL_PATH/bin
  source $INSTALL_PATH/scripts/emr-completion.bash
EOF

# Instrall required AWS CLI version 2
cd $INSTALL_PATH
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
./aws/install -i $INSTALL_PATH/aws-cli -b $INSTALL_PATH/bin
rm -f $INSTALL_PATH/awscliv2.zip
rm -rf $INSTALL_PATH/aws

# Install additional requirements
cd /tmp
wget -O epel.rpm –nv https://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm
yum install -y ./epel.rpm && yum -y install xmlstarlet
