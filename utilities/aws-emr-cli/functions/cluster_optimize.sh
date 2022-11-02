#!/bin/bash

#===============================================================================
# EMR Cluster Checks
#===============================================================================

# number of days before we can consider a cluster long running
LONG_RUNNING_THRESHOLD=1

eval_cluster() {
	report_header "Cluster Basic Checks"
	_check_emr_release
	_check_long_running_cluster
	_check_bootstrap_actions
	_check_autoscaling
	_check_aws_tags
}

_check_emr_release() {
	# retrieve the latest release for the current branch used
	emr_latest_release=$($AWS_CLI emr list-release-labels | jq -r .ReleaseLabels[] | cut -c5- | grep ^$cluster_emr_release_major | head -n 1)

	MSG_OK=$(echo "You're already using the latest EMR release ($emr_latest_release) for EMR $cluster_emr_release_major.x")

	MSG_WARN=$(echo "Your cluster is running $cluster_emr_release_num. Please consider upgrading to the latest EMR release for this branch: $emr_latest_release")

	[[ "$cluster_emr_release_num" == "$emr_latest_release" ]] && report_success "$MSG_OK" || report_warning "$MSG_WARN"
}

_check_long_running_cluster() {

	if [[ $cluster_running_days -gt $LONG_RUNNING_THRESHOLD ]]; then

		MSG_OK=$(echo "Termination Protection is enabled")
		MSG_WARN=$(echo "This cluster has been running for $cluster_running_days days and doesn't have Termination Protection enabled. Consider enabling the Termination Protection feature. https://docs.aws.amazon.com/emr/latest/ManagementGuide/UsingEMR_TerminationProtection.html")

		[[ "$cluster_termination_protected" == "true" ]] && report_success "$MSG_OK" || report_warning "$MSG_WARN"


		MSG_OK=$(echo "Multi Master feature enabled")
		MSG_WARN=$(echo "This cluster has been running for $cluster_running_days days. Consider enabling the EMR Multi Master feature. https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-plan-ha-launch.html-")
		[[ "$(is_multimaster)" == "true" ]] && report_success "$MSG_OK" || report_warning "$MSG_WARN"

	else
		report_success "This cluster has been running for $cluster_running_days days. Skipping long running checks (Long Running if running > $LONG_RUNNING_THRESHOLD days)"
	fi
}

_check_autoscaling() {
	if [[ "$cluster_managed_scaling" == "true" ]]; then
		report_success "Managed Scaling is enabled"
	else
		report_warning "Consider enabling Managed Scaling to reduce costs. https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-managed-scaling.html"
	fi
}

_check_aws_tags() {
	[[ ! -z $cluster_tags ]] && success "Cluster is using AWS Tags" || report_warning "The Cluster does not have any AWS Tag associated. Consider adding AWS Tags for auditing purposes. https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-plan-tags.html"
}

_check_bootstrap_actions() {
	BA_THRESHOLD=2
	BA_LONG_THRESHOLD=60 # seconds

	if [[ $cluster_ba_count -gt 0 ]]; then

		[[ $cluster_ba_count -le $BA_THRESHOLD ]] && report_success "You have only $cluster_ba_count Bootstrap Actions attached to the cluster" || report_warning "You have $cluster_ba_count Bootstrap Actions attached to the cluster. Consider using a Custom AMI with needed changes to improve startup time. https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-custom-ami.html"

		# check if BA logs have been deleted by logpusher
		cat /var/log/bootstrap-actions/**/controller &>/dev/null
		if [[ $? -eq 0 ]]; then
			[[ $cluster_ba_longest -le $BA_LONG_THRESHOLD ]] && report_success "Your longest Boostrap Action took $cluster_ba_longest seconds to complete." || report_warning "Your longest Boostrap Action took $cluster_ba_longest seconds to complete. Consider using a Custom AMI with needed changes to improve startup time. https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-custom-ami.html"
		else
			report_warning "Can't process Bootstrap Logs. Logs have been already pushed to S3 and deleted from the node."
		fi
	else
		report_success "No Bootstrap Action detected"
	fi
}

