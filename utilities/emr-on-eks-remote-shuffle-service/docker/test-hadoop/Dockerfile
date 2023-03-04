# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
FROM openjdk:8-jdk-slim AS builder

ARG SPARK_VERSION=3.1.2
ARG HADOOP_VERSION=3.3.1

# Download Spark
ADD https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-without-hadoop.tgz .
RUN tar -xvzf spark-${SPARK_VERSION}-bin-without-hadoop.tgz
RUN mv spark-${SPARK_VERSION}-bin-without-hadoop spark

# Download Hadoop
ADD http://archive.apache.org/dist/hadoop/common/hadoop-${HADOOP_VERSION}/hadoop-${HADOOP_VERSION}.tar.gz .
RUN tar -xvzf hadoop-${HADOOP_VERSION}.tar.gz
RUN mv hadoop-${HADOOP_VERSION} hadoop
RUN rm -rf hadoop/share/doc
WORKDIR /hadoop/share/hadoop/tools/lib


FROM openjdk:8-jdk-slim as final
WORKDIR /opt/spark

# Copy Spark from builder stage
COPY --from=builder /spark /opt/spark
COPY --from=builder /spark/kubernetes/dockerfiles/spark/entrypoint.sh /opt
COPY --from=builder /hadoop /opt/hadoop

RUN set -ex && \
    sed -i 's/http:\/\/deb.\(.*\)/https:\/\/deb.\1/g' /etc/apt/sources.list && \
    apt-get update && \
    ln -s /lib /lib64 && \
    apt install -y bash tini libc6 libpam-modules \
    # krb5-user \
    libnss3 procps && \
    touch /opt/spark/RELEASE && \
    rm /bin/sh && \
    ln -sv /bin/bash /bin/sh && \
    echo "auth required pam_wheel.so use_uid" >> /etc/pam.d/su && \
    chgrp root /etc/passwd && chmod ug+rw /etc/passwd && \
    rm -rf /var/cache/apt/*

# Configure environment variables for spark
ENV SPARK_HOME /opt/spark
ENV HADOOP_HOME /opt/hadoop
ENV SPARK_DIST_CLASSPATH="$HADOOP_HOME/etc/hadoop:$HADOOP_HOME/share/hadoop/common/lib/*:$HADOOP_HOME/share/hadoop/common/*:$HADOOP_HOME/share/hadoop/hdfs:$HADOOP_HOME/share/hadoop/hdfs/lib/*:$HADOOP_HOME/share/hadoop/hdfs/*:$HADOOP_HOME/share/hadoop/yarn:$HADOOP_HOME/share/hadoop/yarn/lib/*:$HADOOP_HOME/share/hadoop/yarn/*:$HADOOP_HOME/share/hadoop/mapreduce/lib/*:$HADOOP_HOME/share/hadoop/mapreduce/*:/contrib/capacity-scheduler/*.jar:$HADOOP_HOME/share/hadoop/tools/lib/*"
ENV SPARK_EXTRA_CLASSPATH="$SPARK_DIST_CLASSPATH"
ENV LD_LIBRARY_PATH /lib64

# Create a Hadoop user/group
RUN addgroup -gid 1000 hadoop && \
useradd -u 1000 -g hadoop -m -s /bin/sh hadoop && \
echo "hadoop:hadoop" | chpasswd
USER hadoop:hadoop
ENV SPARK_USER hadoop

# Set spark workdir
WORKDIR /opt/spark/work-dir

ENTRYPOINT [ "/opt/entrypoint.sh" ]
