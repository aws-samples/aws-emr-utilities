#!/bin/bash

# display helper
help() {
	cat <<-EOF
	usage: emr apps <subcommand>

	  kill				terminate running YARN applications
	  list				list YARN applications
	  logs				return logs for a valid YARN application
	  monitor			monitor the status of YARN applications

	EOF
}

## kill
## usage: emr apps kill <all | ID | YARN_ID>
##
## Terminate all running YARN applications or specific one.
##
## Usage example:
##   emr apps kill 1
##   emr apps kill all
##   emr apps kill application_1581348238683_0001
##
kill() {

	usage_function "apps" "kill" "$*"

	app_id="${1,,}"

	if [[ "$app_id" == "all" ]]; then
		warning "this will terminate all running jobs"
		ask_confirmation
		for a in $(yarn application -list -appStates RUNNING | awk 'NR > 2 { print $1 }'); do
			yarn application -kill "$a" 2>/dev/null;
		done
	else
		_check_app_id $app_id
		app_id=$( _check_app_id $app_id )

		warning "this will terminate $app_id"
		ask_confirmation
		yarn application -kill "$app_id" 2>/dev/null
	fi
	echo && exit 0
}

## list
## usage: emr apps list [STATUS]
##
## List YARN applications. You can specify one or more comma separated YARN status to filter. If STATUS is not defined, it will list only running apps.
##
##   STATUS: ALL, NEW, NEW_SAVING, SUBMITTED, ACCEPTED, RUNNING, FINISHED, FAILED, KILLED
##
## Usage example:
##   emr apps list
##   emr apps list all
##   emr apps list running,submitted
##
list() {

	usage_function "apps" "list" "$*"

	STATUS=("ALL" "NEW" "NEW_SAVING" "SUBMITTED" "ACCEPTED" "RUNNING" "FINISHED" "FAILED" "KILLED")

	[[ -z "$1" ]] && app_status="RUNNING" || app_status=${1^^}

	states=$(echo ${app_status//[[:space:]]/} | tr "," "\n")
	for s in $states; do
		contains "$s" "${STATUS[@]}"
		[[ $? -ne 0 ]] && error "$s is not a valid YARN status"
	done

	echo && yarn application -list -appStates "$app_status" 2>/dev/null
	echo && exit 0
}

## logs
## usage: emr apps logs <YARN_ID>
##
## Return logs for a valid YARN application.
##
## Usage example:
##   emr apps logs 1
##   emr apps logs application_1581348238683_0001
##
logs() {

	usage_function "apps" "logs" "$*"

	app_id="${1,,}"

	_check_app_id $app_id
	app_id=$( _check_app_id $app_id )
	yarn logs -applicationId $app_id

	echo && exit 0
}

## monitor
## usage: emr apps monitor
##
## Monitor the status of YARN jobs, and cluster usage.
##
monitor() {

	usage_function "apps" "monitor" "$*"

	yarn top
	echo && exit 0
}


#===============================================================================
# Helper Functions
#===============================================================================

## _check_app_id
## usage: emr apps _check_app_id <ID | YARN_ID>
##
## Check if the specified ID belongs to a valid YARN application
##
## Usage example:
##   emr apps _check_app_id 1
##   emr apps _check_app_id application_1581348238683_0001
##
_check_app_id() {

	usage_function "apps" "_check_app_id" "$*"

	app_id="${1,,}"
	[[ -z $app_id ]] && error "missing application id"
	[[ ! $app_id =~ ^(application_[0-9]+_)?[0-9]+$ ]] && error "$app_id is not a valid YARN ID"

	yarn application -list -appStates ALL 2>/dev/null | tail -n +3 | awk -F"\t" 'OFS="\t"{print $1}' | sort | grep "$app_id\$" | head -n 1
}

## _check_app_running
## usage: emr apps _check_app_running <ID | YARN_ID>
##
## Return the YARN_ID of applications submitted or in a running state.
##
_check_app_running() {

	usage_function "apps" "_check_app_running" "$*"

	yarn application -list -appStates ALL 2>/dev/null | tail -n +3 | awk -F"\t" 'OFS="\t"{print $1}' | sort -r
	exit 0
}
