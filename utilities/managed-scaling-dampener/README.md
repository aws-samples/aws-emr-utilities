# Managed Scaling Dampener

### Introduction
In certain scenarios, Managed Scaling clusters may be underutilized due to EMR constantly scaling to maximum capacity. This occurs when workloads demand a large number of containers, leading to EMR scaling up to meet these demands. The low utilization is a result of these containers running for a small duration (less than 30 seconds) and by the time the cluster scales up, all pending tasks are already completed. The EMR Managed Scaling algorithm is designed to scale up nodes to the maximum to minimize the impact of failures and optimize for SLA, which is a good fit for many customer use cases. But in some scenarios, when there is a preference to optimize it for cost, we would want it to scale up conservatively and scale down aggressively.

To address this concern, this project introduces adjustable knobs for Managed Scaling in EMR providing customers with the control to define the Initial_max for Managed Scaling, gradually scale up to Upper_max, and efficiently revert back to Initial_max if the container_pending is zero for a specified Cool_down_period.

### Running Directly on EMR Clusters
This script can be executed as a cron job on the master node of an EMR cluster. The frequency of the cron job can be customized. Also, it logs the events to the configured location.

### Instructions
1. The user needs to pass the following 8 values as script arguments in the specified order:

    * node_group_conf="$1": Specifies whether the node group is an Instance Fleet or Instance Group.
    * Upper_max="$2": Sets the threshold for scaling up to the maximum, beyond which scaling will cease.
    * Initial_max="$3": Establishes the initial threshold for scaling up.
    * Scale_increment_max="$4": Determines the threshold for incrementing the maximum in Managed Scaling based on container pending.
    * Cool_down_period_to_initial_max="$5": Sets the cooldown period in seconds. This period dictates the minimum time interval during which container pending should remain zero to reset to the initial max.
    * minimum_cluster_size="$6": Represents the lower boundary of the cluster size.
    * maximum_core_nodes="$7": Specifies the upper boundary of the core node group.
    * maximum_on_demand="$8": Sets the upper boundary of the capacity to be provisioned from the On-Demand market.

2. Schedule the script as a cron job and execute it every 5 minutes, add the following line to your crontab file:

```bash
* * * * * sudo /home/hadoop/MSdampener.sh InstanceGroup 20 12 2 15 3 10 10 >> /home/hadoop/MSdampener_log.txt 2>&1
```

### Limitations
1. The minimum cooldown period can be configured for 1 minute, which aligns with the shortest duration possible for an individual metric's cron job setting.
2. Similarly, the script execution frequency cannot be more frequent than 1 minute due to the same constraint.
3. Configuring this for multiple clusters within a single account may result in a high volume of Managed Scaling API calls to EMR.
