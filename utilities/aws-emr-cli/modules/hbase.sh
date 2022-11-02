#!/bin/bash

# required libraries
source $DIR/../functions/cluster_data.sh
source $DIR/../functions/system_manager.sh

# core configs
HBASE_CMD="sudo -u hbase hbase"
LIMIT="20"
ERROR_MISSING_TABLE="missing table name"

# Module display helper
help() {
	cat <<-EOF
	usage: emr hbase <subcommand>

	  check				consistency check using hbck
	  disable			disable HBase tables
	  enable			enable HBase tables
	  ns				print the namespaces and namespace's tables
	  regions			print all the regions of a table
	  restart			perform a rolling restart of HBase
	  shell				start hbase shell using hbase user
	  status			check the status of HBase deamons in the cluster

	EOF
}

## check
## usage: emr hbase check [TABLE]
##
## Perform consistency check using the hbck utility.
##
##   TABLE            HBase table name. For tables not in the default ns use:
##                    namespace:table_name
##
## Usage example:
##   emr hbase check                    # consistency check for all tables
##   emr hbase check TABLE              # consistency check for a single table
##
check() {

	usage_function "hbase" "check" "$*"

	is_installed "HBase"

	table="$1"
	[[ -z "$table" ]] && $HBASE_CMD hbck -details || $HBASE_CMD hbck -details "$table" 2>/dev/null
	exit 0
}

## disable
## usage: emr hbase disable [TABLE]
##
## Disable a specific HBase table when the TABLE name is specified, or all the
## tables when no paramenter is provided.
##
##   TABLE            HBase table name. For tables not in the default ns use:
##                    namespace:table_name
##
## Usage example:
##   emr hbase disable                    # disable all HBase tables
##   emr hbase disable TABLE              # disable single HBase table
##
disable() {
	usage_function "hbase" "disable" "$*"
	is_installed "HBase"

	format="$1"
	if [ -z $format ]; then
		warning "This will disable all HBase tables"
		ask_confirmation
		echo -e "disable_all '.*'\ny" | $HBASE_CMD shell -n
	else
		warning "This will disable: $format"
		ask_confirmation
		echo -e "disable '$format'\n" | $HBASE_CMD shell -n
	fi

	exit 0
}

## enable
## usage: emr hbase enable [TABLE]
##
## Enable a specific HBase table when the TABLE name is specified, or all the
## tables when no paramenter is provided.
##
##   TABLE            HBase table name. For tables not in the default ns use:
##                    namespace:table_name
##
## Usage example:
##   emr hbase enable                    # enable all HBase tables
##   emr hbase enable TABLE              # enable single HBase table
##
enable() {
	usage_function "hbase" "enable" "$*"
	is_installed "HBase"

	format="$1"
	if [ -z $format ]; then
		warning "This will enable all HBase tables"
		ask_confirmation
		echo -e "enable_all '.*'\ny" | $HBASE_CMD shell -n
	else
		echo -e "enable '$format'\n" | $HBASE_CMD shell -n
	fi

	exit 0
}

## ns
## usage: emr hbase ns [NS]
##
## Return HBase namespaces, or tables within the specified namespace.
##
##   NS            HBase namespace.
##
## Usage example:
##   emr hbase ns                    # list HBase namespaces
##   emr hbase ns NS                 # list tables in the namespace
##
ns() {

	usage_function "hbase" "ns" "$*"

	is_installed "HBase"

	ns="$1"
	if [[ -z "$ns" ]]; then
		echo "list_namespace" | $HBASE_CMD shell -n 2>/dev/null
	else
		echo "list_namespace_tables '$ns'" | $HBASE_CMD shell -n 2>/dev/null
	fi
	exit 0
}

## regions
## usage: emr hbase regions <TABLE>
##
## Retrun regions for the specified table.
##
##   TABLE            HBase table name. For tables not in the default ns use:
##                    namespace:table_name
##
regions() {

	usage_function "hbase" "regions" "$*"
	is_installed "HBase"

	table=$1
	[[ -z "$table" ]] && error "$ERROR_MISSING_TABLE"

	header "HBase Regions - $table"
	echo "list_regions '$table'" | $HBASE_CMD shell -n 2>/dev/null
	exit 0
}

## restart
## usage: emr hbase restart
##
## Performs a rolling restart of HBase, followed by a consistency check.
## Before restarting the service, it first disable all the tables.
##
restart() {

	usage_function "hbase" "restart" "$*"

	is_installed "HBase"
	warning "this will restart the HBase services on all the nodes."
	disable

	# Restart Master
	run_cmd_masters "sudo systemctl stop hbase-master; sudo systemctl start hbase-master"

	# Restart Regions
	run_cmd_workers "sudo systemctl stop hbase-regionserver; sudo systemctl start hbase-regionserver"

	# Restart HMaster again to clear dead servers list
	run_cmd_masters "sudo systemctl stop hbase-master; sudo systemctl start hbase-master"
}

## shell
## usage: emr hbase shell
##
## Start hbase shell using the `hbase` user.
##
shell() {
	usage_function "hbase" "shell" "$*"
	is_installed "HBase"
	$HBASE_CMD shell && exit 0
}

## status
## usage: emr hbase status
##
## Check the status of HBase deamons in the cluster.
##
status() {
	usage_function "hbase" "status" "$*"
	is_installed "HBase"

	header "Master"

	check_service_master "hbase-master"
	check_service_master "hbase-rest"
	check_service_master "hbase-thrift"

	header "Core & Task"
	check_service_slaves "hbase-regionserver"

	echo && exit 0
}

#===============================================================================
# Helper Functions
#===============================================================================
