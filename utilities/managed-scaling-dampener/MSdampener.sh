#!/bin/bash
# Author: Suthan Phillips <suthan@amazon.com>
# Usage: ./MSdampener.sh <node_group_conf> <Upper_max> <Initial_max> <Scale_increment_max> <Cool_down_period_to_initial_max> <minimum_cluster_size> <maximum_core_nodes> <maximum_on_demand>
# Example usage: * * * * * sudo /home/hadoop/MSdampener.sh InstanceGroup 20 12 2 15 3 10 10 >> /home/hadoop/MSdampener_log.txt 2>&1

# Function to log entries with timestamps
log_entry() {
  echo "$(date +"%Y-%m-%d %H:%M:%S") $1" >> "$LOG_LOCATION"
}

# Function to check if the log file exists and create it if not
check_log_file() {
  if [ ! -f "$LOG_LOCATION" ]; then
    touch "$LOG_LOCATION"
  fi
}

# Function to log containersPending metric data with timestamps
log_containers_pending_metric() {
  echo "$(date +"%Y-%m-%d %H:%M:%S") containersPending=$1" >> "$CONTAINERS_PENDING_LOG"
}

# Function to check if the log file for containersPending data exists and create it with an initial entry if not
check_containers_pending_log() {
  if [ ! -f "$CONTAINERS_PENDING_LOG" ]; then
    touch "$CONTAINERS_PENDING_LOG"
    echo "0001-01-01 00:00:00 containersPending=1" >> "$CONTAINERS_PENDING_LOG"
  fi
}

# Check and create the log directory if it doesn't exist
create_log_directory() {
  if [ ! -d "$LOG_DIRECTORY" ]; then
    mkdir -p "$LOG_DIRECTORY"
  fi
}

# User input values
node_group_conf="$1"
Upper_max="$2"
Initial_max="$3"
Scale_increment_max="$4"
Cool_down_period_to_initial_max="$5"
minimum_cluster_size="$6"
maximum_core_nodes="$7"
maximum_on_demand="$8"

# Set the log directory
LOG_DIRECTORY="/var/log/MSdampener/"

# Set the log filename with timestamp
LOG_FILENAME="scaling_log_$(date +"%Y%m%d%H").log"

# Set the complete path for the log file
LOG_LOCATION="${LOG_DIRECTORY}${LOG_FILENAME}"

# Set the containersPending metric data log file
CONTAINERS_PENDING_LOG="${LOG_DIRECTORY}containersPendingmetricdata.log"

# Set the timestamp file path
TIMESTAMP_FILE="${LOG_DIRECTORY}last_updated_timestamp.txt"

# Get the region from the availability zone
REGION=us-east-1

# Flag to turn off or on cooldown
IGNORE_COOLDOWN=false

# Create the log directory
create_log_directory

# Check and create the containersPending log file if it doesn't exist
check_containers_pending_log

# Set CLUSTER_ID
CLUSTER_ID=$(cat /mnt/var/lib/info/job-flow.json | grep -oP '(?<="jobFlowId": ")[^"]+')
log_entry "Cluster ID: $CLUSTER_ID"

# Set master_ip
ip_address=$(hostname -A | tr -d '[:space:]')
log_entry "Master IP address: $ip_address"

# Initialize variables
Current_max=$Initial_max
last_timestamp=0

# Check container pending value
containersPending=$(curl -X GET "http://$ip_address:8088/ws/v1/cluster/metrics" | jq '.clusterMetrics.containersPending')

# Log containersPending metric data with a timestamp
log_containers_pending_metric $containersPending

# Check containersPending and determine the values for $Current_max
if [ "$containersPending" -eq 0 ]; then
  last_entry=$(tail -n 2 "$CONTAINERS_PENDING_LOG" | head -n 1)
  last_timestamp=$(echo "$last_entry" | cut -d ' ' -f 1-2)
  last_unix_timestamp=$(date -d "$last_timestamp" +%s)
  last_containers_pending=$(echo "$last_entry" | awk -F= '{print $2}')

  if [ "$last_containers_pending" -eq 0 ]; then
    current_timestamp=$(date +%s)
    time_elapsed=$((current_timestamp - last_unix_timestamp))

    if [ $time_elapsed -ge $Cool_down_period_to_initial_max ]; then
      Current_max=$Initial_max
      log_entry "Cooldown period passed and containersPending is 0 for both current and last entry. Resetting Current_max to Initial_max: $Current_max"
    fi
  fi
elif [ "$containersPending" -gt 0 ]; then
  Current_max=$((Current_max + Scale_increment_max))
  log_entry "Increasing Current_max due to containers pending. New Current_max: $Current_max"
fi

# Set Managed scaling in EMR with the values computed
if [ "$node_group_conf" == "InstanceFleet" ]; then
  aws emr put-managed-scaling-policy --cluster-id $CLUSTER_ID --managed-scaling-policy "ComputeLimits={MinimumCapacityUnits=$minimum_cluster_size, MaximumCapacityUnits=$Current_max, MaximumOnDemandCapacityUnits=$maximum_on_demand, MaximumCoreCapacityUnits=3, UnitType=InstanceFleetUnits}" --region $REGION
  log_entry "Setting Managed scaling policy for Instance Fleet with MaximumCapacityUnits: $Current_max"
else
  aws emr put-managed-scaling-policy --cluster-id $CLUSTER_ID --managed-scaling-policy "ComputeLimits={MinimumCapacityUnits=$minimum_cluster_size, MaximumCapacityUnits=$Current_max, MaximumOnDemandCapacityUnits=$maximum_on_demand, MaximumCoreCapacityUnits=$maximum_core_nodes, UnitType=Instances}" --region $REGION
  log_entry "Setting Managed scaling policy for Instance Group with MaximumCapacityUnits: $Current_max"
fi

log_entry "MSDampener Execution is completed"

