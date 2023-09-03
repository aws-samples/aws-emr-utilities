#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

ARG java_image_tag=8-jdk-focal

FROM eclipse-temurin:${java_image_tag} as jdk
ARG SPARK_VERSION=3.2

RUN apt-get update && apt-get install -y git
# build celeborn binary
RUN git clone https://github.com/apache/incubator-celeborn.git -b v0.3.0-incubating
# RUN git clone https://github.com/apache/incubator-celeborn.git -b branch-${CELEBORN_VERSION}
WORKDIR incubator-celeborn
RUN ./build/make-distribution.sh -Pspark-${SPARK_VERSION} && \
    cat *.tgz | tar -xvzf - && \
    mv apache-celeborn-*-bin /opt/celeborn

FROM eclipse-temurin:${java_image_tag}
ENV celeborn_uid=10006
ENV celeborn_gid=10006
ENV CELEBORN_HOME=/opt/celeborn
ENV PATH=/opt/java/openjdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/busybox:${CELEBORN_HOME}/sbin:${CELEBORN_HOME}/bin

ENV DEBIAN_FRONTEND=noninteractive
RUN set -ex && \
    apt-get update && \
    apt-get install -y git bash tini busybox bind9-utils telnet net-tools procps dnsutils krb5-user && \
    ln -snf /bin/bash /bin/sh && \
    rm -rf /var/cache/apt/* && \
    mkdir /opt/busybox && \
    busybox --install /opt/busybox && \
    groupadd --gid=${celeborn_gid} celeborn && \
    useradd  --uid=${celeborn_uid} --gid=${celeborn_gid} celeborn -d /home/celeborn -m && \
    mkdir -p ${CELEBORN_HOME}

COPY --from=jdk /opt/celeborn/bin /opt/celeborn/bin
COPY --from=jdk /opt/celeborn/sbin /opt/celeborn/sbin
COPY --from=jdk /opt/celeborn/master-jars /opt/celeborn/master-jars
COPY --from=jdk /opt/celeborn/worker-jars /opt/celeborn/worker-jars
COPY --from=jdk /opt/celeborn/jars /opt/celeborn/jars
COPY --from=jdk /opt/celeborn/RELEASE /opt/celeborn/RELEASE

RUN chown -R celeborn:celeborn ${CELEBORN_HOME} && \
    chmod -R ug+rw ${CELEBORN_HOME} && \
    chmod a+x ${CELEBORN_HOME}/bin/* && \
    chmod a+x ${CELEBORN_HOME}/sbin/*

USER celeborn
WORKDIR ${CELEBORN_HOME}
ENTRYPOINT ["/usr/bin/tini", "--"]
