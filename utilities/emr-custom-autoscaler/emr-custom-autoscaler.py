#!/usr/bin/env python3
import os
from datetime import datetime, timedelta
import argparse
import boto3
import logging
import sys
import json

###########################################################
# Name: emr-custom-autoscaler
#
# Description: The script gets the YARNMemoryAvailablePercentage. AppsPending, and ContainerPendingRatio and performs
# scaling up or down based on specified thresholds.
#
# It uses the modify-instance-fleet command to update the instance fleet's capacity with the desired TargetSpotCapacity
# or TargetOnDemandCapacity values.
#
# Only works with TASK Instance Fleets
#
# Author: Suthan Phillips (suthan@amazon.com)
#         Ian Meyers (meyersi@amazon.com)
#
# Created: June 26th, 2023
#
# Version: 1.1
#
# Ported primary logic from Bash to Python with various bug fixes, support for OD or Spot based Instance Fleets, and
# support for cli-args, configuration object (for python API usage), and via environment variables to support AWS Lambda
# based scheduling.
#
# Usage: python3 emr-custom-autoscaler.py
# Parameters: cluster_id - The ID of the EMR Cluster you want to scale
#             s3_bucket - The name of the S3 Bucket where the last-scale timestamp information should be stored
#
##########################################################

START_TIME = datetime.utcnow() - timedelta(minutes=5)
END_TIME = datetime.utcnow()
DATE_FORMAT_STRING = "%Y-%m-%d %H:%M:%S"
LAST_RUN_TS = "last_run_timestamp"

logger = None
s3 = boto3.client('s3')

__version__ = 1.1


# method to run a CloudWatch Metrics Query and return a result from the EMR Namespace
def run_metric_query(cloudwatch, metric_name: str, cluster_id: str) -> list:
    # Define the metric query
    metric_query = {
        'Id': 'm1',
        'MetricStat': {
            'Metric': {
                'Namespace': "AWS/ElasticMapReduce",
                'MetricName': metric_name,
                'Dimensions': [
                    {
                        'Name': 'JobFlowId',
                        'Value': cluster_id
                    }
                ]
            },
            'Period': 300,
            'Stat': 'Average'
        },
        'ReturnData': True
    }

    # Retrieve the metric data
    response = cloudwatch.get_metric_data(
        MetricDataQueries=[metric_query],
        StartTime=START_TIME,
        EndTime=END_TIME,
        ScanBy='TimestampDescending'
    )

    # Extract the metric values
    v = response['MetricDataResults'][0]['Values'][0]
    global logger
    logger.debug(f"Metric {metric_name}: {v}")
    return v


# generate the timestamp filename for targeting S3
def get_ts_filename(cluster_id: str) -> str:
    return f"emr-custom-autoscaler-{cluster_id}"


# get the last scale time from the S3 object, if there is one
def get_last_scale_ts(s3, s3_bucket: str, cluster_id: str) -> datetime:
    # Retrieve the object
    response = None
    try:
        response = s3.get_object(Bucket=s3_bucket, Key=get_ts_filename(cluster_id))
    except s3.exceptions.NoSuchKey:
        pass

    # Access the object data
    if response is not None and response.get('Body') is not None:
        object_data = response['Body'].read()
        json_data = json.loads(object_data)

        return datetime.strptime(json_data.get(LAST_RUN_TS), DATE_FORMAT_STRING)
    else:
        return None


# write a record of the last scale time for the cluster to s3
def write_last_scale_ts(s3, s3_bucket: str, cluster_id: str, ts: datetime) -> None:
    data = json.dumps({
        LAST_RUN_TS: ts.strftime(DATE_FORMAT_STRING)
    })
    response = s3.put_object(Body=data, Bucket=s3_bucket, Key=get_ts_filename(cluster_id))

    # Check if the operation was successful
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise Exception('Error writing timestamp file to S3.')


# validate input arguments have been provided, or synthesize them from the environment
def validate_args(args: dict) -> None:
    # load any missing args from the environment
    args = load_args_from_env(args)

    # if we've been provided a config dict, then use that
    if args.get("config_object") is not None:
        args = json.loads(args.get("config_object"))
    else:
        if args.get("config_s3_file") is not None:
            tokens = args.get("config_s3_file").split("/")
            response = s3.get_object(Bucket=tokens[2], Key="/".join(tokens[3:]))
            if response is not None and response.get('Body') is not None:
                print(f'Loading configuration from S3 Path {args.get("config_s3_file")}')
                object_data = response['Body'].read()
                args.update(json.loads(object_data))

    required = ["cluster_id", "s3_bucket", "region"]

    if args.get("region") is None:
        args["region"] = os.getenv("AWS_REGION")

    for x in required:
        if args.get(x) is None:
            raise Exception(f"Argument {x} is required")


