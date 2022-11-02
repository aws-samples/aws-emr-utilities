#!/bin/bash

# required libraries
source $DIR/../functions/cluster_data.sh
source $DIR/../functions/cluster_details.sh
source $DIR/../functions/cluster_optimize.sh
source $DIR/../functions/system_manager.sh

# Messages
ERR_MASTER_ONLY="this command can be launched from the master node only"

# display helper
help() {
	cat <<-EOF
	usage: emr cluster <subcommand>

	  details			display information about the cluster
	  evaluate			evaluate cluster configuration for optimizations
	  execute			run bash script on all slave nodes
	  metrics			display the YARN metrics of the cluster
	  nodes				display cluster topology details
	  reboot			reboot all nodes of the cluster
	  scale				perform scale down/up operations
	  share				copy a file on all slave nodes
	  update			perform security updates on all nodes

	EOF
}

## details
## usage: emr cluster details
##
## Print detailed information about the cluster
##
details() {

	usage_function "cluster" "details" "$*"

	# collect and print cluster core info
	collect_cluster
	details_cluster

	# collect and print cluster network info
	collect_cluster_network
	details_cluster_network

	exit 0
}

## evaluate
## usage: emr cluster evaluate
##
## Evaluate cluster configuration for optimizations
##
evaluate() {

	usage_function "cluster" "evaluate" "$*"

	is_master $ERR_MASTER_ONLY
	cluster_id
	create_report "EMR - $CLUSTER_ID - $(date +'%D %T')"

	# collect data
	collect_cluster
	collect_cluster_network

	collect_nodes_master
	collect_nodes_core
	collect_nodes_tasks

	# recommendations
	eval_cluster
	eval_topology
	eval_networking
	eval_system
	eval_frameworks

	echo
	echo "emr: report stored in $(realpath $REPORT_PATH)"
	echo && exit 0
}

