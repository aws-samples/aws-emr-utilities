#!/bin/bash
#
###########################################################
# Name: IFautoscaler.sh
#
# Description: The script gets the YARNMemoryAvailablePercentage and other metrics and performs scaling up or down based on the thresholds. It uses the modify-instance-fleet command to update the instance fleet's capacity with the desired TargetSpotCapacity and TargetOnDemandCapacity values..
# 
# Author: Suthan Phillips (suthan@amazon.com)
# 
# Created: June 26th, 2023
# 
# Version: 1.1
# -Implemented functionality to move old log files to an S3 bucket.
# -Incorporated logic to validate the values of MIN and MAX, ensuring that if the value falls below MIN or exceeds MAX, it will be set to the respective limits.
# -Enhanced the code to include additional logs. 
# 
# Usage Instructions: 
#  For a one-time execution, run the following command with sudo privileges:
#    sudo sh IFautoscaler.sh
#  To schedule the script as a cron job and execute it every 5 minutes, add the following line to your crontab file:
#    */5 * * * * /bin/bash IFautoscaler.sh
##########################################################


# Set your EMR cluster ID
CLUSTER_ID=$(cat /mnt/var/lib/info/job-flow.json | grep -oP '(?<="jobFlowId": ")[^"]+')

# "----------------------------------------"

# Set the threshold for scaling up (YARN Memory Available Percentage)
UP_THRESHOLD=20

# Set the threshold for scaling down (YARN Memory Available Percentage)
DOWN_THRESHOLD=80

# Set the threshold for Container Pending Ratio
CONTAINER_PENDING_RATIO_UP_THRESHOLD=1
CONTAINER_PENDING_RATIO_DOWN_THRESHOLD=0

# Set the threshold for Apps Pending
APPS_PENDING_UP_THRESHOLD=5
APPS_PENDING_DOWN_THRESHOLD=0

# Set the cooldown period in seconds
COOLDOWN_PERIOD=300

# Set the minimum and maximum capacity
MIN_CAPACITY=2
MAX_CAPACITY=20

# Set the scaling factors
YARN_MEMORY_SCALE_FACTOR=2
CONTAINER_PENDING_RATIO_SCALE_FACTOR=3
APPS_PENDING_SCALE_FACTOR=1

# Set the S3 bucket within your AWS account where the logs should be pushed.
S3_BUCKET="s3://suthan--emr-bda/tmp/logs"

# "----------------------------------------"

# Set the log directory
LOG_DIRECTORY="/var/log/IFautoscaler/"

# Set the log filename with timestamp
LOG_FILENAME="scaling_log_$(date +"%Y%m%d%H").log"

# Set the complete path for the log file
LOG_LOCATION="${LOG_DIRECTORY}${LOG_FILENAME}"

# Set the timestamp file path
TIMESTAMP_FILE="${LOG_DIRECTORY}last_updated_timestamp.txt"

# Get the region from the availability zone
REGION=us-east-1

# knob to turn off or on cooldown
IGNORE_COOLDOWN=false

# Check if the log directory exists, if not, create it
if [ ! -d "$LOG_DIRECTORY" ]; then
  mkdir -p "$LOG_DIRECTORY"
fi

# Log entry function to write to the log file with timestamp
log_entry() {
  echo "$(date +"%Y-%m-%d %H:%M:%S") $1" >> "$LOG_LOCATION"
}

# Function to check if the log file exists and create it if not
#check_log_file() {
if [ ! -f "$LOG_LOCATION" ]; then
  touch "$LOG_LOCATION"
fi
#}

# echo "----------------------------------------" >> "$LOG_LOCATION"
# Retrieve the YARN Memory Available Percentage
YARN_MEMORY_AVAILABLE=$(aws cloudwatch get-metric-data \
  --metric-data-queries '[{"Id": "m1", "MetricStat": {"Metric": {"Namespace": "AWS/ElasticMapReduce", "MetricName": "YARNMemoryAvailablePercentage", "Dimensions": [{"Name": "JobFlowId", "Value": "'"$CLUSTER_ID"'"}]}, "Period": 300, "Stat": "Average"}, "ReturnData": true}]' \
  --start-time "$(date -u -d '5 minutes ago' +'%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
  --output json \
  --region "$REGION" \
  | jq -r '.MetricDataResults[].Values[0]')

