#!/bin/bash

usage="Usage: 
$(basename "$0") --logroot <S3 path to Spark event log> --parserjar <path to event log parser jar file> [--vCPU_per_hour_price <EMR serverless vCPU per hour price>] [--GB_per_hour_price <EMR serverless per GB per hour price>]"
	
export SERVERLESS_vCPU_per_hour=0.052624
export SERVERLESS_GB_per_hour=0.0057785

while [[ $# -gt 0 ]]; do
  case $1 in
    --logroot)
      EVENT_LOG_ROOT="$2"
      shift # past argument
      shift # past value
      ;;
    --parserjar)
      PARSER_PATH="$2"
      shift # past argument
      shift # past value
      ;;
    --vCPU_per_hour_price)
      SERVERLESS_vCPU_per_hour="$2"
      shift # past argument
      shift # past value
      ;;
    --GB_per_hour_price)
      SERVERLESS_GB_per_hour="$2"
      shift # past argument
      shift # past value
      ;;  
    -*|--*)
      echo "Unknown option $1"
	  echo $usage
      exit 1
      ;;
    *)
      POSITIONAL_ARGS+=("$1") # save positional arg
      shift # past argument
      ;;
  esac
done

if [ -z "$EVENT_LOG_ROOT" ]
then
      echo "--logroot is missing"
	  echo $usage
	  exit 1
fi

if [ -z "$PARSER_PATH" ]
then
      echo "--parserjar is missing"
	  echo $usage
	  exit 1
fi


export LOG_PARSER_ARGS=""
for p in `aws s3 ls $EVENT_LOG_ROOT | awk -v ROOT="$EVENT_LOG_ROOT" '{$1=$2=$3=""; if ($4) {gsub(/^[ \t]+/, "", $4); PATH=ROOT""$4;  print PATH}}'`
do
    export LOG_PARSER_ARGS="$LOG_PARSER_ARGS $p"
done    

echo "Parse event log for: $LOG_PARSER_ARGS"

spark-submit --class com.amazonaws.emr.telemetry.spark.SparkEventLogParser $PARSER_PATH  $LOG_PARSER_ARGS | 
jq -r '[.app_id,.resource_utilization.driver_core_seconds,.resource_utilization.driver_memory_mb_seconds,.resource_utilization.total_executor_core_seconds,.resource_utilization.total_executor_memory_mb_seconds]|@csv' | 
awk -v per_cpu_cost="$SERVERLESS_vCPU_per_hour" -v per_mem_cost="$SERVERLESS_GB_per_hour" -F',' '
BEGIN{
LINES="app_id,driver_core_seconds,driver_memory_mb_seconds,total_executor_core_seconds,total_executor_memory_mb_seconds"
}
{
cpu+=$2;cpu+=$4;mem+=$3;mem+=$5;
LINES=LINES"\n"$0
} 
END{
    print " "
    print LINES; 
	
    print " "
    print "total_core_seconds,total_memory_mb_seconds"
    print cpu","mem;
	
    print " "
    total_core_hours=cpu/3600
    total_memory_gb_hours=mem/3600/1024
    print "total_core_hours,total_memory_gb_hours"
    print total_core_hours","total_memory_gb_hours;
	
	print " "
    print "serverless_vcpu_per_hour_price,serverless_per_GB_per_hour_price"
    print per_cpu_cost","per_mem_cost;
	
    print " "
    estimated_serverless_vcpu_cost=total_core_hours*per_cpu_cost
    estimated_serverless_memory_cost=total_memory_gb_hours*per_mem_cost
    print "estimated_serverless_vcpu_cost,estimated_serverless_memory_cost"
    print estimated_serverless_vcpu_cost","estimated_serverless_memory_cost;
}'