# run the autoscaler
def run_scaler(args=None):
    validate_args(args)

    # Configure Logging
    if len(logging.getLogger().handlers) > 0:
        logging.getLogger().setLevel(args.get("log_level", "INFO"))
    else:
        logging.basicConfig(level=args.get("log_level", "INFO"))

    # Create a logger instance
    global logger
    logger = logging.getLogger(__name__)

    # suppress debug logging in the AWS SDK
    if args.get("log_level").upper() == "DEBUG":
        logging.getLogger('boto3').setLevel(logging.INFO)
        logging.getLogger('botocore').setLevel(logging.INFO)
        logging.getLogger('urllib3').setLevel(logging.INFO)
        logging.getLogger('s3transfer').setLevel(logging.INFO)

    logger.debug(args)

    # Create CloudWatch client
    cloudwatch = boto3.client('cloudwatch', region_name=args.get("region"))
    emr = boto3.client('emr', region_name=args.get("region"))

    # Retrieve the YARN Memory Available, Containers Pending, and Apps Pending
    yarn_memory_available = run_metric_query(cloudwatch, "YARNMemoryAvailablePercentage", args.get("cluster_id"))
    container_pending_ratio = run_metric_query(cloudwatch, "ContainerPendingRatio", args.get("cluster_id"))
    apps_pending = run_metric_query(cloudwatch, "AppsPending", args.get("cluster_id"))

    # To get the last updated timestamp from the timestamp file
    last_scale_timestamp = get_last_scale_ts(s3, args.get("s3_bucket"), args.get("cluster_id"))
    if last_scale_timestamp is None:
        last_scale_timestamp = END_TIME - timedelta(minutes=args.get("cooldown_period"))
        elapsed_time = (END_TIME - last_scale_timestamp).total_seconds() / 60
    else:
        elapsed_time = 0

    # Calculate the desired instance count based on the scaling factors and thresholds
    scale_factor_count = 0

    if elapsed_time >= args.get("cooldown_period") or args.get("ignore_cooldown") is True:
        if yarn_memory_available <= args.get("up_threshold"):
            scale_factor_count += args.get("yarn_memory_scale_factor")
            logger.info("Setting scale factor to (+{}) due to lower YARN Memory Available threshold breach.".format(
                args.get("yarn_memory_scale_factor")))

        if yarn_memory_available > args.get("down_threshold"):
            scale_factor_count -= args.get("yarn_memory_scale_factor")
            logger.info("Setting scale factor to (-{}) due to upper YARN Memory Available threshold breach.".format(
                args.get("yarn_memory_scale_factor")))

        if apps_pending >= args.get("apps_pending_up_threshold"):
            scale_factor_count += args.get("apps_pending_scale_factor")
            logger.info("Setting scale factor to (+{}) due to lower Apps Pending threshold breach.".format(
                args.get("apps_pending_scale_factor")))
        if apps_pending < args.get("apps_pending_down_threshold"):
            scale_factor_count -= args.get("apps_pending_scale_factor")
            logger.info("Setting scale factor to (-{}) due to upper Apps Pending threshold breach.".format(
                args.get("apps_pending_scale_factor")))

        if container_pending_ratio >= args.get("container_pending_ratio_up_threshold"):
            scale_factor_count += args.get("container_pending_ratio_scale_factor")
            logger.info("Setting scale factor to (+{}) due to lower Container Pending Ratio threshold breach.".format(
                args.get("container_pending_ratio_scale_factor")))
        if container_pending_ratio < args.get("container_pending_ratio_down_threshold"):
            scale_factor_count -= args.get("container_pending_ratio_scale_factor")
            logger.info("Setting scale factor to (-{}) due to upper Container Pending Ratio threshold breach.".format(
                args.get("container_pending_ratio_scale_factor")))
    else:
        logger.info("Cooldown period not met. Skipping.")
        sys.exit(1)

    # Get the current capacity
    current_capacity = None
    instance_fleet_id = None
    response = emr.list_instance_fleets(
        ClusterId=args.get("cluster_id")
    )
    instance_fleets = response['InstanceFleets']
    for fleet in instance_fleets:
        if fleet.get("InstanceFleetType") == "TASK":
            if fleet.get("Status").get("State") == "RESIZING":
                logger.info("Try again later. Task Fleet is currently resizing")
                sys.exit(-2)

            current_capacity = fleet.get("TargetSpotCapacity")
            instance_fleet_id = fleet.get("Id")
            fleet_type = "SPOT" if fleet.get("TargetSpotCapacity") != 0 else "OD"

    logger.debug(f"Current Fleet Capacity is {current_capacity}")

    # Calculate the new capacity based on the scaling factor
    new_capacity = current_capacity + scale_factor_count

    # Check if the new capacity is less than the minimum capacity
    if new_capacity < args.get("min_capacity"):
        logger.debug(
            "New capacity: {} is less than MIN capacity: {}, setting target to MIN capacity".format(new_capacity,
                                                                                                    args.get(
                                                                                                        "min_capacity")))
        new_capacity = args.get("min_capacity")

    # Check if the new capacity is greater than the maximum capacity
    if new_capacity > args.get("max_capacity"):
        logger.debug(
            "New capacity: {} is greater than MAX capacity: {}, setting target to MAX capacity".format(new_capacity,
                                                                                                       args.get(
                                                                                                           "max_capacity")))
        new_capacity = args.get("max_capacity")

    # Scale the instance fleet if the new capacity is different from the current capacity
    if scale_factor_count != 0 and new_capacity != current_capacity:
        # logger.debug("Scaling instance fleet to {} instances.".format(new_capacity))
        # Modify instance fleet
        logger.info("Scaling instance fleet to {} units.".format(new_capacity))

        target_od = 0
        target_spot = 0
        if fleet_type == "SPOT":
            target_spot = new_capacity
        else:
            target_od = new_capacity

        modify_args = {"ClusterId": args.get("cluster_id"),
                       "InstanceFleet": {
                           'InstanceFleetId': instance_fleet_id,
                           'TargetOnDemandCapacity': target_od,
                           'TargetSpotCapacity': target_spot
                       }}
        response = emr.modify_instance_fleet(**modify_args)
    else:
        logger.info("No scaling required. Instance Fleet size is correct.")

    # write the last scale timestamp back to s3
    write_last_scale_ts(s3, args.get("s3_bucket"), args.get("cluster_id"), last_scale_timestamp)


