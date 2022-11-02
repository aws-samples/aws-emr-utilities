#!/bin/bash

# required libraries
source $DIR/../functions/cluster_data.sh
source $DIR/../functions/system_manager.sh

# Display usage helper
help() {
	cat <<-EOF
	usage: emr hdfs <subcommand>

	  balancer			re-balance HDFS blocks across datanodes
	  configs			display main HDFS configurations
	  fs-image			generate XML report of the NN fsImage
	  ls				file listing with auto-completion
	  namenode			return the hostname of the active namenode
	  report			generate HDFS dfsadmin report
	  safe-mode			enter or leave the HDFS safe-mode
	  secondary			return the hostname of the secondary namenode
	  status			check the status of HDFS deamons in the cluster
	  under-replica			analyze and replicate under-replicated files
	  usage				details on filesystem utilization

	EOF
}

## balancer
## usage: emr hdfs balancer [BANDWIDTH]
##
## Launch the balancer process to re-balance HDFS blocks across datanodes.
##
##   BANDWIDTH            maximum network bandwidth, a DataNode can use during
##                        the balancing operation. Format: n<MB?|GB?>
##
## Usage example:
##
##   emr hdfs balancer               # Launch balancer
##   emr hdfs balancer 10mb          # Launch balancer with 10 MB/s x datanode
##   emr hdfs balancer 1G            # Launch balancer with 1 GB/s x datanode
##
balancer() {

	usage_function "hdfs" "balancer" "$*"

	BANDWIDTH=${1,,}

	[[ (! -z $BANDWIDTH) && (! "$BANDWIDTH" =~ [0-9]+[m,g]b? ) ]] && error "$BANDWIDTH is not properly defined"

	[[ ${BANDWIDTH//[[:digit:]]/} =~ mb? ]] && BANDWIDTH=$((1024*1024*${BANDWIDTH/[a-z]*/}))
	[[ ${BANDWIDTH//[[:digit:]]/} =~ gb? ]] && BANDWIDTH=$((1024*1024*1024*${BANDWIDTH/[a-z]*/}))

	# maximum amount of network bandwidth, in bytes per second used by datanodes
	hdfs dfsadmin -setBalancerBandwidth $BANDWIDTH
	# run balancer on the cluster
	hdfs balancer 2>/dev/null && exit 0
}

## configs
## usage: emr hdfs configs
##
## Displays main HDFS configurations.
##
configs() {

	usage_function "hdfs" "configs" "$*"
	echo
	print_k_v "fs.defaultFS" "$(hdfs getconf -confKey fs.defaultFS 2>/dev/null)"
	print_k_v "dfs.replication" "$(hdfs getconf -confKey dfs.replication 2>/dev/null)"
	print_k_v "dfs.replication.max" "$(hdfs getconf -confKey dfs.replication.max 2>/dev/null)"
	print_k_v "dfs.blocksize" "$(hdfs getconf -confKey dfs.blocksize 2>/dev/null)"
	echo
	print_k_v "dfs.namenode.name.dir" "$(hdfs getconf -confKey dfs.namenode.name.dir 2>/dev/null)"
	print_k_v "dfs.namenode.checkpoint.edits.dir" "$(hdfs getconf -confKey dfs.namenode.checkpoint.edits.dir 2>/dev/null)"
	print_k_v "dfs.hosts.exclude" "$(hdfs getconf -confKey dfs.hosts.exclude 2>/dev/null)"
	echo && exit 0
}

## fs-image
## usage: emr hdfs fs-image
##
## Generate an XML report of the fsImage file.
## Save current namespace into storage directories and reset edits log.
## The HDFS will be put in SAFE MODE to write all the edit logs.
##
fs-image() {

	usage_function "hdfs" "fs-image" "$*"

	is_master "cannot generate fsImage dump from a node that is not MASTER"

	timestamp=$(date +%s)
	logfile="$LOG_PATH/hdfs-fsImage-$timestamp.xml"
	fsImage_dir="/mnt/namenode/current/"
	fsImage="$(sudo ls $fsImage_dir | grep fsimage | sort | tail -n2 | head -n1)"

	warning "This will put the HDFS in safe mode."
	warning "During this period HDFS will be accessible only in read-only mode."
	ask_confirmation

	hdfs dfsadmin -safemode enter 2>/dev/null
	hdfs dfsadmin -saveNamespace 2>/dev/null
	sudo hdfs oiv -i "$fsImage_dir/$fsImage" -o "$logfile" -p XML 2>/dev/null
	hdfs dfsadmin -safemode leave 2>/dev/null
	message "dump file written in ${bold}$logfile${normal}" && exit 0
}

## ls
## usage: emr hdfs ls [PATH]
##
## HDFS listing with path auto-completion.
## Uses `hadoop-httpfs.service` service ## to provide auto-completion features.
##
ls() {
	usage_function "hdfs" "ls" "$*"

	systemctl is-active --quiet hadoop-httpfs.service status
	[[ $? -ne 0 ]] && warning "hadoop-httpfs.service not running." && warning "start the service to enable auto completion feature"

	[[ -z $1 ]] && hdfs dfs -ls -h / 2>/dev/null && exit 0
	hdfs dfs -ls -h $1 2>/dev/null && exit 0
}

## namenode
## usage: emr hdfs namenode
##
## Return the hostname of the active namenode.
##
namenode() {
	usage_function "hdfs" "namenode" "$*"
	hdfs getconf -namenodes 2>/dev/null && exit 0

}

## report
## usage: emr hdfs report
##
## Generate the HDFS dfsadmin report.
##
report() {
	usage_function "hdfs" "report" "$*"
	hdfs dfsadmin -report 2>/dev/null && exit 0
}

## safe-mode
## usage: emr hdfs safe-mode <on|off>
##
## Toggle the HDFS safe-mode. When safe-mode is active, you can only use the
## HDFS in read-only mode.
##
safe-mode() {

	usage_function "hdfs" "safe-mode" "$*"

	cmd=${1,,}
	[[ -z "$cmd" ]] && error "you must specify a status [on|off]" && exit 2
	[[ "$cmd" != "on" && "$cmd" != "off" ]] && error "you must specify a valid status [on|off]" && exit 2
	[ "$cmd" == "on" ] && warning "HDFS will enter in a read-only state" && ask_confirmation
	[ "$cmd" == "on" ] && hdfs dfsadmin -safemode enter 2>/dev/null || hdfs dfsadmin -safemode leave 2>/dev/null && exit 0
}

## secondary
## usage: emr hdfs secondary
##
## Return the hostname of the secondary namenode (requires EMR multi master).
##
secondary() {
	usage_function "hdfs" "secondary" "$*"
	hdfs getconf -secondaryNameNodes 2>/dev/null && exit 0
}

## status
## usage: emr hdfs status
##
## Check the status of HDFS deamons in the cluster.
##
status() {
	usage_function "hdfs" "status" "$*"

    header "Master"

	check_service_master "hadoop-hdfs-namenode"
	check_service_master "hadoop-httpfs"

	if [[ $(is_multimaster) == "true" ]]; then
		check_service_master "hadoop-hdfs-journalnode"
		check_service_master "hadoop-hdfs-zkfc"
	fi

	header "Core"
	check_service_core "hadoop-hdfs-datanode"

	echo && exit 0
}

## under-replica
## usage: emr hdfs under-replica
##
## Generate a list of under-replicated blocks on HDFS and tries to fix them.
##
under-replica() {
	LIMIT=20

	usage_function "hdfs" "under-replica" "$*"

	timestamp=$(date +%s)
	logfile="$LOG_PATH/hdfs-block-replica.$timestamp.log"

	hdfs fsck / 2>/dev/null | grep 'Under replicated' | awk -F':' '{print $1}' >>$logfile
	message "report written in ${bold}$logfile${normal}"

	under_replicated_files=$(cat $logfile | wc -l)
	if [[ $under_replicated_files -gt 0 ]]; then
		warning "There are $under_replicated_files files under replicated"
		warning "Under replica file list (first $LIMIT)"
		cat $logfile
		echo
		# retrieve current replica value
		curr_replica=$(hdfs getconf -confKey dfs.replication)
		warning "Would like to repair those files?"
		warning "This will set the default replication factor: $curr_replica"
		ask_confirmation

		for f in $(cat "$logfile"); do
			hadoop fs -setrep "$curr_replica" "$f"
		done
	else
		success "No under replicated blocks detected"
	fi

}

## usage
## usage: emr hdfs usage
##
## Details on filesystem utilization
##
usage() {
	usage_function "hdfs" "usage" "$*"
	hdfs dfs -df -h && exit 0
}

#===============================================================================
# Helper Functions
#===============================================================================
