FROM armadik/ranger-builds:2.4.0 as base

FROM ubuntu:22.04
USER root
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=base /opt/ranger /opt/ranger-src
ENV RANGER_VERSION=2.4.0
ENV RANGER_ADMIN_CONF=/opt/ranger-admin
ENV RANGER_USER=ranger
ENV RANGER_HOME=/opt/ranger-admin

RUN useradd -m -s /bin/bash $RANGER_USER

RUN apt-get update && \
    apt-get install -y openjdk-8-jdk lsb-release bc wget vim curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk-amd64

RUN tar -zxf /opt/ranger-src/target/ranger-$RANGER_VERSION-admin.tar.gz -C /opt/  && \
    ln -s /opt/ranger-$RANGER_VERSION-admin $RANGER_HOME && \
    rm -rf /opt/ranger-src/target/*

RUN wget https://jdbc.postgresql.org/download/postgresql-42.7.3.jar -O /usr/share/java/postgresql-connector-java.jar && \
    chmod 644 /usr/share/java/postgresql-connector-java.jar && \
    chown ranger:ranger /usr/share/java/postgresql-connector-java.jar

# RUN wget https://repo1.maven.org/maven2/mysql/mysql-connector-java/8.0.29/mysql-connector-java-8.0.29.jar -O /usr/share/java/mysql-connector-java.jar && \
#     chmod 644 /usr/share/java/mysql-connector-java.jar && \
#     chown $RANGER_USER /usr/share/java/mysql-connector-java.jar
    
RUN mkdir -p $RANGER_HOME/jisql/lib/
COPY *.jar $RANGER_HOME/jisql/lib/
COPY bootstrap.sh $RANGER_HOME/
RUN chmod +x $RANGER_HOME/bootstrap.sh && \
    chown -R $RANGER_USER $RANGER_HOME/

EXPOSE 6080
WORKDIR $RANGER_HOME
ENTRYPOINT ["/opt/ranger-admin/bootstrap.sh"]
# ENTRYPOINT [ "bash" ]