#===============================================================================
# Cluster Topology Checks
#===============================================================================
# min number of CORE nodes for a production cluster
CORE_NUM_THRESHOLD=2

eval_topology() {
	report_header "Cluster Topology Checks"
	_check_nodes_master
	_check_nodes_core
	_check_instances
	_check_nodes_disks
}

_check_nodes_master() {
	[[ "$nodes_master_market" == "ON_DEMAND" ]] && report_success "MASTER nodes are running as ON DEMAND instances" || report_warning "MASTER nodes are running on $nodes_core_market instances. Consider using only ON DEMAND instances for MASTER nodes."
}

_check_nodes_core() {
	[[ $nodes_core_number -gt $CORE_NUM_THRESHOLD ]] && report_success "There are at least $CORE_NUM_THRESHOLD CORE nodes in the cluster" || report_warning "You're using only $nodes_core_number CORE nodes. If this is a production cluster consider using a minimum of $CORE_NUM_THRESHOLD CORE nodes to increase resiliency to HDFS issues."

	[[ "$nodes_core_market" == "ON_DEMAND" ]] && report_success "CORE nodes are running as ON DEMAND instances" || report_warning "CORE nodes are running on $nodes_core_market instances. Consider using only ON DEMAND instances for CORE nodes."
}

_check_nodes_tasks() {
	echo
}

_check_instances() {

	# check if using graviton instances
	graviton_instances=$(echo $INSTANCES_DATA | jq -r '.Instances[] | select(.Status.State=="RUNNING") | select(.InstanceType | contains("6g"))')

	[[ ! -z  $graviton_instances ]] && report_success "Cluster using Graviton instances" || report_warning "Cluster not using Graviton instances. Consider switching to reduce costs and increase performance"

	# check if cluster is using instance types of different types
	i_families=$(echo $INSTANCES_DATA | jq -r '.Instances[] | select(.Status.State=="RUNNING") | .InstanceType' | uniq | awk -F. '{ print $1 }' | cut -c 1-1 | uniq | wc -l)

	[[ $i_families -gt 1 ]] && report_warning "Cluster using different families. If this is a transient cluster running a single job in parallel, you might not be able to fully utilize all cluster resources. For this specific use cases select a single instance family." || report_success "Cluster using a single instance family"
}

_check_nodes_disks() {

	declare -A instance_disk
	instance_disk=( ['large']="1" ['xlarge']="2" ['2xlarge']="4" ['4xlarge']="4" ['8xlarge']="4" ['9xlarge']="4" ['10xlarge']="4" ['12xlarge']="4" ['16xlarge']="4" ['18xlarge']="4" ['24xlarge']="4" )

	volumes_data=$(echo $INSTANCES_DATA | jq '.Instances[] | select(.Status.State=="RUNNING") | { Id: (if has("InstanceGroupId") then .InstanceGroupId else .InstanceFleetId end), InstanceType: .InstanceType, State: .Status.State, EbsVolumes: (.EbsVolumes|length)}' | jq -s '. | unique')

	readarray -t tmp_instances < <(echo $volumes_data | jq -r '.[].Id')
	for i in "${tmp_instances[@]}"; do

		tmp_data=$(echo $volumes_data | jq --arg I_ID "$i" '.[] | select(.Id == $I_ID)')

		i_type=$(echo $tmp_data | jq -r '.InstanceType')
		i_family=$(echo $i_type | awk -F. '{ print $1 }')
		i_size=$(echo $i_type | awk -F. '{ print $2 }')
		i_disks=$(echo $tmp_data | jq -r '.EbsVolumes')

		[[ $i_disks -ge "${instance_disk[$i_size]}" ]] && report_success "$i - $i_type has $i_disks volumes attached" || report_warning "$i - $i_type has $i_disks volumes attached. Consider using ${instance_disk[$i_size]} to increase performance"

	done

}

#===============================================================================
# Cluster Networking Checks
#===============================================================================
IP_THRESHOLD=20

eval_networking() {
	report_header "Cluster Networking Checks"
	_check_subnet_ip
	_check_s3_gw
}

