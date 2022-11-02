#!/bin/bash

######################################
# Cluster BASE
######################################

# Retrieve cluster details
collect_cluster() {

	cluster_data
	managed_scaling_data

	# base details
	cluster_id=$(echo $CLUSTER_DATA | jq -r .Cluster.Id)
	cluster_name=$(echo $CLUSTER_DATA | jq -r .Cluster.Name)

	# retrieve current emr release used along with major version
	cluster_emr_release=$(echo $CLUSTER_DATA | jq -r .Cluster.ReleaseLabel)
	cluster_emr_release_num=${cluster_emr_release#emr-}
	cluster_emr_release_major=${cluster_emr_release_num:0:1}

	cluster_composition=$(echo $CLUSTER_DATA | jq -r .Cluster.InstanceCollectionType)

	cluster_is_multimaster=$(is_multimaster)
	cluster_auto_terminate=$(echo $CLUSTER_DATA | jq -r .Cluster.AutoTerminate)
	cluster_termination_protected=$(echo $CLUSTER_DATA | jq -r .Cluster.TerminationProtected)
	cluster_scale_down_behaviour=$(echo $CLUSTER_DATA | jq -r .Cluster.ScaleDownBehavior)

	# creation time
	cluster_creation_time=$(echo $CLUSTER_DATA | jq -r .Cluster.Status.Timeline.CreationDateTime)
	cluster_creation_date=$(echo $cluster_creation_time | { read gmt ; date -d "$gmt" ; })
	cluster_creation_epoch=$(date -d $cluster_creation_time +"%s")

	# ready time
	cluster_ready_time=$(echo $CLUSTER_DATA | jq -r .Cluster.Status.Timeline.ReadyDateTime)
	cluster_ready_date=$(echo $cluster_ready_time | { read gmt ; date -d "$gmt" ; })
	cluster_ready_epoch=$(date -d $cluster_ready_time +"%s")

	# compute time
	cluster_startup_sec=$(echo "$cluster_ready_epoch - $cluster_creation_epoch" | bc)
	cluster_startup_time=$(eval "echo $(date -d@"$cluster_startup_sec" +'$((%s/3600/24)) days %H hours %M minutes %S seconds')")

	cluster_running_sec=$(echo "$(date +%s) - $cluster_creation_epoch" | bc -l)
	cluster_running_time=$(eval "echo $(date -d@"$cluster_running_sec" +'$((%s/3600/24)) days %H hours %M minutes %S seconds')")
	cluster_running_days=$(eval "echo $(date -d@"$cluster_running_sec" +'$((%s/3600/24))')")

	# cluster status
	cluster_status=$(echo $CLUSTER_DATA | jq -r .Cluster.Status.State)
	cluster_last_state=$(echo $CLUSTER_DATA | jq -r .Cluster.Status.StateChangeReason.Message)

	# logs
	cluster_logs_encrypted=$(echo $CLUSTER_DATA | jq '.Cluster | has("LogEncryptionKmsKeyId")')
	cluster_logs_encryption_key=$(echo $CLUSTER_DATA | jq -r '.Cluster.LogEncryptionKmsKeyId')
	cluster_log_path=$(echo $CLUSTER_DATA | jq -r .Cluster.LogUri)

	cluster_apps=$(echo $CLUSTER_DATA | jq -r '.Cluster.Applications[] | join(", ")')
	cluster_apps_formatted=$(echo $CLUSTER_DATA | jq -r '.Cluster.Applications[] | (.Name|tostring) + " (" + (.Version|tostring) + ")"' | awk '{print}' ORS=', ')

	cluster_tags=$(echo $CLUSTER_DATA | jq -r '.Cluster.Tags[] | {Key: .Key, Value: .Value} | join(", ")')
	cluster_tags_formatted=$(echo $CLUSTER_DATA | jq -r '.Cluster.Tags[] | (.Key|tostring) + " -> " + (.Value|tostring)' | awk '{print}' ORS=', ')

	# Bootstrap Actions
	cluster_ba_count=$(echo $CLUSTER_DATA | jq -r '.[].BootstrapActions[].ScriptPath' | wc -l)
	cluster_ba_longest=$(cat /var/log/bootstrap-actions/**/controller 2> /dev/null | grep "process run time: " | sed -r 's/.*process run time: ([0-9]+) seconds/\1/' | uniq | sort | head -n 1)
	[[ -z $cluster_ba_longest ]] && cluster_ba_longest=0

}

collect_cluster_network() {

	[[ -z "$CLUSTER_DATA" ]] && collect_cluster

	# Availability Zone
	availability_zone=$(echo $CLUSTER_DATA | jq -r .Cluster.Ec2InstanceAttributes.Ec2AvailabilityZone)

	# subnet
	subnet_id=$(echo $CLUSTER_DATA | jq -r .Cluster.Ec2InstanceAttributes.Ec2SubnetId)
	subnet_data=$($AWS_CLI ec2 describe-subnets --subnet-ids $subnet_id)
	subnet_cidr=$(echo $subnet_data | jq -r .Subnets[].CidrBlock)
	subnet_available_ip=$(echo $subnet_data | jq -r .Subnets[].AvailableIpAddressCount)

	# vpc
	vpc_id=$(echo $subnet_data | jq -r .Subnets[].VpcId)
	vpc_data=$($AWS_CLI ec2 describe-vpcs --vpc-ids $vpc_id)
	vpc_dhcp_id=$(echo $vpc_data | jq -r .Vpcs[].DhcpOptionsId)
	dhcp_data=$($AWS_CLI ec2 describe-dhcp-options --dhcp-options-ids $vpc_dhcp_id)

	# dns
	dns=$(echo $dhcp_data | jq -r '.DhcpOptions[].DhcpConfigurations[] | select(.Key=="domain-name-servers") | .Values[].Value')
	domain_name=$(echo $dhcp_data | jq -r '.DhcpOptions[].DhcpConfigurations[] | select(.Key=="domain-name") | .Values[].Value')

	# master public dns
	master_public_hostname=$(echo $CLUSTER_DATA | jq -r .Cluster.MasterPublicDnsName)

	# S3 GW endpoint
	subnet_gw_endpoints=$($AWS_CLI ec2 describe-route-tables --filters Name=association.subnet-id,Values=subnet-034c7258 | jq -r '.RouteTables[0].Routes[]| select (.DestinationPrefixListId) | .GatewayId')

	if [[ ! -z $subnet_gw_endpoints ]]; then
		vpc_s3_gw=$($AWS_CLI ec2 describe-vpc-endpoints --vpc-endpoint-ids $(echo $subnet_gw_endpoints | tr '\n' ' ') | jq -r '.VpcEndpoints[] | select (.ServiceName | contains ("s3")) | .VpcEndpointId')

		if [[ ! -z $vpc_s3_gw ]]; then
			subnet_has_s3_gw="true"
		else
			subnet_has_s3_gw="false"
			vpc_s3_gw="none"
		fi
	else
		subnet_has_s3_gw="false"
		vpc_s3_gw="none"
	fi

}

######################################
# Cluster NODES
######################################

# Retrieve details for MASTER nodes
collect_nodes_master() {

	cluster_data
	instances_data

	cluster_composition=$(echo $CLUSTER_DATA | jq -r .Cluster.InstanceCollectionType)

	if [[ "$cluster_composition" == "INSTANCE_FLEET" ]]; then
		# EMR Instance Fleet
		nodes_master_id=$(echo $CLUSTER_DATA | jq -r '.Cluster.InstanceFleets[] | select(.InstanceFleetType | contains("MASTER")) | .Id')
		nodes_master_data=$(echo $INSTANCES_DATA | jq -r --arg MASTER_ID "$nodes_master_id" '.Instances[] | select(.InstanceFleetId==$MASTER_ID) | select(.Status.State=="RUNNING")')

		market_num=$(echo $nodes_master_data | jq -r .Market | uniq |wc -l)
		if [[ $market_num -eq 1 ]]; then
			nodes_master_market=$(echo $nodes_master_data | jq -r .Market | uniq)
		else
			# mixed (ON DEMAND + SPOT) - In case we add this in the future
			nodes_master_market="MIXED (ON DEMAND + SPOT)"
		fi

	else
		# EMR Instance Group
		nodes_master_id=$(echo $CLUSTER_DATA | jq -r '.Cluster.InstanceGroups[] | select(.InstanceGroupType | contains("MASTER")) | .Id')
		nodes_master_data=$(echo $INSTANCES_DATA | jq -r --arg MASTER_ID "$nodes_master_id" '.Instances[] | select(.InstanceGroupId==$MASTER_ID) | select(.Status.State=="RUNNING")')
		nodes_master_market=$(echo $nodes_master_data | jq -r .Market | uniq)
	fi

	# common section
	nodes_master_number=$(echo "$nodes_master_data" | jq -r 'select(.Status.State=="RUNNING") | .Id' | wc -l)

}

# Retrieve details for CORE nodes
collect_nodes_core() {

	cluster_data
	instances_data

	cluster_composition=$(echo $CLUSTER_DATA | jq -r .Cluster.InstanceCollectionType)

	if [[ "$cluster_composition" == "INSTANCE_FLEET" ]]; then
		# EMR Instance Fleet
		nodes_core_id=$(echo $CLUSTER_DATA | jq -r '.Cluster.InstanceFleets[] | select(.InstanceFleetType | contains("CORE")) | .Id')
		nodes_core_data=$(echo $INSTANCES_DATA | jq -r --arg CORE_ID "$nodes_core_id" '.Instances[] | select(.InstanceFleetId==$CORE_ID) | select(.Status.State=="RUNNING")')

		market_num=$(echo $nodes_core_data | jq -r .Market | uniq |wc -l)
		if [[ $market_num -eq 1 ]]; then
			nodes_core_market=$(echo $nodes_core_data | jq -r .Market | uniq)
		else
			# mixed (ON DEMAND + SPOT)
			nodes_core_market="MIXED (ON DEMAND + SPOT)"
		fi

	else
		# EMR Instance Group
		nodes_core_id=$(echo $CLUSTER_DATA | jq -r '.Cluster.InstanceGroups[] | select(.InstanceGroupType | contains("CORE")) | .Id')
		nodes_core_data=$(echo $INSTANCES_DATA | jq -r --arg CORE_ID "$nodes_core_id" '.Instances[] | select(.InstanceGroupId==$CORE_ID) | select(.Status.State=="RUNNING")')
		nodes_core_market=$(echo $nodes_core_data | jq -r .Market | uniq)
	fi

	# common section
	nodes_core_number=$(echo "$nodes_core_data" | jq -r 'select(.Status.State=="RUNNING") | .Id' | wc -l)

}

# Retrieve details for TASK nodes
collect_nodes_tasks() {

	cluster_data
	instances_data

	cluster_composition=$(echo $CLUSTER_DATA | jq -r .Cluster.InstanceCollectionType)

	if [[ "$cluster_composition" == "INSTANCE_FLEET" ]]; then
		# EMR Instance Fleet
		nodes_task_id=$(echo $CLUSTER_DATA | jq -r '.Cluster.InstanceFleets[] | select(.InstanceFleetType | contains("TASK")) | .Id')

		nodes_task_data=$(echo $INSTANCES_DATA | jq -r --arg TASK_ID "$nodes_task_id" '.Instances[] | select(.InstanceFleetId==$TASK_ID) | select(.Status.State=="RUNNING")')

		readarray -t NODES_TASK_ALL < <(echo $nodes_task_data | jq -r '[.InstanceFleetId] | .[]'| uniq)

	else
		# EMR Instance Group
		readarray -t NODES_TASK_ALL < <(echo $CLUSTER_DATA | jq -r '.Cluster.InstanceGroups[] | select(.InstanceGroupType | contains("TASK")) | .Id ' | uniq)

		nodes_task_data=$INSTANCES_DATA

	fi

}

######################################
# TASK GROUPS Helper
######################################
print_nodes_tasks_groups() {
	collect_nodes_tasks
	for task_group in "${NODES_TASK_ALL[@]}"; do
		echo $task_group
	done
}

######################################
# Cluster EC2 IDs for System Manager
######################################

function cluster_nodes_ec2id() {
	collect_nodes_master
	readarray -t MASTERS_EC2ID < <(echo $nodes_master_data | jq -r '.Ec2InstanceId')

	if [[ "$cluster_composition" == "INSTANCE_FLEET" ]]; then
		# EMR Instance Fleet
		readarray -t SLAVES_EC2ID < <(echo $INSTANCES_DATA | jq -r --arg MASTER_ID "$nodes_master_id" '.Instances[] | select(.InstanceFleetId!=$MASTER_ID and .Status.State=="RUNNING") | .Ec2InstanceId')
	else
		# EMR Instance Group
		readarray -t SLAVES_EC2ID < <(echo $INSTANCES_DATA | jq -r --arg MASTER_ID "$nodes_master_id" '.Instances[] | select(.InstanceGroupId!=$MASTER_ID and .Status.State=="RUNNING") | .Ec2InstanceId')
	fi

}

function cluster_masters_ec2id() {
	collect_nodes_master
	readarray -t MASTERS_EC2ID < <(echo $nodes_master_data | jq -r '.Ec2InstanceId')
}

function cluster_cores_ec2id() {
	collect_nodes_core
	readarray -t CORES_EC2ID < <(echo $nodes_core_data | jq -r '.Ec2InstanceId')
}
