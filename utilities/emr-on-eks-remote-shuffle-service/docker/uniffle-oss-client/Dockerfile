# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
ARG SPARK_BASE_IMAGE=021732063925.dkr.ecr.us-east-1.amazonaws.com/spark:3.2.0_hadoop_3.3.1

FROM openjdk:8-jdk-slim as rss-client
ARG SPARK_VERSION=3.2.0
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries

# install maven
RUN apt-get update && apt-get install -y curl git tar zlib1g zlib1g-dev lzop lsof netcat dnsutils less procps iputils-ping \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"
# compile RSS client
RUN git clone https://github.com/apache/incubator-uniffle.git /tmp/uniffle
WORKDIR /tmp/uniffle
RUN mvn clean package -Pspark${SPARK_VERSION} -DskipTests -Dmaven.javadoc.skip=true 

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
USER root

COPY --from=rss-client /tmp/uniffle/client-spark/spark3/target/shaded/rss-client-spark3-*-shaded.jar  ${SPARK_HOME}/jars/uniffle-rss-client.jar
COPY --from=sbt /tmp/emr-on-eks-benchmark/benchmark/target/scala-2.12/*jar ${SPARK_HOME}/examples/jars/

WORKDIR /home/hadoop   
# Use hadoop user and group 
USER hadoop:hadoop

