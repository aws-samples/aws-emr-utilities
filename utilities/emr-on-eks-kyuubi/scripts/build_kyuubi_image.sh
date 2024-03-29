#!/usr/bin/env bash
#===============================================================================
#!# build_kyuubi_image.sh - Build a custom EMR image to use Kyuubi on K8s.
#!# The script create a docker image using the EMR on EKS base image adding
#!# additional datasources (Delta, Mysql JDBC) and configures the Kyuubi AuthZ
#!# plugin for Ranger.
#!#
#!#  version         1.0
#!#  author          ripani
#!#
#===============================================================================
#?#
#?# usage: ./build_kyuubi_image.sh <YOUR_ACCOUNT_ID> <YOUR_REGION> <EMR_ACCOUNT_ID> <EMR_RELEASE>
#?#        ./build_kyuubi_image.sh $AWS_ACCOUNT eu-west-1 483788554619 emr-6.6.0
#?#
#?#   YOUR_ACCOUNT_ID            AWS Account ID where the docker image will be created
#?#   YOUR_REGION                AWS Region used to retrieve the EMR Base Image and create a local ECR repo for the custom image
#?#   EMR_ACCOUNT_ID             EMR Account ID to retrieve the base image
#?#                              https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/docker-custom-images-tag.html
#?#   EMR_RELEASE                EMR base release version
#?#
#===============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

function usage() {
  [ "$*" ] && echo "$0: $*"
  sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
  exit -1
}

