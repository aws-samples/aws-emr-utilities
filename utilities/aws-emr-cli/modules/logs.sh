#!/bin/bash

# Display usage helper
help() {
	cat <<-EOF
	usage: emr logs <sub-command>

	  emr-ic			EMR intance controller (Internal)
	  emr-is			EMR intance state (Internal)
	  emr-step			EMR Step logs
	  hadoop-hdfs			Hadoop HDFS daemons logs
	  hadoop-yarn			Hadoop YARN daemons logs
	  hive				Hive server logs
	  hbase				HBase server logs
	  hue				Hue server logs
	  livy				Livy server logs
	  phoenix			Phoenix server logs
	  trino				Trino server logs
	  zeppelin			Zeppelin server logs

	EOF
}

#set -x

ERR_MSG_DEST="you must specify a valid S3 path"
MSG_IS_MASTER="this command can only run on the EMR MASTER node"

# retrieve cluster id
cluster_id

## emr-ic
## usage: emr logs emr-ic <TARGET> <FILTER>
##
## Send local Instance Controller logs to an S3 path or AWS Support Case
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs emr-ic s3://BUCKET              # send all logs to S3 bucket
##   emr logs emr-ic s3://BUCKET 2022-10      # send only logs for the month
##
emr-ic() {

	target="$1"
	filter="$2"
	log_path="/emr/instance-controller/log"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/instance-controller/"

	usage_function "logs" "emr-ic" "$*"
	_check_params $target
	_find_and_send "$log_path" "$log_path/instance-controller.log.*$filter.*" "$target" "$path_prefix" && exit 0
}

## emr-is
## usage: emr logs emr-is <TARGET> <FILTER>
##
## Send local Instance State logs to an S3 path or AWS Support Case
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs emr-is s3://BUCKET              # send all logs to S3 bucket
##   emr logs emr-is s3://BUCKET 2022-10      # send only logs for the month
##
emr-is() {

	target="$1"
	filter="$2"
	log_path="/emr/instance-state"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/instance-state/"

	usage_function "logs" "emr-is" "$*"
	_check_params $target
	_find_and_send "$log_path" "$log_path/instance-state.log.*$filter.*" "$target" "$path_prefix" && exit 0
}

## emr-step
## usage: emr logs emr-step <TARGET> <FILTER>
##
## Send local EMR Step's logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs emr-step s3://BUCKET                  # send all logs to S3 bucket
##   emr logs emr-step s3://BUCKET s-1J7H6ZDBA1OBI  # send logs for single step
##
emr-step() {

	target="$1"
	filter="$2"
	log_path="/var/log/hadoop/steps"
	path_prefix="$CLUSTER_ID/steps"

	usage_function "logs" "emr-step" "$*"
	_check_params $target
	is_master $MSG_IS_MASTER
	_find_and_send "$log_path" "$log_path/.*$filter.*" "$target" "$path_prefix" "true" && exit 0
}

## hadoop-hdfs
## usage: emr logs hadoop-hdfs <TARGET> <FILTER>
##
## Send local Hadoop HDFS logs to an S3 path or AWS Support Case.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs hadoop-hdfs s3://BUCKET           # send all logs to S3 bucket
##   emr logs hadoop-hdfs s3://BUCKET 2022-10   # send only logs for the month
##
hadoop-hdfs() {

	target="$1"
	filter="$2"
	log_path="/var/log/hadoop-hdfs"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/hadoop-hdfs/"

	usage_function "logs" "hadoop-hdfs" "$*"
	_check_params $target
	_find_and_send "$log_path" "$log_path/hadoop-hdfs.*$filter.*" "$target" "$path_prefix" && exit 0
}

## hadoop-yarn
## usage: emr logs hadoop-yarn <TARGET> <FILTER>
##
## Send local Hadoop YARN logs to an S3 path or AWS Support Case.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs hadoop-yarn s3://BUCKET           # send all logs to S3 bucket
##   emr logs hadoop-yarn s3://BUCKET 2022-10   # send only logs for the month
##
hadoop-yarn() {

	target="$1"
	filter="$2"
	log_path="/var/log/hadoop-yarn"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/hadoop-yarn/"

	usage_function "logs" "hadoop-yarn" "$*"
	_check_params $target
	_find_and_send "$log_path" "$log_path/hadoop-yarn.*$filter.*" "$target" "$path_prefix" && exit 0
}

## hive
## usage: emr logs hive <TARGET> <FILTER>
##
## Send local Hive logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs hive s3://BUCKET                  # send all logs to S3 bucket
##   emr logs hive s3://BUCKET 2022-10          # send only logs for the month
##
hive() {

	target="$1"
	filter="$2"
	log_path="/var/log/hive"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/hive/"

	usage_function "logs" "hive" "$*"
	is_master $MSG_IS_MASTER
	is_installed "Hive"
	_check_params $target
	_find_and_send "$log_path" "$log_path/hive.*$filter.*" "$target" "$path_prefix" && exit 0
}

