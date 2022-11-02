#!/bin/bash

source $DIR/../functions/cluster_data.sh
source $DIR/../functions/system_manager.sh

# Module display helper
help() {
	cat <<-EOF
	usage: emr benchmark <subcommand>

	  hbase				execute HBase performance evaluation
	  spark				execute Spark SQL TPC-DS benchmark
	  teragen			execute teragen benchmark
	  terasort			execute terasort benchmark

	EOF
}

# Hadoop / YARN tests (Teragen, Terasort)
HADOOP_JAR="/usr/lib/hadoop-mapreduce/hadoop-mapreduce-examples.jar"

# HBase configs
HBASE_CMD="sudo -u hbase hbase"

# Spark configs
SPARK_BENCH_JAR="/usr/lib/spark/jars/spark-sql-perf.jar"
SPARK_BENCH_UTILITY_JAR="/usr/lib/spark/jars/spark-benchmark.jar"
SPARK_BENCH_RESULTS="hdfs:///tmp/spark-benchmark-results"
SPARK_DATASET_SCALE="10"
TCPDS_INSTALL_PATH="/opt/tpcds-kit"
TPCDS_GIT_REPO="https://github.com/databricks/tpcds-kit.git"

# Teragen / Terasort configs
# number of 100-byte rows (default 1GB)
TERAGEN_ROWS=10000000
TERAGEN_OUT="hdfs:///tmp/teragen/"
TERASORT_OUT="hdfs:///tmp/terasort/"

# Messages
WARN_DEV="It's recommended to use this command in a DEV environment only"
WARN_KILL="All running YARN applications will be killed!"
MSG_IS_MASTER="this command can only run on the EMR MASTER node"

## hbase
## usage: emr benchmark hbase <OPTIONS>
##
## Execute HBase performance evaluation.
##
##   OPTIONS            HBase pe options to run the benchmark
##
## Usage example:
##
##   emr benchmark hbase         # display HBase pe options
##
hbase() {
	usage_function "benchmark" "hbase" "$*"
	is_installed "HBase"
	is_master $MSG_IS_MASTER
	$HBASE_CMD pe $*
	echo && exit 0
}

## spark
## usage: emr benchmark spark <DATASET> [SCALE]
##
## Execute Spark SQL TPC-DS benchmark (parquet).
## If the dataset does not exists, it's automatically created.
## Creation of the dataset is not supported when managed scaling is enabled.
##
##   DATASET            Location of the TPC-DS dataset. If the dataset does not
##                      exists, it will be created. Supports S3 and HDFS paths
##   SCALE              Define the size in GB of the dataset. This is only used
##                      when the dataset does not exists. Default: 10
##
## Usage example:
##
##   emr benchmark spark s3://BUCKET           # Benchmark, dataset on S3
##   emr benchmark spark s3://BUCKET 1000      # Benchmark, create 1TB data
##   emr benchmark spark hdfs:///tmp/data      # Benchmark dataset on HDFS
##
spark() {

	usage_function "benchmark" "spark" "$*"

	is_installed "Spark"
	is_master $MSG_IS_MASTER

	DATASET="$1"
	SCALE="$2"
	[[ -z $DATASET ]] && error "Dataset path not defined"
	[[ -z $SCALE ]] && SCALE="$SPARK_DATASET_SCALE"

	# check if managed scaling is enabled
	# check DATASET path exists
	managed_scaling_data
	require_data=$(hdfs dfs -test -d "$DATASET";echo $?)
	[[ $require_data -ne 0 && $cluster_managed_scaling == "true" ]] && error "spark benchmark does not support managed scaling when the input dataset doesn't exists."

	warning "$WARN_KILL"
	warning "$WARN_DEV"
	warning "This will install additional software on cluster nodes!"
	ask_confirmation

	# Build required libraries
	[[ ! -f "$SPARK_BENCH_JAR" ]] && _install_spark_benchmark
	[[ ! -f "$SPARK_BENCH_UTILITY_JAR" ]] && _install_spark_benchmark_utility

	# Install TPCDS KIT on all cluster nodes if we have to generate TCPDS data
	[[ $require_data -ne 0 && ! -d "$TCPDS_INSTALL_PATH" ]] && run_cmd_all ["yum install -y gcc make flex bison byacc git sbt && yum clean all","rm -rf $TCPDS_INSTALL_PATH","mkdir -p $TCPDS_INSTALL_PATH","git clone $TPCDS_GIT_REPO $TCPDS_INSTALL_PATH","cd $TCPDS_INSTALL_PATH/tools","make OS=LINUX"]

	# Kill all YARN applications
	_kill_yarn_apps

	vcores=$(_total_vcores)
	[[ ! $vcores =~ [0-9]+ ]] && error "Invalid number of vcores detected invoking YARN RM API."

	spark-submit \
		--class com.amazonaws.BenchmarkRun \
		$SPARK_BENCH_UTILITY_JAR \
		"$DATASET" \
		"$SPARK_BENCH_RESULTS" \
		"$TCPDS_INSTALL_PATH/tools" \
		"parquet" \
		"$SCALE" \
		"1" \
		"" \
		"$vcores"

	echo && exit 0
}