# Retrieve the Container Pending Ratio
CONTAINER_PENDING_RATIO=$(aws cloudwatch get-metric-data \
  --metric-data-queries '[{"Id": "m2", "MetricStat": {"Metric": {"Namespace": "AWS/ElasticMapReduce", "MetricName": "ContainerPendingRatio", "Dimensions": [{"Name": "JobFlowId", "Value": "'"$CLUSTER_ID"'"}]}, "Period": 300, "Stat": "Average"}, "ReturnData": true}]' \
  --start-time "$(date -u -d '5 minutes ago' +'%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
  --output json \
  --region "$REGION" \
  | jq -r '.MetricDataResults[].Values[0]')

# Retrieve the Apps Pending
APPS_PENDING=$(aws cloudwatch get-metric-data \
  --metric-data-queries '[{"Id": "m3", "MetricStat": {"Metric": {"Namespace": "AWS/ElasticMapReduce", "MetricName": "AppsPending", "Dimensions": [{"Name": "JobFlowId", "Value": "'"$CLUSTER_ID"'"}]}, "Period": 300, "Stat": "Average"}, "ReturnData": true}]' \
  --start-time "$(date -u -d '5 minutes ago' +'%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
  --output json \
  --region "$REGION" \
  | jq -r '.MetricDataResults[].Values[0]')



CURRENT_TIME=$(date +"%s")

#For testing
converted_time=$(date -d @$CURRENT_TIME)
echo "Converted current time: $converted_time"

# To get the last updated timestamp from the timestamp file

if [ -f "$TIMESTAMP_FILE" ]; then
  echo "Found Timestamp File" 
  LAST_SCALE_TIMESTAMP=$(cat "$TIMESTAMP_FILE")
else
  echo "Could not find Timestamp File" 
  LAST_SCALE_TIMESTAMP=$(($(date +"%s") - $COOLDOWN_PERIOD))
fi

LAST_SCALE_TIME=$(date -d "$LAST_SCALE_TIMESTAMP" +%s)

#For testing
converted_time=$(date -d @$LAST_SCALE_TIME)
echo "Converted last scale time: $converted_time"

# Get the elapsed time since the last log entry
elapsed_time=$((CURRENT_TIME - LAST_SCALE_TIME))


# Calculate the desired instance count based on the scaling factors and thresholds
scale_factor_count=0

if [ "$elapsed_time" -ge "$COOLDOWN_PERIOD" ]; then
  if (( $(echo "$YARN_MEMORY_AVAILABLE <= $UP_THRESHOLD" | bc -l) )); then
    scale_factor_count=$(echo "$scale_factor_count + $YARN_MEMORY_SCALE_FACTOR" | bc)
    log_entry "Adjusted scale factor count (+$YARN_MEMORY_SCALE_FACTOR) due to YARN Memory Available threshold breach."
  fi
  if (( $(echo "$APPS_PENDING >= $APPS_PENDING_UP_THRESHOLD" | bc -l) )); then
    scale_factor_count=$(echo "$scale_factor_count + $APPS_PENDING_SCALE_FACTOR" | bc)
    log_entry "Adjusted scale factor count (+$APPS_PENDING_SCALE_FACTOR) due to Apps Pending threshold breach."
  fi
  if (( $(echo "$CONTAINER_PENDING_RATIO >= $CONTAINER_PENDING_RATIO_UP_THRESHOLD" | bc -l) )); then
    scale_factor_count=$(echo "$scale_factor_count + $CONTAINER_PENDING_RATIO_SCALE_FACTOR" | bc)
    log_entry "Adjusted scale factor count (+$CONTAINER_PENDING_RATIO_SCALE_FACTOR) due to Container Pending Ratio threshold breach."
  fi
  if (( $(echo "$YARN_MEMORY_AVAILABLE > $DOWN_THRESHOLD" | bc -l) )); then
    scale_factor_count=$(echo "$scale_factor_count - $YARN_MEMORY_SCALE_FACTOR" | bc)
    log_entry "Adjusted scale factor count (-$YARN_MEMORY_SCALE_FACTOR) due to YARN Memory Available threshold breach."
  fi
  if (( $(echo "$APPS_PENDING < $APPS_PENDING_DOWN_THRESHOLD" | bc -l) )); then
    scale_factor_count=$(echo "$scale_factor_count - $APPS_PENDING_SCALE_FACTOR" | bc)
    log_entry "Adjusted scale factor count (-$APPS_PENDING_SCALE_FACTOR) due to Apps Pending threshold breach."
  fi
  if (( $(echo "$CONTAINER_PENDING_RATIO < $CONTAINER_PENDING_RATIO_DOWN_THRESHOLD" | bc -l) )); then
    scale_factor_count=$(echo "$scale_factor_count - $CONTAINER_PENDING_RATIO_SCALE_FACTOR" | bc)
    log_entry "Adjusted scale factor count (-$CONTAINER_PENDING_RATIO_SCALE_FACTOR) due to Container Pending Ratio threshold breach."
  fi

