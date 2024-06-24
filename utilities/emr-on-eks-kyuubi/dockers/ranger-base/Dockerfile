FROM maven:3.6.3-jdk-8 as base

ARG RANGER_VERSION=2.4.0
ENV RANGER_VERSION=${RANGER_VERSION}

RUN apt-get update && \
    apt-get install -y python3-dev gcc git && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV MAVEN_OPTS "-Xmx4096m -XX:MaxMetaspaceSize=512m"

RUN cd /opt \
    && git clone --depth 1 -b release-ranger-$RANGER_VERSION https://github.com/apache/ranger.git \
    && cd ranger \
    && mvn -DskipTests clean compile package install --errors --show-version -am  --no-transfer-progress \
    && rm -rf ~/.m2 \
    && ls | grep -v target | xargs rm -rf \
    && rm -rf target/*.zip

ENTRYPOINT ["bash"]