[[ $# -ne 4 ]] && echo "error: wrong parameters" && usage

#===============================================================================
# Configurations
#===============================================================================

## AWS Account used for local ECR Repository
ACCOUNT_ID="$1"
REGION="$2"

## EMR on EKS Base Image
EMR_ACCOUNT="$3"
EMR_RELEASE="$4"
EMR_IMAGE="$EMR_ACCOUNT.dkr.ecr.$REGION.amazonaws.com/spark/$EMR_RELEASE:latest"

## Additional configs
KYUUBI_PATH="/usr/lib/kyuubi"
RANGER_VERSION="2.2.0"
DELTA_VERSION="1.2.1"
DELTA_PATH="/usr/lib/delta/"

### download EMR image
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $EMR_ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker pull $EMR_IMAGE

## Build directories
mkdir -p $DIR/build && mkdir -p $DIR/tmp

#===============================================================================
# Kyuubi build - distro, authZ plugin and kyuubi JDBC drivers
#===============================================================================
cd $DIR/tmp
git clone https://github.com/apache/incubator-kyuubi.git
cd $DIR/tmp/incubator-kyuubi/

KYUUBI_RELEASE=`mvn help:evaluate -Dexpression=project.version -q -DforceStdout`

./build/dist --tgz --spark-provided
mvn package -pl :kyuubi-spark-authz_2.12 -DskipTests
mvn package -pl :kyuubi-hive-jdbc -DskipTests

cp apache-kyuubi-$KYUUBI_RELEASE-bin.tgz $DIR/build/
cp extensions/spark/kyuubi-spark-authz/target/kyuubi-spark-authz_2.12-$KYUUBI_RELEASE.jar $DIR/build/

#===============================================================================
# Ranger build - dependencies for authZ plugin only
#===============================================================================
cd $DIR/tmp
git clone --depth 1 -b "release-ranger-$RANGER_VERSION" https://github.com/apache/ranger.git
cd ranger
mvn clean package dependency:copy-dependencies -pl :ranger-hdfs-plugin -am -DskipTests

# dependencies
cp agents-common/target/ranger-plugins-common-$RANGER_VERSION.jar $DIR/build/
cp agents-audit/target/ranger-plugins-audit-$RANGER_VERSION.jar $DIR/build/
cp agents-common/target/dependency/gethostname4j-*.jar $DIR/build/
cp agents-common/target/dependency/jna-*.jar $DIR/build/
cp agents-common/target/dependency/jackson-jaxrs-*.jar $DIR/build/
cp agents-common/target/dependency/jersey-bundle-*.jar $DIR/build/


#===============================================================================
# Dockerfile - build
#===============================================================================
cd $DIR/build/

cat << EOF > $DIR/build/Dockerfile
FROM $EMR_IMAGE
USER root

# Kyuubi dist and authZ plugin
COPY apache-kyuubi-$KYUUBI_RELEASE-bin.tgz /usr/lib/
COPY kyuubi-spark-authz_2.12-$KYUUBI_RELEASE.jar /usr/lib/spark/jars/

# Ranger dependencies for authZ plugin
COPY ranger-plugins-audit-$RANGER_VERSION.jar /usr/lib/spark/jars/
COPY ranger-plugins-common-$RANGER_VERSION.jar /usr/lib/spark/jars/
COPY gethostname4j-*.jar /usr/lib/spark/jars/
COPY jna-*.jar /usr/lib/spark/jars/
COPY jackson-jaxrs-*.jar /usr/lib/spark/jars/
COPY jersey-bundle-*.jar /usr/lib/spark/jars/

RUN cd /usr/lib/ && \
    tar zxf apache-kyuubi-$KYUUBI_RELEASE-bin.tgz && \
    mv apache-kyuubi-$KYUUBI_RELEASE-bin kyuubi && \
    rm apache-kyuubi-$KYUUBI_RELEASE-bin.tgz && \
    mkdir -p /etc/kyuubi && ln -s $KYUUBI_PATH/conf /etc/kyuubi/ && \
    mv $KYUUBI_PATH/conf/kyuubi-env.sh.template $KYUUBI_PATH/conf/kyuubi-env.sh && \
    echo -e "export SPARK_HOME=/usr/lib/spark" >> $KYUUBI_PATH/conf/kyuubi-env.sh && \
    echo -e "export SPARK_CONF_DIR=/etc/spark/conf" >> $KYUUBI_PATH/conf/kyuubi-env.sh && \
    echo -e "export HADOOP_CONF_DIR=/etc/hadoop/conf" >> $KYUUBI_PATH/conf/kyuubi-env.sh && \
    echo -e "export KYUUBI_WORK_DIR_ROOT=$KYUUBI_PATH/work" >> $KYUUBI_PATH/conf/kyuubi-env.sh && \
    ln -s $KYUUBI_PATH/externals/engines/spark/kyuubi-spark-sql-engine*.jar $KYUUBI_PATH/externals/engines/spark/kyuubi-spark-sql.jar && \
    mkdir -p $DELTA_PATH && cd $DELTA_PATH && \
    curl -O https://repo1.maven.org/maven2/io/delta/delta-core_2.12/$DELTA_VERSION/delta-core_2.12-$DELTA_VERSION.jar && \
    curl -O https://repo1.maven.org/maven2/io/delta/delta-storage/$DELTA_VERSION/delta-storage-$DELTA_VERSION.jar && \
    ln -s $DELTA_PATH/delta-core_2.12-$DELTA_VERSION.jar $DELTA_PATH/delta-core.jar && \
    ln -s $DELTA_PATH/delta-storage-$DELTA_VERSION.jar $DELTA_PATH/delta-storage.jar && \
    cd /usr/lib/spark/jars/ && \
    curl -O https://repo1.maven.org/maven2/mysql/mysql-connector-java/8.0.26/mysql-connector-java-8.0.26.jar && \
    chown hadoop:hadoop -R $KYUUBI_PATH

USER hadoop:hadoop
CMD [ "$KYUUBI_PATH/bin/kyuubi", "run" ]
EOF

## build docker image
docker build -t kyuubi/${EMR_RELEASE} .

## create ECR repository
aws ecr create-repository \
    --repository-name kyuubi/$EMR_RELEASE \
    --image-scanning-configuration scanOnPush=true \
    --region $REGION

## push image on ECR repository
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
docker tag kyuubi/${EMR_RELEASE} ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/kyuubi/${EMR_RELEASE}
docker push ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/kyuubi/${EMR_RELEASE}

rm -rf $DIR/build/
rm -rf $DIR/tmp
