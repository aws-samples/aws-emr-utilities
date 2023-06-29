# Instance Fleet Autoscaler

### Introduction
At present, EMR Auto Scaling only offers support for Instance groups. Managed Scaling feature in EMR provides support for both Instance groups and Instance Fleets, but it does not support node labels today. In the above explained configuration where AM is placed exclusively on core nodes, Managed Scaling, being unaware of node labeling, may result in potential problems by not scaling up when necessary.
To address this concern, this script is developed to enable Autoscaling in EMR providing customers with a seamless transition option who currently utilize instance groups with Autoscaling in EMR and wish to switch to instance fleets.

### About the Script
This script is as a bootstrap action that executes a cron job on the master node of an EMR cluster. The frequency of the cron job can be set to match the cooldown period. The script implements an algorithm similar to custom autoscaling to initiate scaling events. Also, it logs the events to the configured location.

### Instructions
1. The user is required to configure the following 15 values in the script.
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






### Limitations
1) Cooldown period is standardized and applies to all metrics. There is no flexibility to set different cooldown periods for individual metrics. Users can define the cool down period in seconds and it will evaulate it for all metrics (Yarn memory, container pending, apps pending) . The evaluation will always be for 1 - 5 minute interval

