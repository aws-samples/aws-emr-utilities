FROM armadik/ranger-builds:2.4.0 as base

FROM ubuntu:22.04
USER root
COPY --from=base /opt/ranger /opt/ranger-src

ARG RANGER_VERSION=2.4.0
ENV RANGER_VERSION=${RANGER_VERSION}
ENV JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk-amd64
ENV USERSYNC_HOME=/opt/ranger_usersync

RUN apt-get update && \
    apt-get install -y openjdk-8-jdk python3-minimal python-is-python3 vim xmlstarlet && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN tar -zxf /opt/ranger-src/target/ranger-$RANGER_VERSION-usersync.tar.gz -C /opt/  && \
    ln -s /opt/ranger-$RANGER_VERSION-usersync $USERSYNC_HOME && \
    rm -rf /opt/ranger-src

COPY bootstrap.sh $RANGER_USERSYNC_HOME/

RUN useradd -m -s /bin/bash ranger && \
    chmod +x $USERSYNC_HOME/bootstrap.sh && \
    chown -R ranger $USERSYNC_HOME/

WORKDIR $USERSYNC_HOME
ENTRYPOINT ["/opt/ranger_usersync/bootstrap.sh"]
# ENTRYPOINT ["bash"]