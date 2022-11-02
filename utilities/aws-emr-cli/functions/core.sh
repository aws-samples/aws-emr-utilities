#!/bin/bash

# Colors
R='\033[0;31m'
G='\033[0;32m'
Y='\033[0;33m'
W='\033[0;37m'

bold=$(tput bold)
normal=$(tput sgr0)
delimiter="=================================================================="

# Terminal settings
export TERM="xterm-color"
export REPORT_PATH="$LOG_PATH/emr-optimizations.txt"

######################################
# Commons helpers
######################################

# Check if an array contain a string
contains() {
	local e match="$1"
	shift
	for e; do [[ "$e" == "$match" ]] && return 0; done
	return 1
}

# Check if the user has the permission to execute the script
function check_user() {
	MSG="You should use the $USER user."
	[ "$(whoami)" != $USER ] && error $MSG && exit 2
}

function check_requirements() {
	check_aws
}

# Check if the AWS CLI installed is compatible
# Otherwise it installs the new AWS CLI 2
function check_aws() {
	AWS_CLI_VERSION=$($AWS_CLI --version | awk '{print $1}' | awk -F/ '{print $2}')
	VERSION_MAJOR=$(echo $AWS_CLI_VERSION | awk -F. '{print $1}')
	VERSION_MINOR=$(echo $AWS_CLI_VERSION | awk -F. '{print $2}')

	[[ ! $VERSION_MAJOR -eq 2 ]] && error "AWS CLI 2 not installed"
}

######################################
# Commons helpers
######################################

# Print the usage helper using the header as source
function credits() {
	[ "$*" ] && echo "$0: $*"
	sed -n '/^#!#/,/^$/s/^#!# \{0,1\}//p' "$0"
	exit 0
}

# Print the usage helper using the header as source
function usage() {
	[ "$*" ] && echo "$0: $*"
	sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
}

# Generate usage of a method based on its comments
function usage_function() {
	if [[ "$3" == "help" ]]; then
		sed -n "/^## $2$/,/^$/s/^## \{0,1\}//p" "$DIR/../modules/$1.sh" | tail -n +2
		exit 0
	fi
}

######################################
# Cluster helpers
######################################
# get cluster ID using configuration files
function cluster_id() {
	unset CLUSTER_ID
	F1="/emr/instance-controller/lib/info/job-flow.json"
	F2="/emr/instance-controller/lib/info/extraInstanceData.json"
	F3="/var/lib/info/extraInstanceData.json"
	F4="/var/lib/instance-controller/extraInstanceData.json"
	F5="/var/lib/info/job-flow.json"
	F6="/var/aws/emr/userData.json"

	CLUSTER_ID=$(cat $F1 2>/dev/null | jq -r .jobFlowId)
	[ ! -z "$CLUSTER_ID" ] && return
	CLUSTER_ID=$(cat $F2 2>/dev/null | jq -r .jobFlowId)
	[ ! -z "$CLUSTER_ID" ] && return
	CLUSTER_ID=$(cat $F3 2>/dev/null | jq -r .jobFlowId)
	[ ! -z "$CLUSTER_ID" ] && return
	CLUSTER_ID=$(cat $F4 2>/dev/null | jq -r .jobFlowId)
	[ ! -z "$CLUSTER_ID" ] && return
	CLUSTER_ID=$(cat $F5 2>/dev/null | jq -r .jobFlowId)
	[ ! -z "$CLUSTER_ID" ] && return
	CLUSTER_ID=$(cat $F6 2>/dev/null | jq -r .clusterId)

	error "can't find the cluster id...are you using EMR ?"
}

# Retrieve the cluster data
function cluster_data() {
	cluster_id
	CLUSTER_DATA=$($AWS_CLI emr describe-cluster --cluster-id $CLUSTER_ID)
}

# Retrieve the instance data
function instances_data() {
	cluster_id
	INSTANCES_DATA=$($AWS_CLI emr list-instances --cluster-id $CLUSTER_ID)
}

function managed_scaling_data() {
	cluster_id
	MANAGED_SCALING_DATA=$($AWS_CLI emr get-managed-scaling-policy --cluster-id $CLUSTER_ID)
	if [[ -z $MANAGED_SCALING_DATA ]]; then
		cluster_managed_scaling="false"
	else
		cluster_managed_scaling=$(echo $MANAGED_SCALING_DATA | jq -r '. | has("ManagedScalingPolicy")')
	fi
}

function is_installed() {
	cluster_id
	cluster_data
	readarray -t APPLICATIONS < <(echo $CLUSTER_DATA | jq -r .Cluster.Applications[].Name)
	contains "$1" "${APPLICATIONS[@]}"
	[[ $? -ne 0 ]] && error "$1 not installed" && exit 2
}

function is_master() {
	IS_MASTER=$(cat /emr/instance-controller/lib/info/instance.json | jq .isMaster)
	[[ "$IS_MASTER" != "true" ]] && error "$1" && exit 2
}

function is_multimaster() {
	IS_MULTIMASTER=$(cat /emr/instance-controller/lib/info/extraInstanceData.json | jq -r '.haEnabled')
	echo $IS_MULTIMASTER
}

######################################
# Visuals helpers
######################################

function report_header() {
	echo
	echo -e "${delimiter}"
	echo -e "${bold}$1${normal}"
	echo -e "${delimiter}"
	echo
}

function report_entry() {
	[[ ! -z ${2} && ${2} != "\n\n" && ${2} != "\n[]\n"  ]] && echo -e "${bold}${1}${normal} ${2}"
}


function header() {
	echo -e "\n${bold}$1${normal}"
}

function message() {
	echo -e "$1"
}

# visual function for services that are down
function down() {
	echo -e "[${R}!!${W}] $*"
}

# used to display if a service is up and running
function up() {
	echo -e "[${G}UP${W}] $*"
}

function ok() {
	echo -e "emr: $*" && exit 0
}

# Display error messages
function error() {
	echo -e "emr: error: $*" && exit 2
}

function experimental() {
	echo -e "[${R}EXPERIMENTAL${W}] $*"
}

function warning() {
	echo -e "[${Y}WARNING${W}] $*"
}

function success() {
	echo -e "[${G}SUCCESS${W}] $*"
}

function failure() {
	echo -e "[${R}FAILURE${W}] $*"
}


# Print a Key / Value entry
function print_k_v() {
	[[ ! -z ${2} && ${2} != "\n\n" && ${2} != "\n[]\n"  ]] && echo -e "${bold}${1}${normal} ${2}"
}

# Ask confirmation
function ask_confirmation() {
	read -p "Do you want to continue? (y/N) " -n 1 -r
	[[ $REPLY =~ ^[Yy]$ ]] && echo || { echo ; exit 1; }
}

function print_fixed_len() {
	echo "$1" | fold -s -w100
}

######################################
# Text REPORT
######################################
function create_report() {

	cat <<-EOF > $REPORT_PATH
	${delimiter}
	${bold}$1${normal}
	${delimiter}

	The following, highlights a set of best practice you can use to reduce costs and improve performance in your EMR cluster.

	EOF
}

function report_success() {
	success "$1"
}

function report_warning() {
	warning "$1"
	echo " - $1" >> "$REPORT_PATH"
	echo >> "$REPORT_PATH"
}
