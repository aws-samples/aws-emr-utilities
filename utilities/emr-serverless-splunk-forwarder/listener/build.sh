#!/bin/bash
# Builds the Spark listener JAR.
# Requires: Java JDK, spark-core JAR (extracted from the EMR Serverless image)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Extract spark-core from the Docker image if not already present
if [ ! -f /tmp/spark-core.jar ]; then
    echo "Extracting spark-core JAR from EMR Serverless image..."
    SPARK_JAR=$(docker run --rm --entrypoint="" public.ecr.aws/emr-serverless/spark/emr-7.1.0:latest \
        find /usr/lib/spark/jars -name "spark-core*" | head -1)
    docker run --rm --entrypoint="" -v /tmp:/out public.ecr.aws/emr-serverless/spark/emr-7.1.0:latest \
        cp "$SPARK_JAR" /out/spark-core.jar
fi

mkdir -p build
javac -cp /tmp/spark-core.jar -d build src/com/emr/splunk/SplunkForwarderListener.java
cd build
jar cf ../splunk-listener.jar com/
cd ..
rm -rf build

echo "Built: listener/splunk-listener.jar"