## teragen
## usage: emr benchmark teragen [SCALE]
##
##   SCALE		Scale factor of the dataset in GB (default 1GB)
##
## Execute teragen performance test.
## The test generates sample data that can be used for terasort benchmark.
##
teragen() {

	usage_function "benchmark" "teragen" "$*"

	SCALE=${1,,}
	[[ -z $SCALE ]] && dataset_size=$TERAGEN_ROWS
	[[ (! -z $SCALE) && (! "$SCALE" =~ [0-9]+g?b? ) ]] && error "$SCALE is not properly defined"
	[[ (! -z $SCALE) && ( "$SCALE" =~ [0-9]+g?b? ) ]] && dataset_size=$((10000000 * ${SCALE/[a-z]*/}))

	warning "$WARN_KILL"
	warning "$WARN_DEV"
	warning "$TERAGEN_OUT folder will be deleted!"
	ask_confirmation

	# Kill all YARN applications
	_kill_yarn_apps

	# Delete the output directory if exists
	hdfs dfs -rm -r -f -skipTrash "${TERAGEN_OUT}"

	# Run teragen
	hadoop jar $HADOOP_JAR teragen "$dataset_size" "$TERAGEN_OUT"
	echo && exit 0
}

## terasort
## usage: emr benchmark terasort
##
## Execute terasort performance test.
## The test uses data generated in the teragen becnhmark.
##
terasort() {

	usage_function "benchmark" "terasort" "$*"

	warning "$WARN_KILL"
	warning "$WARN_DEV"
	warning "$TERASORT_OUT folder will be deleted from HDFS!"
	ask_confirmation

	hdfs dfs -test -d "$TERAGEN_OUT"
	[[ $? -ne 0 ]] && error "$TERAGEN_OUT folder does not exists. Run teragen benchmark first"

	# Kill all YARN applications
	_kill_yarn_apps

	# Delete the output directory if exists
	hdfs dfs -rm -r -f -skipTrash "${TERASORT_OUT}"

	# Run terasort
	hadoop jar $HADOOP_JAR terasort \
		-Dmapreduce.terasort.output.replication=1 \
		${TERAGEN_OUT} ${TERASORT_OUT}

	echo && exit 0
}


#===============================================================================
# Helper Functions
#===============================================================================

# Kill all YARN applications scheduled and running
_kill_yarn_apps() {
	for app in $(yarn application -list -appStates NEW,NEW_SAVING,SUBMITTED,ACCEPTED,RUNNING 2>/dev/null | awk 'NR > 2 { print $1 }'); do
		yarn application -kill $app;
	done
}

# Install Spark benchmark
_install_spark_benchmark() {

	BUILD_PATH="/tmp/spark-sql-perf"

	sudo wget https://repos.fedorapeople.org/repos/dchen/apache-maven/epel-apache-maven.repo -O /etc/yum.repos.d/epel-apache-maven.repo
	sudo sed -i s/\$releasever/6/g /etc/yum.repos.d/epel-apache-maven.repo
	sudo wget https://www.scala-sbt.org/sbt-rpm.repo -O /etc/yum.repos.d/sbt-rpm.repo

	sudo yum -y install apache-maven git sbt

	# Download and build databricks perf tools
	git clone https://github.com/databricks/spark-sql-perf $BUILD_PATH
	cd $BUILD_PATH

	sbt clean +package
	sudo cp target/scala-2.12/spark-sql-perf_2.12-*.jar $SPARK_BENCH_JAR
	rm -rf $BUILD_PATH
	cd -
}

_install_spark_benchmark_utility() {
	# Build spark benchmark utility
	cd "$INSTALL_PATH/spark-benchmark"
	sudo mkdir -p $INSTALL_PATH/spark-benchmark/libs
	sudo cp $SPARK_BENCH_JAR "$INSTALL_PATH/spark-benchmark/libs/"
	sudo sbt clean package
	sudo cp target/scala-2.12/spark-benchmark_*.jar /usr/lib/spark/jars/spark-benchmark.jar
	cd -
}

_total_vcores() {
	curl --compressed -H "Accept: application/json" -X GET http://$(hostname -f):8088/ws/v1/cluster/metrics 2> /dev/null | jq -r .clusterMetrics.totalVirtualCores
}
