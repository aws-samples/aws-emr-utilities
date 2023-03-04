# FROM amazoncorretto:8 as rss-jdk8
FROM openjdk:8-jdk-slim as rss-jdk8
# FROM amazonlinux:2 as rss-jdk8
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries

# install maven

# uncomment the line if run amazonlinux2 as the base image
# RUN yum update -y && yum install -y tar git  
RUN apt-get update && apt-get install -y curl git tar procps
RUN mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn
