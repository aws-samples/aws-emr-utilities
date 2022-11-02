#!/bin/bash

# usage helper
help() {
	cat <<-EOF
	usage: emr node <subcommand>

	  configs			local hadoop configurations of the node
	  disk				details on disks utilization
	  gc				details on garbage collection utilization
	  heap				heap summary or memory dump of a jvm process
	  memory			details on memory utilization
	  net				details on network utilization
	  thread			thread dump of a java process

	EOF
}

## configs
## usage: emr node configs
##
## Return local Hadoop configurations of the node.
##
configs() {

	usage_function "node" "configs" "$*"

	# retrieve the node configurations from the local IC
	curl 'http://localhost:8321/configuration' 2> /dev/null | jq
	exit 0
}

## disk
## usage: emr node disk [FREQ] [TIME] [PATH]
##
## Gather details on disks utilization.
##
##   FREQ			Delay between each run (sec)
##   TIME			Maximum execution time (sec)
##   PATH			Path used to store logs
##
## Usage example:
##
##   emr node disk                      # print single datapoint
##   emr node disk 30 300               # collect data points in log files
##   emr node disk 30 300 /tmp/data     # logs in custom path
##
disk() {

	usage_function "node" "disk" "$*"
	_print_or_collect _disk "disk-\${ts}.dump.log" "null" $*
	exit 0
}

_disk() {
	LIMIT=10

	echo && uptime
	echo && df -h
	echo && iostat -x 1 1

	for p in $(find / -mindepth 1 -maxdepth 1 -type d -iname mnt*); do
		echo "# Top $LIMIT directory in $p"
		sudo du -Sh $p | sort -rh | head -n $LIMIT
		echo

		echo "# Top $LIMIT files in $p"
		sudo find $p -type f -exec du -Sh {} + | sort -rh | head -n $LIMIT
		echo
	done
}

## gc
## usage: emr node gc <PID> [FREQ] [TIME] [PATH]
##
## Gather statistics of frequency and time spent in garbage collection.
## Requires to specify the PID of a running java process.
## If no PID is passed, returns a list of running java applications.
##
##   PID			Process Identifier of a Java command
##   FREQ			Delay between each run (sec)
##   TIME			Maximum execution time (sec)
##   PATH			Path used to store logs
##
## Usage example:
##
##   emr node gc                        # list running java processes
##   emr node gc 1                      # print single datapoint
##   emr node gc 1 30 300               # collect data points in log files
##   emr node gc 1 30 300 /tmp/data     # logs in custom path
##
gc() {

	usage_function "node" "gc" "$*"

	pid="$1"
	[[ -z "$1" ]] && sudo sudo jps -lV | sort -k 2 && echo && error "you must specify a java process id"

	if ! ps -p $pid >/dev/null; then
		error "the process id $pid does not exit"
	fi

	_print_or_collect _gc "gc-\${param}.ts-\${ts}.dump.log" $*
	exit 0
}

_gc() {
	pid="$1"
	owner=$(ps -o user= -p $pid)
	echo && uptime
	echo && sudo su -s /bin/bash -c "jstat -gcutil -t $pid 5 5" "$owner"
}

## heap
## usage: emr node heap <PID> [dump]
##
## Gather heap summary details or memory dump of a jvm process.
## Requires to specify the PID of a running java process.
## If no PID is passed, returns a list of running java applications.
##
##   PID			Process Identifier of a Java command
##
## Usage example:
##
##   emr node heap                        # list running java processes
##   emr node heap 1                      # print heap summary
##   emr node heap 1 dump                 # generate heap binary dump
##
heap() {

	usage_function "node" "heap" "$*"

	pid=$(echo ${1/dump/})
	dump=$(echo ${1/$pid/})

	[[ -z "$1" ]] && sudo sudo jps -lV | sort -k 2 && echo && error "you must specify a java process id"

	if ! ps -p $pid >/dev/null; then
		error "the process id $pid does not exit"
	fi

	mkdir -p $LOG_PATH
	owner=$(ps -o user= -p $pid)
	if [[ $dump == "dump" ]]; then
		echo && sudo su - $owner -c "jmap -dump:format=b,file=$LOG_PATH/heap-$pid.dump.bin $pid"
	else
		echo && uptime
		echo && sudo jmap -heap $pid
	fi
	exit 0
}

## memory
## usage: emr node memory [FREQ] [TIME] [PATH]
##
## Gather details on memory utilization.
##
##   FREQ			Delay between each run (sec)
##   TIME			Maximum execution time (sec)
##   PATH			Path used to store logs
##
## Usage example:
##
##   emr node memory                      # print single datapoint
##   emr node memory 30 300               # collect data points in log files
##   emr node memory 30 300 /tmp/data     # logs in custom path
##
memory() {

	usage_function "node" "memory" "$*"
	_print_or_collect _memory "memory-\${ts}.dump.log" "null" $*
	exit 0
}

_memory() {
	echo && uptime
	echo && free -mh
	echo && ps -eo pid,user,%mem,%cpu --sort -rss | head
	echo
}

## net
## usage: emr node net [FREQ] [TIME] [PATH]
##
## Gather details on network utilization.
##
##   FREQ			Delay between each run (sec)
##   TIME			Maximum execution time (sec)
##   PATH			Path used to store logs
##
## Usage example:
##
##   emr node net                      # print single datapoint
##   emr node net 30 300               # collect data points in log files
##   emr node net 30 300 /tmp/data     # logs in custom path
##
net() {

	usage_function "node" "net" "$*"
	_print_or_collect _net "net-\${ts}.dump.log" "null" $*
	exit 0
}

_net() {
	echo && uptime
	echo && netstat -i
	echo && ss -s
	echo && sar -n DEV 1 1
	echo && nstat
	echo && netstat -s
	echo
}

## thread
## usage: emr node thread <PID> [FREQ] [TIME] [PATH]
##
## Gather details on the thread dump of a java process.
## Requires to specify the PID of a running java process.
## If no PID is passed, returns a list of running java applications.
##
##   PID			Process Identifier of a Java command
##   FREQ			Delay between each run (sec)
##   TIME			Maximum execution time (sec)
##   PATH			Path used to store logs
##
## Usage example:
##
##   emr node thread                        # list running java processes
##   emr node thread 1                      # print single datapoint
##   emr node thread 1 30 300               # collect data points in log files
##   emr node thread 1 30 300 /tmp/data     # logs in custom path
##
thread() {

	usage_function "node" "thread" "$*"

	pid="$1"
	[[ -z "$1" ]] && sudo sudo jps -lV | sort -k 2 && echo && error "you must specify a java process id"

	if ! ps -p $pid >/dev/null; then
		error "the process id $pid does not exit"
	fi

	_print_or_collect _thread "thread-\${param}.ts-\${ts}.dump.log" $*
	exit 0
}

_thread() {
	pid="$1"
	owner=$(ps -o user= -p $pid)
	sudo su -s /bin/bash -c "jstack -l $pid" "$owner"
}

#===============================================================================
# Helper Functions
#===============================================================================
_print_or_collect() {

	DUMP_PATH="$LOG_PATH/${1/_/}"

	funct="$1"
	format="$2"
	param="$3"
	frequency="$4"
	timeout="$5"
	dest_path="$6"

	if [[ (! -z $frequency) || (! -z $timeout) ]]; then

		[[ ! $frequency =~ ^[0-9]+$ ]] && error "frequency should be an integer (sec)"
		[[ ! $timeout =~ ^[0-9]+$ ]] && error "timeout should be an integer (sec)"
		[[ $frequency -gt $timeout ]] && error "timeout should be greater than frequency"
		[[ ! -z $dest_path ]] && DUMP_PATH="$dest_path/${funct/_/}"

		mkdir -p $DUMP_PATH
		data_points=$(_ceiling_rounding $timeout $frequency)
		{ for (( i=1; i<=$data_points; i++ )); do ts=$(date +"%s") ; eval "$funct $param" > $DUMP_PATH/$(eval echo $format) ; sleep $frequency; done } &

		success "data collection for '${funct/_/}' started. Reports in $DUMP_PATH"

	else
		eval "$funct $param"
	fi
	exit 0
}

_ceiling_rounding() {
	X=$1
	Y=$2
	echo "$(( ( $X / $Y ) + ( $X % $Y > 0 ) ))"
}