def load_args_from_env(dictionary):
    for key, value in dictionary.items():
        env_var = os.getenv(key.upper())
        if value is None and env_var is not None:
            dictionary[key] = env_var
    return dictionary


def lambda_handler(event, context):
    args = setup_args()
    args.update(event)
    run_scaler(args)


def setup_args():
    # Create an argument parser
    parser = argparse.ArgumentParser(description='emr-custom-autoscaler')

    # Add arguments
    parser.add_argument('--cluster_id', type=str, help='Cluster ID')
    parser.add_argument('--config_object', type=str, help="Configuration Object (JSON)")
    parser.add_argument('--config_s3_file', type=str, help="Configuration S3 File")
    parser.add_argument('--up_threshold', type=int, default=20,
                        help='Threshold for scaling up (YARN Memory Available Percentage)')
    parser.add_argument('--down_threshold', type=int, default=80,
                        help='Threshold for scaling down (YARN Memory Available Percentage)')
    parser.add_argument('--container_pending_ratio_up_threshold', type=int, default=1,
                        help='Threshold for Container Pending Ratio (up)')
    parser.add_argument('--container_pending_ratio_down_threshold', type=int, default=0,
                        help='Threshold for Container Pending Ratio (down)')
    parser.add_argument('--apps_pending_up_threshold', type=int, default=5, help='Threshold for Apps Pending (up)')
    parser.add_argument('--apps_pending_down_threshold', type=int, default=0, help='Threshold for Apps Pending (down)')
    parser.add_argument('--cooldown_period', type=int, default=300, help='Cooldown period in seconds')
    parser.add_argument('--ignore_cooldown', type=bool, default=False, help='Ignore Cooldown')
    parser.add_argument('--min_capacity', type=int, default=2, help='Minimum capacity')
    parser.add_argument('--max_capacity', type=int, default=20, help='Maximum capacity')
    parser.add_argument('--yarn_memory_scale_factor', type=int, default=2,
                        help='Scaling factor for YARN Memory Available')
    parser.add_argument('--container_pending_ratio_scale_factor', type=int, default=3,
                        help='Scaling factor for Container Pending Ratio')
    parser.add_argument('--apps_pending_scale_factor', type=int, default=1, help='Scaling factor for Apps Pending')
    parser.add_argument('--s3_bucket', type=str, help='S3 bucket for log storage')
    parser.add_argument('--log_level', type=str, help='Log Level')

    # Parse the arguments
    args = parser.parse_args()
    return vars(args)


if __name__ == '__main__':
    args = setup_args()
    # invoke the scaler
    run_scaler(args)