_check_subnet_ip() {
	[[ $subnet_available_ip -lt $IP_THRESHOLD ]] && report_warning "Your subnet has only $subnet_available_ip available IP. Consider increasing the subnet mask" || report_success "You have $subnet_available_ip available IP"
}

_check_s3_gw() {
	[[ "$subnet_has_s3_gw" == "true" ]] && report_success "Your subnet has an S3 Gateway Endpoint" || report_warning "Your subnet doesn't have an S3 Gateway Endpoint. Consider adding one to improve performance. https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html"
}

#===============================================================================
# Master Node Checks
#===============================================================================
MEM_THRESHOLD=85

eval_system() {
	report_header "Cluster Master Node Checks"
	_check_memory
}

_check_memory() {
	used_mem_perc=$(free | grep Mem | awk '{print $3/$2 * 100.0}')
	[[ $(echo "if (${used_mem_perc} > $MEM_THRESHOLD) 1 else 0" | bc) -eq 1 ]] && report_warning "Your memory utilization is greater than $MEM_THRESHOLD%. Consider using an instance with more available memory" || report_success "Memory utilization is lower than $MEM_THRESHOLD%"

	oom_count=$(sudo dmesg | grep oom_reaper | wc -l)
	[[ $oom_count -gt 0 ]] && report_warning "Detected $oom_count Out Of Memory issues. Some processes were killed by the kernel due to the lack of memory. Consider using an instance with more RAM" || report_success "No Out Of Memory issues detected"

}

#===============================================================================
# Application Frameworks Checks
#===============================================================================

eval_frameworks() {
	report_header "Cluster Frameworks Checks"
	_check_hdfs
}

# HDFS
_check_hdfs() {

	HDFS_USAGE_PERCENTAGE=80 # 80%
	HDFS_USAGE_BYTES=536870912000 # 500GB

	cluster_hdfs_replication=$(hdfs getconf -confKey dfs.replication 2>/dev/null)
	[[ $cluster_hdfs_replication -gt 1 ]] && report_success "You have a minimal HDFS replication factor of $cluster_hdfs_replication" || report_warning "You have a minimal HDFS replication factor of $cluster_hdfs_replication. Consider increasing it, by tuning the hdfs-site configuration dfs.replication. For more details, see https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-hdfs-config.html"

	[[ $cluster_hdfs_replication -le $nodes_core_number ]] && report_success "You have sufficient number of CORE nodes to replicate HDFS blocks" || report_warning "You have a minimal HDFS replication factor of $cluster_hdfs_replication, which is greater than the number of CORE nodes ($nodes_core_number). You will not be able to replicate your data. Consider increasing the number of CORE nodes at least to $cluster_hdfs_replication"

	hdfs_utilization=$(hdfs dfsadmin -report 2> /dev/null | sed -nr "s/DFS Used%: (.*)%$/\1/p" | head -n 1)
	[[ $(echo "$hdfs_utilization < $HDFS_USAGE_PERCENTAGE" |bc -l) ]] && report_success "HDFS utilization is $hdfs_utilization%, which is lower than alert threshold $HDFS_USAGE_PERCENTAGE%" || report_warning "HDFS utilization is greater than $HDFS_USAGE_PERCENTAGE%. Consider adding more CORE nodes before running out of space"

	hdfs_utilization=$(hdfs dfs -du -s / 2> /dev/null | awk '{print $1}')
	[[ $(echo "$HDFS_USAGE_BYTES - $hdfs_utilization" | bc -l) -gt 0 ]] && report_success "HDFS utilization pattern low ( usage < 500 GB)" || report_warning "HDFS utilization pattern high ( usage > 500 GB). Consider storing your data on Amazon S3"

	under_replica_files=$(hdfs fsck / 2>/dev/null | grep 'Under replicated' | awk -F':' '{print $1}' | wc -l)
	[[ $under_replica_files -eq 0 ]] && report_success "There are no files with under replicated blocks" || report_warning "You have $under_replica_files files with under replicated blocks. Use the 'emr hdfs' command to generate a detailed report"

}
