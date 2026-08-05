#!/usr/bin/python
# Sample bootstrap action from an EMR 5.33.0 cluster
set -e

# Install dependencies
yum install -y mysql-server java-1.8.0-openjdk-devel python-pip
pip install boto3 numpy pandas

# Start MySQL
service mysqld start
chkconfig mysqld on

# Get instance metadata for conditional logic
INSTANCE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type)
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
echo "Running on $INSTANCE_TYPE ($INSTANCE_ID)"

# Sync EMRFS metadata
emrfs sync s3://my-data-bucket/warehouse/

# Copy custom JARs
hadoop fs -cp s3n://my-jars-bucket/lib/custom-udf.jar /usr/lib/hadoop/lib/

# Set Java home
export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk

echo "Bootstrap complete"
