FROM python:3.11-slim AS base

# Copy OpenJDK 17
ENV JAVA_HOME=/opt/java/openjdk
COPY --from=eclipse-temurin:11 $JAVA_HOME $JAVA_HOME
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Upgrade pip
RUN pip3 install --upgrade pip

# Configure PySpark
ENV PYSPARK_DRIVER_PYTHON python3
ENV PYSPARK_PYTHON python3

# Install pyarrow
RUN pip3 install pyarrow==12.0.0