## hbase
## usage: emr logs hbase <TARGET> <FILTER>
##
## Send local HBase logs to an S3 path or AWS Support Case.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs hbase s3://BUCKET                  # send all logs to S3 bucket
##   emr logs hbase s3://BUCKET 2022-10          # send only logs for the month
##
hbase() {

	target="$1"
	filter="$2"
	log_path="/var/log/hbase"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/hbase/"

	usage_function "logs" "hbase" "$*"
	is_installed "HBase"
	_check_params $target
	_find_and_send "$log_path" "$log_path/hbase-hbase.*$filter.*" "$target" "$path_prefix" && exit 0
}

## hue
## usage: emr logs hue <TARGET> <FILTER>
##
## Send local Hue logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs hive s3://BUCKET                  # send all logs to S3 bucket
##
hue() {

	target="$1"
	filter="$2"
	log_path="/var/log/hue"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/hue/"

	usage_function "logs" "hue" "$*"
	is_master $MSG_IS_MASTER
	is_installed "Hue"
	_check_params $target
	_find_and_send "$log_path" "$log_path/.*$filter.*" "$target" "$path_prefix" && exit 0
}

## livy
## usage: emr logs livy <TARGET> <FILTER>
##
## Send local Livy logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs livy s3://BUCKET                  # send all logs to S3 bucket
##
livy() {

	target="$1"
	filter="$2"
	log_path="/var/log/livy"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/livy/"

	usage_function "logs" "livy" "$*"
	is_master $MSG_IS_MASTER
	is_installed "Livy"
	_check_params $target
	_find_and_send "$log_path" "$log_path/.*$filter.*" "$target" "$path_prefix" && exit 0
}

## phoenix
## usage: emr logs phoenix <TARGET> <FILTER>
##
## Send local Phoenix logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs phoenix s3://BUCKET              # send all logs to S3 bucket
##
phoenix() {

	target="$1"
	filter="$2"
	log_path="/var/log/phoenix"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/phoenix/"

	usage_function "logs" "phoenix" "$*"
	is_master $MSG_IS_MASTER
	is_installed "Phoenix"
	_check_params $target
	_find_and_send "$log_path" "$log_path/.*$filter.*" "$target" "$path_prefix" && exit 0
}

## trino
## usage: emr logs trino <TARGET> <FILTER>
##
## Send local Trino logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs trino s3://BUCKET              # send all logs to S3 bucket
##
trino() {

	target="$1"
	filter="$2"
	log_path="/var/log/trino"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/trino/"

	usage_function "logs" "trino" "$*"
	is_master $MSG_IS_MASTER
	is_installed "Trino"
	_check_params $target
	_find_and_send "$log_path" "$log_path/.*$filter.*" "$target" "$path_prefix" && exit 0
}

## zeppelin
## usage: emr logs zeppelin <TARGET> <FILTER>
##
## Send local Zeppelin logs to an S3 path or AWS Support Case.
## NOTE: This is only supported when running on the EMR MASTER node.
##
##   TARGET            target path for shipping logs
##   FILTER            regex expression to filter logs
##
## Usage example:
##
##   emr logs zeppelin s3://BUCKET              # send all logs to S3 bucket
##
zeppelin() {

	target="$1"
	filter="$2"
	log_path="/var/log/zeppelin"
	path_prefix="$CLUSTER_ID/nodes/$(hostname -f)/zeppelin/"

	usage_function "logs" "zeppelin" "$*"
	is_master $MSG_IS_MASTER
	is_installed "Zeppelin"
	_check_params $target
	_find_and_send "$log_path" "$log_path/.*$filter.*" "$target" "$path_prefix" && exit 0
}

#===============================================================================
# Helper Functions
#===============================================================================

## _check_params
## usage: _check_params <TARGET>
##
_check_params() {
	[[ -z $1 ]] && error $ERR_MSG_DEST
}

## _find_and_send
## usage: _find_and_send <LOG_PATH> <REGEX> <TARGET> <PREFIX>
##
_find_and_send() {
	#set -x
	log_path="$1"
	regex_expr="$2"
	target="$3"
	prefix="$4"
	is_folder="$5"

	find $log_path -maxdepth 1 -regextype egrep -regex "$regex_expr" -exec bash -c "_send_log \"{}\" $target $prefix $is_folder" \;
	exit 0
}

## _send_log
## usage: _send_log <FILE> <TARGET> <PREFIX> <IS_FOLDER>
##
## send logs to an S3 path or HTTP endpoint (AWS support case)
##
_send_log() {
	#set -x
	ERR_MSG_DEST_INVALID="invalid target path"

	if [[ $2 == s3://* ]]; then
		target_path="${2%/}/$3"
		if [[ ! -z $4 ]]; then
			target_path="$target_path/$(echo $1 | sed -r 's/.*\/(.*)$/\1/')"
			aws s3 cp $1 $target_path --recursive
		else
			aws s3 cp $1 $target_path
		fi
	elif [[ $2 == http:// ]]; then
		# TODO - Add integration with AWS support cases
		echo "HTTP Not yet supported"
		exit 0
	else
		echo $ERR_MSG_DEST_INVALID
		exit 0
	fi
}

export -f _send_log
