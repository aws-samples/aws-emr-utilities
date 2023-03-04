FROM openjdk:8-jdk-slim
# FROM amazoncorretto:8 
ARG MAVEN_VERSION=3.8.6
ARG BASE_URL=https://apache.osuosl.org/maven/maven-3/${MAVEN_VERSION}/binaries
ARG HADOOP_VERSION
ARG RSS_VERSION
ARG AUTHOR=unknown
ARG GIT_BRANCH=unknown
ARG GIT_COMMIT=unknown
LABEL author=${AUTHOR}
LABEL git-branch=${GIT_BRANCH}
LABEL git-commit=${GIT_COMMIT}

RUN apt-get update && apt-get install -y curl git zlib1g zlib1g-dev lzop lsof netcat dnsutils less procps iputils-ping \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# RUN useradd -ms /bin/bash rssadmin \
#     && mkdir -p /data/rssadmin/ \
#     && chown -R rssadmin:rssadmin /data

# Use EMR on EKS user ID 999 instead
RUN addgroup -gid 1000 hadoop \    
    && useradd -u 999 -g hadoop -ms /bin/bash hadoop \
    && echo "hadoop:hadoop" | chpasswd \
    && mkdir -p /data/rssadmin/ \
    && chown -R 999:1000 /data

# install maven
RUN mkdir -p /usr/share/maven /usr/share/maven/ref \
 && curl -fsSL -o /tmp/apache-maven.tar.gz ${BASE_URL}/apache-maven-${MAVEN_VERSION}-bin.tar.gz \
 && tar -xzf /tmp/apache-maven.tar.gz -C /usr/share/maven --strip-components=1 \
 && rm -f /tmp/apache-maven.tar.gz \
 && ln -s /usr/share/maven/bin/mvn /usr/bin/mvn
ENV MAVEN_HOME /usr/share/maven
ENV MAVEN_CONFIG "$USER_HOME_DIR/.m2"

USER hadoop:hadoop

# compile RSS server
RUN git clone https://github.com/apache/incubator-uniffle.git /tmp/uniffle
WORKDIR /tmp/uniffle
# COPY build_distribution.sh .
RUN ./build_distribution.sh --spark3-profile 'spark3.2.0'

RUN cp rss-*.tgz /data/rssadmin \
    && tar -xvf /data/rssadmin/rss-*.tgz --one-top-level=/data/rssadmin/rss --strip-components 1 \
    && rm /data/rssadmin/rss/bin/rss-env.sh \
    && rm -rf /data/rssadmin/rss-*.tgz rss-*.tgz

COPY rss-env.sh /data/rssadmin/rss/bin
COPY start.sh /data/rssadmin/rss/bin

COPY hadoop-${HADOOP_VERSION}.tar.gz /data/rssadmin
RUN tar -zxvf /data/rssadmin/hadoop-*.tar.gz --one-top-level=/data/rssadmin/hadoop --strip-components 1 \
    && rm -rf /data/rssadmin/hadoop-*.tar.gz 
COPY hadoopconfig/ /data/rssadmin/hadoop/etc/hadoop

ENV RSS_VERSION ${RSS_VERSION}
ENV HADOOP_VERSION ${HADOOP_VERSION}

WORKDIR /data/rssadmin/rss
CMD bash bin/start.sh
