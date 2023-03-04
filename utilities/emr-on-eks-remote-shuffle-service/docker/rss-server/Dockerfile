# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
# ARG SPARK_BASE_IMAGE=public.ecr.aws/myang-poc/spark:3.2.1_hadoop_3.3.1

FROM amazoncorretto:11 as rss-jdk11
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries

# install maven
RUN yum update -y && yum install -y git tar
RUN mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn

ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"

# compile RSS server
RUN git clone -b k8s-spark-3.2 --single-branch https://github.com/datapunchorg/RemoteShuffleService.git /tmp/RemoteShuffleService
WORKDIR /tmp/RemoteShuffleService
RUN mvn clean package -Pserver -DskipTests -Dmaven.javadoc.skip=true
RUN rm target/original-remote-shuffle-service-*.jar \
    && rm target/remote-shuffle-service-*-sources.jar \
    && mv target/remote-shuffle-service-server-*.jar /tmp/RemoteShuffleService/remote-shuffle-service-server.jar
