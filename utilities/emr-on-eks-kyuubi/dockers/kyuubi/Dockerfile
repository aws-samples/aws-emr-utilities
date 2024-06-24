ARG SPARK_BASE_IMAGE=755674844232.dkr.ecr.us-east-1.amazonaws.com/spark/emr-6.15.0

FROM maven:3.8.8-amazoncorretto-8-debian AS kyuubi_compiled
USER root

ARG DEBIAN_FRONTEND=noninteractive
ARG KYUUBI_VERSION=1.8.0
ARG SPARK_VERSION=3.4.1
ARG RANGER_VERSION=2.4.0

ENV KYUUBI_VERSION=${KYUUBI_VERSION}
ENV SPARK_VERSION=${SPARK_VERSION}
ENV RANGER_VERSION=${RANGER_VERSION}
ENV KYUUBI_HOME=/opt/kyuubi
ENV KYUUBI_LOG_DIR=${KYUUBI_HOME}/logs
ENV KYUUBI_PID_DIR=${KYUUBI_HOME}/pid
ENV KYUUBI_WORK_DIR_ROOT=${KYUUBI_HOME}/work

# Setup Kyuubi
RUN set -ex && \
    apt-get update  && \
    apt-get install -y git bash tini libc6 libpam-modules krb5-user libnss3 procps && \
    ln -snf /bin/bash /bin/sh && \
    mkdir -p ${KYUUBI_HOME} ${KYUUBI_LOG_DIR} ${KYUUBI_PID_DIR} ${KYUUBI_WORK_DIR_ROOT} && \
    rm -rf /var/cache/apt/*

# Download Kyuubi dist
ADD https://dlcdn.apache.org/kyuubi/kyuubi-${KYUUBI_VERSION}/apache-kyuubi-${KYUUBI_VERSION}-bin.tgz .
RUN tar -xvzf apache-kyuubi-${KYUUBI_VERSION}-bin.tgz && \
    mv apache-kyuubi-${KYUUBI_VERSION}-bin/* ${KYUUBI_HOME}

# Compile Kyuubi Spark Authz plugin from source
RUN cd $KYUUBI_WORK_DIR_ROOT \
    && git clone --depth 1 -b v${KYUUBI_VERSION} https://github.com/apache/kyuubi.git \
    && cd kyuubi \
    && build/mvn clean package -pl :kyuubi-spark-authz_2.12 -DskipTests -Dspark.version=$SPARK_VERSION -Dranger.version=$RANGER_VERSION \
    && mv ${KYUUBI_WORK_DIR_ROOT}/kyuubi/extensions/spark/kyuubi-spark-authz/target/kyuubi-spark-authz_2.12-${KYUUBI_VERSION}.jar ${KYUUBI_HOME}/externals/engines/spark/kyuubi-spark-authz_2.12-${KYUUBI_VERSION}.jar \
    && mv ${KYUUBI_WORK_DIR_ROOT}/kyuubi/extensions/spark/kyuubi-spark-authz/target/scala-2.12/jars/* ${KYUUBI_HOME}/externals/engines/spark/ \
    && rm -rf ${KYUUBI_WORK_DIR_ROOT}/kyuubi


FROM ${SPARK_BASE_IMAGE}
USER root

ENV SPARK_DIST_CLASSPATH="/usr/lib/hudi/*:/usr/share/aws/iceberg/lib/*:/usr/share/aws/delta/lib/*:/usr/lib/hadoop-lzo/lib/*:/usr/lib/hadoop/hadoop-aws.jar:/usr/share/aws/aws-java-sdk/*:/usr/share/aws/emr/emrfs/conf:/usr/share/aws/emr/emrfs/lib/*:/usr/share/aws/emr/emrfs/auxlib/*:/usr/share/aws/emr/security/conf:/usr/share/aws/emr/security/lib/*:/usr/share/aws/hmclient/lib/aws-glue-datacatalog-spark-client.jar:/usr/share/java/Hive-JSON-Serde/hive-openx-serde.jar:/usr/share/aws/sagemaker-spark-sdk/lib/sagemaker-spark-sdk.jar:/home/hadoop/extrajars/*"
ENV SPARK_EXTRA_CLASSPATH="$SPARK_DIST_CLASSPATH"
ENV KYUUBI_HOME=/usr/lib/kyuubi

COPY --from=kyuubi_compiled /opt/kyuubi ${KYUUBI_HOME}
COPY --from=kyuubi_compiled /opt/kyuubi/externals/engines/spark ${SPARK_HOME}/jars

# ADD https://repo1.maven.org/maven2/mysql/mysql-connector-java/8.0.26/mysql-connector-java-8.0.26.jar ${SPARK_HOME}/jars

WORKDIR ${KYUUBI_HOME}
RUN chown hadoop:hadoop -R ${KYUUBI_HOME} && \
    chmod ug+rw -R ${KYUUBI_HOME} && \
    chmod a+rwx -R ${KYUUBI_HOME}/work

USER hadoop:hadoop
CMD [ "./bin/kyuubi", "run" ]