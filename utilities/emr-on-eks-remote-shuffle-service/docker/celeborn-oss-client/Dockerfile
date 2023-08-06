# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
ARG SPARK_BASE_IMAGE=public.ecr.aws/myang-poc/spark:3.2.0_hadoop_3.3.1

FROM openjdk:8-jdk-slim as rss-client
ARG SPARK_VERSION=3.2

# install maven
ENV MAVEN_VERSION=3.8.8
ENV BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries
RUN apt-get update && apt-get install -y curl git tar zlib1g zlib1g-dev lzop lsof netcat dnsutils less procps iputils-ping \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"
# build celeborn binary
# RUN git clone https://github.com/apache/incubator-celeborn.git -b branch-${CELEBORN_VERSION}
RUN git clone https://github.com/apache/incubator-celeborn.git
WORKDIR incubator-celeborn
RUN ./build/make-distribution.sh -Pspark-${SPARK_VERSION} && \
    cat *.tgz | tar -xvzf - && \
    mv apache-celeborn-*-bin /tmp/celeborn

FROM mozilla/sbt:8u292_1.5.4 as sbt
ENV SPARK_VERSION=3.2.0
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
# ENV HADOOP_VERSION=3.3.1

# # Download Hadoop
# ADD http://archive.apache.org/dist/hadoop/common/hadoop-${HADOOP_VERSION}/hadoop-${HADOOP_VERSION}.tar.gz .
# # Unzip Hadoop
# RUN tar -xvzf hadoop-${HADOOP_VERSION}.tar.gz
# RUN mv hadoop-${HADOOP_VERSION} /opt/hadoop
# RUN rm -rf hadoop/share/doc

COPY --from=rss-client /tmp/celeborn/spark  ${SPARK_HOME}/jars
COPY --from=sbt /tmp/emr-on-eks-benchmark/benchmark/target/scala-2.12/*jar ${SPARK_HOME}/examples/jars/

# ENV HADOOP_HOME /opt/hadoop
# ENV SPARK_DIST_CLASSPATH="$HADOOP_HOME/etc/hadoop:$HADOOP_HOME/share/hadoop/common/lib/*:$HADOOP_HOME/share/hadoop/common/*:$HADOOP_HOME/share/hadoop/hdfs:$HADOOP_HOME/share/hadoop/hdfs/lib/*:$HADOOP_HOME/share/hadoop/hdfs/*:$HADOOP_HOME/share/hadoop/yarn:$HADOOP_HOME/share/hadoop/yarn/lib/*:$HADOOP_HOME/share/hadoop/yarn/*:$HADOOP_HOME/share/hadoop/mapreduce/lib/*:$HADOOP_HOME/share/hadoop/mapreduce/*:/contrib/capacity-scheduler/*.jar:$HADOOP_HOME/share/hadoop/tools/lib/*"
# # Create a Hadoop user/group
# RUN addgroup -gid 1000 hadoop && \
# useradd -u 1000 -g hadoop -m -s /bin/sh hadoop && \
# echo "hadoop:hadoop" | chpasswd
# USER hadoop:hadoop
# ENV SPARK_USER hadoop

WORKDIR /home/hadoop   
# Use hadoop user and group 
USER hadoop:hadoop

