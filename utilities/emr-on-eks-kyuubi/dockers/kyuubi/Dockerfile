ARG SPARK_BASE_IMAGE=755674844232.dkr.ecr.us-east-1.amazonaws.com/spark/emr-6.15.0


FROM eclipse-temurin:8-jdk-focal AS kyuubi_compiled
USER root

ARG KYUUBI_VERSION=1.8.0

ENV kyuubi_uid=10009
ENV KYUUBI_HOME /opt/kyuubi
ENV KYUUBI_LOG_DIR ${KYUUBI_HOME}/logs
ENV KYUUBI_PID_DIR ${KYUUBI_HOME}/pid
ENV KYUUBI_WORK_DIR_ROOT ${KYUUBI_HOME}/work

# Setup Kyuubi
RUN set -ex && \
    sed -i 's/http:\/\/deb.\(.*\)/https:\/\/deb.\1/g' /etc/apt/sources.list && \
    apt-get update  && \
    apt-get install -y bash tini libc6 libpam-modules krb5-user libnss3 procps && \
    ln -snf /bin/bash /bin/sh && \
    useradd -u ${kyuubi_uid} -g root kyuubi -d /home/kyuubi -m && \
    mkdir -p ${KYUUBI_HOME} ${KYUUBI_LOG_DIR} ${KYUUBI_PID_DIR} ${KYUUBI_WORK_DIR_ROOT} && \
    rm -rf /var/cache/apt/*

# Download Kyuubi dist and authZ plugin
ADD https://dlcdn.apache.org/kyuubi/kyuubi-${KYUUBI_VERSION}/apache-kyuubi-${KYUUBI_VERSION}-bin.tgz .
RUN tar -xvzf apache-kyuubi-${KYUUBI_VERSION}-bin.tgz && \
    mv apache-kyuubi-${KYUUBI_VERSION}-bin/* ${KYUUBI_HOME}
ADD https://repo1.maven.org/maven2/org/apache/kyuubi/kyuubi-spark-authz_2.12/${KYUUBI_VERSION}/kyuubi-spark-authz_2.12-${KYUUBI_VERSION}.jar ${KYUUBI_HOME}/externals/engines/spark 
RUN chown root -R ${KYUUBI_HOME}/externals/engines/spark/


FROM ${SPARK_BASE_IMAGE}
USER root

ENV SPARK_DIST_CLASSPATH="/usr/lib/hudi/*:/usr/share/aws/iceberg/lib/*:/usr/share/aws/delta/lib/*:/usr/lib/hadoop-lzo/lib/*:/usr/lib/hadoop/hadoop-aws.jar:/usr/share/aws/aws-java-sdk/*:/usr/share/aws/emr/emrfs/conf:/usr/share/aws/emr/emrfs/lib/*:/usr/share/aws/emr/emrfs/auxlib/*:/usr/share/aws/emr/security/conf:/usr/share/aws/emr/security/lib/*:/usr/share/aws/hmclient/lib/aws-glue-datacatalog-spark-client.jar:/usr/share/java/Hive-JSON-Serde/hive-openx-serde.jar:/usr/share/aws/sagemaker-spark-sdk/lib/sagemaker-spark-sdk.jar:/home/hadoop/extrajars/*"
ENV SPARK_EXTRA_CLASSPATH="$SPARK_DIST_CLASSPATH"
ENV KYUUBI_HOME /usr/lib/kyuubi

COPY --from=kyuubi_compiled /opt/kyuubi ${KYUUBI_HOME}
COPY --from=kyuubi_compiled /opt/kyuubi/externals/engines/spark ${SPARK_HOME}/jars
COPY --from=kyuubi_compiled /opt/kyuubi/externals/engines/spark ${KYUUBI_HOME}/jars

ADD https://repo1.maven.org/maven2/mysql/mysql-connector-java/8.0.26/mysql-connector-java-8.0.26.jar ${SPARK_HOME}/jars

WORKDIR ${KYUUBI_HOME}
RUN chown hadoop:hadoop -R ${KYUUBI_HOME}/ && \
    chmod ug+rw -R ${KYUUBI_HOME} && \
    chmod a+rwx -R ${KYUUBI_HOME}/work && \
    chmod +rwx -R ${SPARK_HOME}/jars

CMD [ "./bin/kyuubi", "run" ]
USER hadoop:hadoop