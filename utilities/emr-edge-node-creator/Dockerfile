FROM amazonlinux:latest
ENV JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk

RUN yum update -y && yum install -y awscli python3 tar gzip sudo which yum-utils less procps
# COPY *.sh sbin/
ADD https://raw.githubusercontent.com/melodyyangaws/aws-emr-utilities/main/utilities/emr-edge-node-creator/setup-emr-edge-node-s3.sh sbin/
RUN chmod +x sbin/*.sh

ENTRYPOINT ["sbin/setup-emr-edge-node-s3.sh"]
EXPOSE 5000-5020 10000 4040-4060