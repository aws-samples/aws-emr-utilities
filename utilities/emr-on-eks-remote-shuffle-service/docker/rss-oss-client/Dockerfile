# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
ARG SPARK_BASE_IMAGE=ghcr.io/datapunchorg/spark:spark-3.2.1-1643336295

FROM amazonlinux:2 as tpc-toolkit
# compile tpcds kit
RUN yum update -y && \
    yum group install -y "Development Tools" \
    && git clone https://github.com/databricks/tpcds-kit.git -b master /tmp/tpcds-kit \
    && cd /tmp/tpcds-kit/tools \
    && make OS=LINUX 

FROM mozilla/sbt:8u292_1.5.4 as sbt
ARG SPARK_VERSION=3.2.0

# Build the Databricks SQL perf library
RUN git clone -b spark-${SPARK_VERSION} --single-branch https://github.com/aws-samples/emr-on-eks-benchmark.git /tmp/emr-on-eks-benchmark \
    && cd /tmp/emr-on-eks-benchmark/spark-sql-perf/ \
    && sbt +package   
     
# Use the compiled Databricks SQL perf library to build benchmark utility
RUN cd /tmp/emr-on-eks-benchmark/ && mkdir /tmp/emr-on-eks-benchmark/benchmark/libs \
    && cp /tmp/emr-on-eks-benchmark/spark-sql-perf/target/scala-2.12/*.jar /tmp/emr-on-eks-benchmark/benchmark/libs \
    && cd /tmp/emr-on-eks-benchmark/benchmark && sbt assembly

FROM ${SPARK_BASE_IMAGE}
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries

USER root

COPY --from=tpc-toolkit /tmp/tpcds-kit/tools /opt/tpcds-kit/tools
COPY --from=sbt /tmp/emr-on-eks-benchmark/benchmark/target/scala-2.12/*jar ${SPARK_HOME}/examples/jars/

# # upgrade JDK to corretto11, still not compatible with EMR on EKS
# RUN apt-get update && apt-get install -y curl git
#     && curl -fsSL -o /usr/lib/amazon-corretto-11-x64-linux-jdk.tar.gz https://corretto.aws/downloads/latest/amazon-corretto-11-x64-linux-jdk.tar.gz \
#     && tar -xzf /usr/lib/amazon-corretto-11-x64-linux-jdk.tar.gz \
#     && rm -f /usr/lib/amazon-corretto-11-x64-linux-jdk.tar.gz
# RUN update-alternatives --install "/usr/bin/java" "java" "/usr/lib/amazon-corretto-11.0.16.9.1-linux-x64/bin/java" 2000
# ENV JAVA_HOME /usr/lib/java
# ENV PATH $PATH:$JAVA_HOME/bin

# install maven
RUN apt-get update && apt-get install -y curl git \
 && mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"

# compile RSS client
RUN git clone -b k8s-spark-3.2 --single-branch https://github.com/datapunchorg/RemoteShuffleService.git /tmp/RemoteShuffleService
WORKDIR /tmp/RemoteShuffleService
RUN mvn clean package -Pclient -DskipTests -Dmaven.javadoc.skip=true \
    && rm target/original-remote-shuffle-service-*.jar \
    && rm target/remote-shuffle-service-*-sources.jar \
    && mv target/remote-shuffle-service-client-*.jar ${SPARK_HOME}/jars/ 

WORKDIR /home/hadoop   
# Use hadoop user and group 
USER hadoop:hadoop