else
  log_entry "Cooldown period not met. Skipping."
  # echo "----------------------------------------" >> "$LOG_LOCATION"
  exit 1 
fi


# Get the current capacity
CURRENT_CAPACITY=$(aws emr list-instance-fleets --region "$REGION" --cluster-id "$CLUSTER_ID" --query 'InstanceFleets[?InstanceFleetType==`TASK`].TargetSpotCapacity' --output text)

# Calculate the new capacity based on the scaling factor
NEW_CAPACITY=$((CURRENT_CAPACITY + scale_factor_count))

# Check if the new capacity is less than the minimum capacity
if [ "$NEW_CAPACITY" -lt "$MIN_CAPACITY" ]; then
  NEW_CAPACITY="$MIN_CAPACITY"
  log_entry "New capacity: $NEW_CAPACITY is less than MIN capacity: $MIN_CAPACITY, setting to MIN capacity"
fi

# Check if the new capacity is greater than the maximum capacity
if [ "$NEW_CAPACITY" -gt "$MAX_CAPACITY" ]; then
  NEW_CAPACITY="$MAX_CAPACITY"
  log_entry "New capacity: $NEW_CAPACITY is greater than MAX capacity: $MAX_CAPACITY, setting to MAX capacity"
fi

# Retrieve the instance fleet ID dynamically for InstanceFleetType "TASK"
INSTANCE_FLEET_TYPE=$(aws emr list-instance-fleets --region "$REGION" --cluster-id "$CLUSTER_ID" --query "InstanceFleets[?InstanceFleetType=='TASK'].Id" --output text)

# Scale the instance fleet if the new capacity is different from the current capacity
if [ "$scale_factor_count" -ne 0 ]; then
  #log_entry "Scaling instance fleet to $NEW_CAPACITY instances."
  aws emr modify-instance-fleet \
    --cluster-id "$CLUSTER_ID" \
    --instance-fleet InstanceFleetId="$INSTANCE_FLEET_TYPE",TargetOnDemandCapacity=0,TargetSpotCapacity="$NEW_CAPACITY" \
    --region "$REGION"
  log_entry "Scaling instance fleet to $NEW_CAPACITY units."

  if [[ -f "$TIMESTAMP_FILE" ]]; then
    rm "$TIMESTAMP_FILE"
    log_entry "Deleted existing timestamp file."
  fi

  echo "$(date +"%Y-%m-%d %H:%M:%S")" > "$TIMESTAMP_FILE"
  log_entry "Created/updated timestamp file with the current timestamp."
  # echo "----------------------------------------" >> "$LOG_LOCATION"
else
  log_entry "No scaling required. Desired instance count is zero."
  # echo "----------------------------------------" >> "$LOG_LOCATION"
fi


# Send previous hour logs to S3 and clean up the current directory
# S3_BUCKET="s3://suthan--emr-bda/tmp/logs"
LOG_DIRECTORY="/var/log/IFautoscaler/"
LOG_PREFIX="scaling_log_"

# Get the current hour
CURRENT_HOUR=$(date "+%Y%m%d%H")

# Iterate through log files
for LOG_FILE in "$LOG_DIRECTORY""$LOG_PREFIX"*; do
  # Extract the timestamp from the log file name
  LOG_TIMESTAMP=${LOG_FILE#"${LOG_DIRECTORY}${LOG_PREFIX}"}
  LOG_TIMESTAMP=${LOG_TIMESTAMP%".log"}

  # Check if the log timestamp is a valid number
  if [[ -n "$LOG_TIMESTAMP" && "$LOG_TIMESTAMP" =~ ^[0-9]+$ ]]; then
    # Check if the log file is not from the current hour
    if [[ "$LOG_TIMESTAMP" != "$CURRENT_HOUR" ]]; then
      # Upload the log file to S3
      aws s3 cp "$LOG_FILE" "$S3_BUCKET/"

      # Remove the log file from the local disk
      rm "$LOG_FILE"
    fi
  fi
done