## metrics
## usage: emr cluster metrics
##
## Display YARN metrics of the cluster in JSON format
##
metrics() {

	usage_function "cluster" "metrics" "$*"

	echo $(curl http://localhost:8321/getInstanceMetrics 2> /dev/null)  | jq  '.data | fromjson' && exit 0

}

## nodes
## usage: emr cluster nodes
##
## Print cluster topology details
##
nodes() {

	usage_function "cluster" "nodes" "$*"

	# collect nodes data
	collect_nodes_master && collect_nodes_core && collect_nodes_tasks

	echo
	echo "${bold}MASTER${normal} $nodes_master_id ($nodes_master_number nodes)"
	echo "$delimiter"
	echo "$nodes_master_data" | jq -r 'select(.Status.State=="RUNNING") | (.Ec2InstanceId + "\t" + .PrivateIpAddress + "\t" + .InstanceType + "\t" + .Market)'
	echo && echo
	echo "${bold}CORE${normal} $nodes_core_id ($nodes_core_number nodes)"
	echo "$delimiter"
	echo "$nodes_core_data" | jq -r '. | (.Ec2InstanceId + "\t" + .PrivateIpAddress + "\t" + .InstanceType + "\t" + .Market)'
	echo && echo

	for task_group in "${NODES_TASK_ALL[@]}"; do

		if [[ "$cluster_composition" == "INSTANCE_FLEET" ]]; then
			# EMR Instance Fleet
			group_data=$(echo $nodes_task_data | jq -r --arg TASK "$task_group" 'select(.InstanceFleetId==$TASK) | select(.Status.State=="RUNNING")')
		else
			# EMR Instance Group
			group_data=$(echo $nodes_task_data | jq -r --arg TASK "$task_group" '.Instances[] | select(.InstanceGroupId==$TASK) | select(.Status.State=="RUNNING")')
		fi

		group_task_count=$(echo $group_data | jq -r '.Id' | wc -l)
		echo "${bold}TASK${normal} $task_group ($group_task_count nodes)"
		echo "$delimiter"
		echo "$group_data" | jq -r '(.Ec2InstanceId + "\t" + .PrivateIpAddress + "\t" + .InstanceType + "\t" + .Market)'
		echo && echo

	done
	exit 0
}

## scale
## usage: emr cluster scale NUM [ IG_ID | IF_ID ]
##
## Perform scale down/up operations on the cluster. If no Instance group is specified the scale operation is performed on CORE IG only.
## Requires the elasticmapreduce:ModifyInstanceGroups defined in the EC2 instance role.
##
## Usage example:
##   emr cluster scale 3
##   emr cluster scale 3 ig-xxxxxxxx
##
scale() {

	usage_function "cluster" "scale" "$*"

	group_id="$2"
	instance_count="$1"

	[ -z "$instance_count" ] && error "you need to specify an instance count"

	# retrieve TASK nodes information
	collect_nodes_tasks

	# Check if IG has been passed. if none use the CORE IG
	if [ ! -z "$group_id" ]; then
		contains "$group_id" "${NODES_TASK_ALL[@]}"
		[[ $? -ne 0 ]] && error "$group_id is not a valid instance group" && print_nodes_tasks_groups && exit 2
	else
		# retrieve the CORE IG
		collect_nodes_core
		task_group="$nodes_core_id"
	fi

	if [[ "$cluster_composition" == "INSTANCE_FLEET" ]]; then
		# EMR Instance Fleet
		$AWS_CLI emr modify-instance-fleet --cluster-id $CLUSTER_ID --instance-fleet "{ \"InstanceFleetId\": \"$task_group\", \"TargetOnDemandCapacity\": $instance_count, \"TargetSpotCapacity\": 0 }"
	else
		# EMR Instance Group
		$AWS_CLI emr modify-instance-groups --cluster-id $CLUSTER_ID  --instance-groups "[ { \"InstanceGroupId\": \"$task_group\", \"InstanceCount\": $instance_count } ]"
	fi

	exit 0
}

## share
## usage: emr cluster share <FILE> <S3_TEMP> [DEST_PATH]
##
## Copy a file on all slave nodes (CORE/TASK) of the cluster. If no DEST_PATH path is specified, the FILE is stored within the same path as the source FILE.
##
##  DEST_PATH		Remote path used to store the file
##  S3_TEMP  		S3 Bucket path to temporary store the script
##
## Usage example:
##   emr cluster share /home/hadoop/script.sh s3://BUCKET/PATH
##   emr cluster share /home/hadoop/script.sh s3://BUCKET/PATH /tmp
##
share() {

	usage_function "cluster" "share" "$*"

	file=$1
	s3_temp=$2
	dest_path=$3

	[[ -z "$file" ]] && error "you should define an input file"
	[[ ! -f "$file" ]] && error "$file does not exists."
	[[ -z "$s3_temp" ]] && error "you should define an valid S3 path"
	[[ -z "$dest_path" ]] && dest_path=$(realpath $file)

	# cleaning input
	file_name=$(basename $file)
	s3_path="${s3_temp%/}/$file_name"

	# copy file to temporary s3 bucket
	$AWS_CLI s3 cp $file $s3_path
	[ $? -ne 0 ] && error "could not write $s3_path"

	message "\n${bold}copying${normal} local:$file to remote:$dest_path\n"
	run_cmd_workers "$AWS_CLI s3 cp $s3_path $dest_path"

}

## execute
## usage: emr cluster execute <CMD>
##        emr cluster execute <S3_PATH> [LOCAL_SCRIPT]
##
## Execute a command or bash script on cluster nodes. If no file is specified, the script is created automatically using vim.
##
##  CMD			bash command to execute
##  S3_PATH		S3 PATH used to temporary store the script
##  LOCAL_SCRIPT		Path to the script on the local filesystem
##
## Usage example:
##   emr cluster execute "df -h"
##   emr cluster execute s3://BUCKET/PATH
##   emr cluster execute s3://BUCKET/PATH /home/hadoop/script.sh
##
execute() {

	usage_function "cluster" "execute" "$*"

	# execution configs
	timestamp=$(date +%s)
	rnd_script="/tmp/emr_exec_$timestamp.sh"

	# check inputs
	[[ -z "$1" ]] && error "missing command"
	[[ "$1" == s3* ]] && cmd="sh $rnd_script && rm $rnd_script" || cmd="$1"

	# script execution
	if [[ "$2" == s3* ]]; then
		s3_path=$1
		input_script=$2

		# check if the user submitted a script
		# otherwise open a vim editor
		if [[ -z "$input_script" ]]; then
			input_script="$rnd_script"
			# base script
			cat <<-EOF > "$input_script"
			#!/usr/bin/env bash
			# comment the line below to hide the command executed in the output
			set -x

			EOF

			# start vim editor in insert mode
			vim +4 -c 'startinsert' "$input_script"
		else
			input_script=$(realpath $2)
		fi
		warning "you're going to execute $input_script on all the nodes"
		ask_confirmation

		# Distribute the script on all the nodes
		share "$input_script" "$s3_path" "$rnd_script"
		message "\n${bold}executing${normal} $rnd_script\n"
	else
		warning "you're going to execute \"$cmd\" on all the nodes"
		ask_confirmation
	fi

	# Execute
	run_cmd_all "$cmd"

	[[ -f "$rnd_script" ]] && rm "$rnd_script"

	exit 0
}


## reboot
## usage: emr cluster reboot
##
## Performs a soft reboot of an EMR Cluster. The reboot order is slaves first (CORE/TASK), then master node(s).
## The reboot action can be performed from a master node only.
##
reboot() {

	usage_function "cluster" "reboot" "$*"

	is_master "this action can be performed from a master node only."

	warning "this will restart all cluster nodes"
	ask_confirmation
	experimental "this command might unexpectedly terminate your cluster"
	ask_confirmation

	# soft reboot
	_reboot

	exit 0
}

_reboot() {
	run_cmd_all "sudo reboot"
}

## update
## usage: emr cluster update
##
## Perform security updates on all the nodes of the cluster.
##
update() {

	usage_function "cluster" "update" "$*"

	is_master "cannot launch security updates from a node that is not the master"

	warning "This will install critical updates on all nodes and soft reboot the cluster."
	warning "It's strongly recommended to enable the EMR terminate protection before continue."
	ask_confirmation

	run_cmd_all "sudo yum install -y yum-security"

	#_reboot
}
