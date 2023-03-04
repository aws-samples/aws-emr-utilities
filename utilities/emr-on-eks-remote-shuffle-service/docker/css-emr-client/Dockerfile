# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
ARG SPARK_BASE_IMAGE=755674844232.dkr.ecr.us-east-1.amazonaws.com/spark/emr-6.6.0:latest

FROM amazoncorretto:8 as css-client
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries

# install maven
RUN yum update -y && yum install -y tar git \
 && mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"

# compile CSS client
RUN git clone https://github.com/bytedance/CloudShuffleService.git /tmp/CloudShuffleService
WORKDIR /tmp/CloudShuffleService
RUN ./build.sh

RUN mv css-*-bin.tgz css-bin.tgz \
    && mkdir -p /usr/share/css \
    && tar -xzf /tmp/CloudShuffleService/css-bin.tgz -C /usr/share/css --strip-components=1 \
    && rm -f /tmp/CloudShuffleService/css-bin.tgz

FROM mozilla/sbt:8u292_1.5.4 as sbt
ARG SPARK_VERSION=3.2.0

# Build the Databricks SQL perf library
RUN git clone -b spark-3.2.0 --single-branch https://github.com/aws-samples/emr-on-eks-benchmark.git /tmp/emr-on-eks-benchmark \
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

COPY --from=css-client /usr/share/css/client/spark-3/*.jar ${SPARK_HOME}/jars
COPY --from=sbt /tmp/emr-on-eks-benchmark/benchmark/target/scala-2.12/*jar ${SPARK_HOME}/examples/jars/

# # upgrade JDK to corretto11, still not compatible with EMR on EKS
# RUN  yum install -y java-11-amazon-corretto git
# RUN alternatives --install /usr/bin/java java /usr/lib/jvm/java-11-amazon-corretto.x86_64/bin/java 2000
# RUN alternatives --auto java
# ENV JAVA_HOME /usr/lib/jvm/java
# ENV PATH $PATH:$JAVA_HOME/bin

WORKDIR /home/hadoop
USER hadoop:hadoop

