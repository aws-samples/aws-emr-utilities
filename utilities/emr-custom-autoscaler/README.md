# Instance Fleet Autoscaler

### Introduction
At present, EMR Auto Scaling only offers support for Instance groups. Managed Scaling feature in EMR provides support 
for both Instance groups and Instance Fleets, but it does not support node labels today. In the above explained 
configuration where AM is placed exclusively on core nodes, Managed Scaling, being unaware of node labeling, may result 
in potential problems by not scaling up when necessary.

To address this concern, this project was developed to enable Autoscaling in EMR providing customers with a seamless 
transition option who currently utilize Instance Groups with Autoscaling in EMR and wish to switch to Instance Fleets.

### Running Directly on EMR Clusters
This script is as a bootstrap action that executes a cron job on the master node of an EMR cluster. The frequency of 
the cron job can be set to match the cooldown period. The script implements an algorithm similar to custom autoscaling 
to initiate scaling events. Also, it logs the events to the configured location.

### Instructions
1. The user is required to configure the following 15 values in the `IFautoscaler-<version>.sh` script.
* UP_THRESHOLD: The threshold for scaling up based on the YARNMemoryAvailablePercentage.
* DOWN_THRESHOLD: The threshold for scaling down based on the YARN Memory Available Percentage.
* PENDING_RATIO_UP_THRESHOLD: The threshold for scaling up based on the ContainerPendingRatio.
* PENDING_RATIO_DOWN_THRESHOLD: The threshold for scaling down based on the ContainerPendingRatio.
* APPS_PENDING_UP_THRESHOLD: The threshold for scaling up based on AppsPending metric
* APPS_PENDING_DOWN_THRESHOLD::The threshold for scaling up based on AppsPending metric
* MIN_CAPACITY : Minimum possible number of EC2 instances for the instance fleet.
* MAX_CAPACITY: Maximum possible number of EC2 instances for the instance fleet.
* COOLDOWN_PERIOD: The cooldown period in seconds. This period defines the minimum time interval between consecutive scaling events.
* LOG_DIRECTORY: The directory where the log file will be stored. Default set to /var/log/IFautoscale
* YARN_MEMORY_SCALE_FACTOR: Number of units to adjust when yarn memory threshold is breached.
* CONTAINER_PENDING_RATIO_SCALE_FACTOR: Number of units to adjust when Container Pending Ratio threshold is breached.
* APPS_PENDING_SCALE_FACTOR: Number of units to adjust when Apps Pending threshold is breached
* S3_BUCKET: S3 bucket within your AWS account where the logs should be pushed
* REGION: the region of the EMR cluster
2. a)For a one-time execution, run the following command with sudo privileges:
sudo sh IFautoscaler.sh
b)To schedule the script as a cron job and execute it every 5 minutes, add the following line to your crontab file:
   */5 * * * * /bin/bash IFautoscaler.sh

### Running on AWS Lambda
If you prefer to run the autoscaler independent of the cluster, then you can use the `emr-custom-autoscaler.py` python 
module. This supports all of the same logic as the bash based scaling script, but provides the following additional features:
* Ability to run in AWS Lambda independent of the Cluster
* Ability to run from a command line with full configuration support
* Ability to configure threshold settings above from cli args, environment variables, or via a Dict argument for inclusion in other python modules

When running in this mode, the autoscaler places a timestamping lock file onto Amazon S3 for cooldown logic.

#### Instructions - CLI

Simply clone this repository and then run:
```bash
python3 emr-custom-autoscaler.py <options>

options:
  -h, --help            show this help message and exit
  --cluster_id CLUSTER_ID 
                        Cluster ID
  --config_object CONFIG_OBJECT
                        Configuration Object (JSON)
  --config_s3_file CONFIG_S3_FILE
                        Configuration S3 File
  --up_threshold UP_THRESHOLD
                        Threshold for scaling up (YARN Memory Available Percentage)
  --down_threshold DOWN_THRESHOLD
                        Threshold for scaling down (YARN Memory Available Percentage)
  --container_pending_ratio_up_threshold CONTAINER_PENDING_RATIO_UP_THRESHOLD
                        Threshold for Container Pending Ratio (up)
  --container_pending_ratio_down_threshold CONTAINER_PENDING_RATIO_DOWN_THRESHOLD
                        Threshold for Container Pending Ratio (down)
  --apps_pending_up_threshold APPS_PENDING_UP_THRESHOLD
                        Threshold for Apps Pending (up)
  --apps_pending_down_threshold APPS_PENDING_DOWN_THRESHOLD
                        Threshold for Apps Pending (down)
  --cooldown_period COOLDOWN_PERIOD
                        Cooldown period in seconds
  --ignore_cooldown IGNORE_COOLDOWN
                        Ignore Cooldown
  --min_capacity MIN_CAPACITY
                        Minimum capacity
  --max_capacity MAX_CAPACITY
                        Maximum capacity
  --yarn_memory_scale_factor YARN_MEMORY_SCALE_FACTOR
                        Scaling factor for YARN Memory Available
  --container_pending_ratio_scale_factor CONTAINER_PENDING_RATIO_SCALE_FACTOR
                        Scaling factor for Container Pending Ratio
  --apps_pending_scale_factor APPS_PENDING_SCALE_FACTOR
                        Scaling factor for Apps Pending
  --s3_bucket S3_BUCKET
                        S3 bucket for log storage
  --log_level LOG_LEVEL
                        Log Level

```

Arguments `--cluster_id` and `--s3_bucket` are required.

#### Instructions - AWS Lambda

You can use the included deploy.yaml to create a Lambda function which will run the autoscaling module. This supports
environment variable configuration using the parameters listed above, but all in upper case. For example, `--cluster_id`
will require an environment variable called `CLUSTER_ID`. You can also configure a scheduled Event Bridge rule to call 
the Lambda function on a 5 minute basis, and include the required configuration values (using the above lower-case 
naming convention) in the payload of the event sent to the function.

### Limitations
1) Cooldown period is standardized and applies to all metrics. There is no flexibility to set different cooldown periods 
for individual metrics.  c  0y6hjnan define the cool down period in seconds and it will evaulate it for all metrics (Yarn 
memory, container pending, apps pending) . The evaluation will always be for 1 - 5 minute interval

