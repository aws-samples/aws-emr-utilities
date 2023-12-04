#!/bin/bash -e
# Author: Suthan Phillips <suthan@amazon.com>
# Usage: ./MSdampener.sh <node_group_conf> <Initial_max> <Scale_increment_max> <Cool_down_period_to_initial_max>
# Example usage: * * * * * sudo /home/hadoop/MSdampener.sh InstanceGroup 11 3 60 >> /home/hadoop/MSdampener_log.txt 2>&1

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
    echo "0001-01-01 00:00:00 containersPending=0" >> "$CONTAINERS_PENDING_LOG"
  fi
}

# Check and create the log directory if it doesn't exist
create_log_directory() {
  if [ ! -d "$LOG_DIRECTORY" ]; then
    mkdir -p "$LOG_DIRECTORY"
  fi
}

# Function to get AWS EMR managed scaling policy
get_managed_scaling_policy() {
  json_response=$(aws emr get-managed-scaling-policy --cluster-id "$CLUSTER_ID" --region "$REGION" || true)
  
  # Extract values and store in variables
  minimum_capacity=$(echo "$json_response" | jq -r '.ManagedScalingPolicy.ComputeLimits.MinimumCapacityUnits')
  unit_type=$(echo "$json_response" | jq -r '.ManagedScalingPolicy.ComputeLimits.UnitType')
  maximum_capacity=$(echo "$json_response" | jq -r '.ManagedScalingPolicy.ComputeLimits.MaximumCapacityUnits')
  maximum_core_capacity=$(echo "$json_response" | jq -r '.ManagedScalingPolicy.ComputeLimits.MaximumCoreCapacityUnits')
  maximum_on_demand_capacity=$(echo "$json_response" | jq -r '.ManagedScalingPolicy.ComputeLimits.MaximumOnDemandCapacityUnits')
}

# User input values
node_group_conf="$1"
Initial_max="$2"
Scale_increment_max="$3"
Cool_down_period_to_initial_max="$4"

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
REGION=us-west-2

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

# Retrieve and log the managed scaling policy values
get_managed_scaling_policy
log_entry "Managed Scaling Policy Values - MinimumCapacity: $minimum_capacity, UnitType: $unit_type, MaximumCapacity: $maximum_capacity, MaximumCoreCapacity: $maximum_core_capacity, MaximumOnDemandCapacity: $maximum_on_demand_capacity"

# Initialize variables
last_timestamp=0
Current_max=$maximum_capacity

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
      # Check if time_elapsed is greater than 2,592,000 seconds (30 days)
      if [ $time_elapsed -gt 2592000 ]; then
        Current_max=$Initial_max
        log_entry "Cooldown period passed and containersPending is 0 for both current and last entry, this is the first execution. Resetting Current_max to Initial_max: $Current_max"
        echo "Minimum Capacity: $minimum_capacity" > "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Unit Type: $unit_type" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Maximum Capacity: $maximum_capacity" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Maximum Core Capacity: $maximum_core_capacity" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Maximum On-Demand Capacity: $maximum_on_demand_capacity" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
      else
        Current_max=$Initial_max
        log_entry "Cooldown period passed and containersPending is 0 for both current and last entry, Resetting Current_max to initial_max: $Current_max"
      fi
    fi
  fi
elif [ "$containersPending" -gt 0 ]; then
  Current_max=$((Current_max + Scale_increment_max))
  log_entry "Increasing Current_max due to containers pending. New Current_max: $Current_max"
  if [ $time_elapsed -gt 2592000 ]; then
	echo "Minimum Capacity: $minimum_capacity" > "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Unit Type: $unit_type" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Maximum Capacity: $maximum_capacity" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Maximum Core Capacity: $maximum_core_capacity" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
        echo "Maximum On-Demand Capacity: $maximum_on_demand_capacity" >> "$LOG_DIRECTORY/originalMSconfiguration.txt"
  fi 
fi

# Set Managed scaling in EMR with the values computed
if [ "$node_group_conf" == "InstanceFleet" ]; then
  maximum_capacity_org=$(grep "Maximum Capacity:" "$LOG_DIRECTORY/originalMSconfiguration.txt" | cut -d ':' -f 2 | tr -d '[:space:]')
  maximum_on_demand_capacity_org=$(grep "Maximum On-Demand Capacity:" "$LOG_DIRECTORY/originalMSconfiguration.txt" | cut -d ':' -f 2 | tr -d '[:space:]')	
  # For Instance Fleet
  if [ "$maximum_on_demand_capacity_org" -gt "$Current_max" ]; then
    maximum_on_demand_capacity=$Current_max
  else
    maximum_on_demand_capacity=$maximum_on_demand_capacity_org
  fi
  if [ "$maximum_core_capacity" -gt "$Current_max" ]; then
    maximum_core_capacity=$Current_max
  fi
  if [ "$Current_max" -gt "$maximum_capacity_org" ]; then
    Current_max="$maximum_capacity_org"
    log_entry "Current_max exceeds maximum_capacity. Resetting Current_max to maximum_capacity: $Current_max"
  fi
  aws emr put-managed-scaling-policy --cluster-id "$CLUSTER_ID" --managed-scaling-policy "ComputeLimits={MinimumCapacityUnits=$minimum_capacity, MaximumCapacityUnits=$Current_max, MaximumOnDemandCapacityUnits=$maximum_on_demand_capacity, MaximumCoreCapacityUnits=$maximum_core_capacity, UnitType=InstanceFleetUnits}" --region "$REGION"
  log_entry "Setting Managed scaling policy for Instance Fleet with MaximumCapacityUnits: $Current_max, MaximumOnDemandCapacityUnits: $maximum_on_demand_capacity"
else
  maximum_capacity_org=$(grep "Maximum Capacity:" "$LOG_DIRECTORY/originalMSconfiguration.txt" | cut -d ':' -f 2 | tr -d '[:space:]')	
  # For Instance Group
  if [ "$maximum_on_demand_capacity_org" -gt "$Current_max" ]; then
    maximum_on_demand_capacity=$Current_max
  else
    maximum_on_demand_capacity=$maximum_on_demand_capacity_org
  fi
  if [ "$maximum_core_capacity" -gt "$Current_max" ]; then
    maximum_core_capacity=$Current_max
  fi
  if [ "$Current_max" -gt "$maximum_capacity_org" ]; then
    Current_max="$maximum_capacity_org"
    log_entry "Current_max exceeds maximum_capacity. Resetting Current_max to maximum_capacity: $Current_max"
  fi
  aws emr put-managed-scaling-policy --cluster-id "$CLUSTER_ID" --managed-scaling-policy "ComputeLimits={MinimumCapacityUnits=$minimum_capacity, MaximumCapacityUnits=$Current_max, MaximumOnDemandCapacityUnits=$maximum_on_demand_capacity, MaximumCoreCapacityUnits=$maximum_core_capacity, UnitType=Instances}" --region "$REGION"
  log_entry "Setting Managed scaling policy for Instance Group with MaximumCapacityUnits: $Current_max, MaximumOnDemandCapacityUnits: $maximum_on_demand_capacity, MaximumCoreCapacityUnits: $maximum_core_capacity"
fi

log_entry "MSDampener Execution is completed"


