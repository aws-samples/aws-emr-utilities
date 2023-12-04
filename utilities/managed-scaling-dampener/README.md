# Managed Scaling Dampener

### Introduction
In certain scenarios, Managed Scaling clusters may be underutilized due to EMR constantly scaling to maximum capacity. This occurs when workloads demand a large number of containers, leading to EMR scaling up to meet these demands. The low utilization is a result of these containers running for a small duration (less than 30 seconds) and by the time the cluster scales up, all pending tasks are already completed. The EMR Managed Scaling algorithm is designed to scale up nodes to the maximum to minimize the impact of failures and optimize for SLA, which is a good fit for many customer use cases. But in some scenarios, when there is a preference to optimize it for cost, we would want it to scale up conservatively and scale down aggressively.

To address this concern, this project introduces adjustable knobs for Managed Scaling in EMR providing customers with the control to define the Initial_max for Managed Scaling, gradually scale up to Managed Scaling Max, and efficiently revert back to Initial_max if the container_pending is zero for a specified Cool_down_period.

### Execution on EMR Clusters

This involves two scripts:

- **MSdampener.sh**: The primary script that manages Managed Scaling values based on user-provided input.
- **run_continuously_s3.sh**: This script copies 'MSdampener.sh' from an S3 bucket to the Hadoop home directory, running it periodically to control Managed Scaling values.

The latter script is executed as an EMR step on the cluster, with customizable execution frequency by adjusting the sleep command. It logs events to the configured location (default is '/var/log/MSdampener/').

#### Instructions

1. Save the scripts in an S3 location:
```bash
s3://suthan-emr-bda/scripts/new-2/MSdampener.sh
s3://suthan-emr-bda/scripts/new-2/run_continuously_s3.sh
```
2. Modify 'run_continuously_s3.sh' by passing the following four values as arguments to 'MSdampener.sh' in the specified order:

- **node_group_conf**="$1": Specifies if the node group is an Instance Fleet or Instance Group.
- **Initial_max**="$2": Sets the initial threshold for scaling up.
- **Scale_increment_max**="$3": Determines the threshold for incrementing the maximum in Managed Scaling based on container pending.
- **Cool_down_period_to_initial_max**="$4": Sets the cooldown period in seconds, defining the minimum interval for container pending to remain zero to reset to the initial max.

3. Run the modified 'run_continuously_s3.sh' script as an EMR step, for example:
```bash
--steps '[{"Name":"MSdampener","ActionOnFailure":"CONTINUE","Jar":"s3://us-west-2.elasticmapreduce/libs/script-runner/script-runner.jar","Properties":"","Args":["s3://suthan-emr-bda/scripts/new-2/run_continuously_s3.sh"],"Type":"CUSTOM_JAR"}]'
```
    
### Details
The following values will be taken from the Managed Scaling Configuration which you define in your EMR clusters
    * maximum_capacity: Sets the threshold for scaling up to the maximum, beyond which scaling will cease.
    * minimum_capacity: Represents the lower boundary of the cluster size.
    * maximum_core_capacity: Specifies the upper boundary of the core node group.
    * maximum_on_demand_capacity: Sets the upper boundary of the capacity to be provisioned from the On-Demand market.
    
You should be able to see these values stored in 'originalMSconfiguration.txt' file the MSDampener log directory. For example,

```sh
$ cat /var/log/MSdampener/originalMSconfiguration.txt
Minimum Capacity: 4
Unit Type: InstanceFleetUnits
Maximum Capacity: 1600
Maximum Core Capacity: 4
Maximum On-Demand Capacity: 1600
```

### Considerations
1. Ensure that the cooldown period is either less than or equal to the script execution period specified by the sleep command in MSdampener.sh for it to be effective.
2. Configuring this for multiple clusters within a single account may result in a high volume of Managed Scaling API calls to EMR.

