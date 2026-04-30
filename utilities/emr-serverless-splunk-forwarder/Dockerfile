ARG EMR_RELEASE=emr-7.1.0
FROM public.ecr.aws/emr-serverless/spark/${EMR_RELEASE}:latest

USER root

# Install Splunk Universal Forwarder
# Download from https://www.splunk.com/en_us/download/universal-forwarder.html
# Place the RPM in this directory before building
COPY splunkforwarder.rpm /tmp/splunkforwarder.rpm
RUN rpm -Uvh /tmp/splunkforwarder.rpm && rm /tmp/splunkforwarder.rpm

# Create directories and set permissions for hadoop user
RUN mkdir -p /var/log/spark-app \
    /opt/splunkforwarder/var/lib/splunk \
    /opt/splunkforwarder/var/run/splunk \
    /opt/splunkforwarder/var/spool/splunk \
    /opt/splunkforwarder/var/log/splunk && \
    chown -R hadoop:hadoop /opt/splunkforwarder /var/log/spark-app

# Copy config templates and startup script
COPY conf/inputs.conf.template /opt/splunkforwarder/etc/system/local/inputs.conf.template
COPY conf/outputs.conf.template /opt/splunkforwarder/etc/system/local/outputs.conf.template
COPY conf/deploymentclient.conf.template /opt/splunkforwarder/etc/system/local/deploymentclient.conf.template
COPY conf/limits.conf /opt/splunkforwarder/etc/system/local/limits.conf
RUN chown -R hadoop:hadoop /opt/splunkforwarder/etc/system/local/

COPY start-splunk.sh /usr/local/bin/start-splunk.sh
RUN chmod +x /usr/local/bin/start-splunk.sh

# Copy pre-built Spark listener JAR
COPY listener/splunk-listener.jar /usr/lib/spark/jars/splunk-listener.jar

USER hadoop
