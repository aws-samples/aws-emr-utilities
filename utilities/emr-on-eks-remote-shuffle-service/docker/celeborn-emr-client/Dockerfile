# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
ARG SPARK_BASE_IMAGE=755674844232.dkr.ecr.us-east-1.amazonaws.com/spark/emr-6.6.0:latest
ARG SPARK_VERSION=3.2
ARG java_image_tag=8-jdk-focal

FROM eclipse-temurin:${java_image_tag} as cele-client
# install maven
RUN apt-get update && apt-get install -y git bash tini bind9-utils procps dnsutils krb5-user
# build celeborn binary
RUN git clone https://github.com/apache/incubator-celeborn.git
WORKDIR incubator-celeborn
RUN ./build/make-distribution.sh -Pspark-${SPARK_VERSION} && \
    cat *.tgz | tar -xvzf - && \
    mv apache-celeborn-*-bin /tmp/celeborn

FROM mozilla/sbt:8u292_1.5.4 as sbt
# Build the Databricks SQL perf library
RUN git clone -b spark-3.2.0 --single-branch https://github.com/aws-samples/emr-on-eks-benchmark.git /tmp/emr-on-eks-benchmark \
    && cd /tmp/emr-on-eks-benchmark/spark-sql-perf/ \
    && sbt +package   
     
# Use the compiled Databricks SQL perf library to build benchmark utility
RUN cd /tmp/emr-on-eks-benchmark/ && mkdir /tmp/emr-on-eks-benchmark/benchmark/libs \
    && cp /tmp/emr-on-eks-benchmark/spark-sql-perf/target/scala-2.12/*.jar /tmp/emr-on-eks-benchmark/benchmark/libs \
    && cd /tmp/emr-on-eks-benchmark/benchmark && sbt assembly

FROM ${SPARK_BASE_IMAGE}
USER root

COPY --from=cele-client /tmp/celeborn/spark ${SPARK_HOME}/jars
COPY --from=sbt /tmp/emr-on-eks-benchmark/benchmark/target/scala-2.12/*jar ${SPARK_HOME}/examples/jars/

WORKDIR /home/hadoop
USER hadoop:hadoop

