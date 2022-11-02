#!/bin/bash

## _run_nodes_cmd
## usage: _run_nodes_cmd <INSTANCES> <CMD>
##
##  INSTANCES		Remote path used to store the file
##  CMD				Bash command to execute
##
_run_nodes_cmd() {

	commands_id=()
	bash_command=$1
	shift
	instance_ids=("$@")

	for node in "${instance_ids[@]}"; do
		cmd_id=$($AWS_CLI ssm send-command --instance-ids $node \
				--document-name "AWS-RunShellScript" \
				--comment "Remote command execution" \
				--parameters commands="$bash_command" \
				--output text --query "Command.CommandId"
		)
		commands_id+=($cmd_id)
	done

	for cmd in "${commands_id[@]}"; do

		node=""
		output=""
		status="InProgress"

		while [ "$status" = "InProgress" ]; do
			result=$($AWS_CLI ssm list-command-invocations --command-id $cmd --details)
			node=$(echo $result | jq -r .CommandInvocations[].InstanceId)
			status=$(echo $result | jq -r .CommandInvocations[].Status)
			output=$(echo $result | jq -r .CommandInvocations[].CommandPlugins[0].Output)
		done

		[ "$status" = "Success" ] && success "$bold$node$normal completed - $cmd\n$output" || failure "$bold$node$normal - $cmd\n$output"

	done
}

## _run_nodes_cmd
## usage: _run_nodes_cmd <INSTANCES> <SERVICE>
##
##  INSTANCES		Remote path used to store the file
##  SERVICE			Service to check
##
_run_check_service() {

	commands_id=()
	service=$1
	shift
	instance_ids=("$@")

	for node in "${instance_ids[@]}"; do
		cmd_id=$($AWS_CLI ssm send-command --instance-ids $node \
				--document-name "AWS-RunShellScript" \
				--comment "Remote command execution" \
				--parameters commands="systemctl is-active --quiet $service status" \
				--output text --query "Command.CommandId"
		)
		commands_id+=($cmd_id)
	done

	for cmd in "${commands_id[@]}"; do

		node=""
		output=""
		status="InProgress"

		while [ "$status" = "InProgress" ]; do
			result=$($AWS_CLI ssm list-command-invocations --command-id $cmd --details)
			node=$(echo $result | jq -r .CommandInvocations[].InstanceId)
			status=$(echo $result | jq -r .CommandInvocations[].Status)
			output=$(echo $result | jq -r .CommandInvocations[].CommandPlugins[0].Output)
		done

		[ "$status" = "Success" ] && up "$bold$node$normal - $service" || down "$bold$node$normal - $service"

	done
}

## run_cmd_node
## usage: run_cmd_node <CMD> <EC2_ID>
##
##  CMD				Bash command to execute
##  EC2_ID			EC2 Instance ID where launch the command
##
run_cmd_node() {
	_run_nodes_cmd "$1" "$2"
}

## run_cmd_masters
## usage: run_cmd_masters <CMD>
##
##  CMD				Bash command to execute
##
run_cmd_masters() {

	cluster_nodes_ec2id
	_run_nodes_cmd "$1" "${MASTERS_EC2ID[@]}"
}

## run_cmd_workers
## usage: run_cmd_workers <CMD>
##
##  CMD				Bash command to execute
##
run_cmd_workers() {

	cluster_nodes_ec2id
	_run_nodes_cmd "$1" "${SLAVES_EC2ID[@]}"
}

## run_cmd_all
## usage: run_cmd_all <CMD>
##
##  CMD				Bash command to execute
##
run_cmd_all() {
	cluster_nodes_ec2id
	_run_nodes_cmd "$1" "${SLAVES_EC2ID[@]}"
	_run_nodes_cmd "$1" "${MASTERS_EC2ID[@]}"
}

## check_service_master
## usage: check_service_master <SERVICE>
##
##  SERVICE			systemd service to check
##
check_service_master() {
	cluster_masters_ec2id
	_run_check_service "$1" "${MASTERS_EC2ID[@]}"
}

## check_service_core
## usage: check_service_core <SERVICE>
##
##  SERVICE			systemd service to check
##
check_service_core() {
	cluster_cores_ec2id
	_run_check_service "$1" "${CORES_EC2ID[@]}"
}

## check_service_slaves
## usage: check_service_slaves <SERVICE>
##
##  SERVICE			systemd service to check
##
check_service_slaves() {
	cluster_nodes_ec2id
	_run_check_service "$1" "${SLAVES_EC2ID[@]}"
}
