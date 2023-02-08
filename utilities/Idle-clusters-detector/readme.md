## Idle clusters detector

This utility is a tool to monitor the idleness of Amazon EMR clusters in the account. 
It makes use of the ‘IsIdle’ and ‘MRActiveNodes’ metrics from cloudwatch.

#### Why Idle clusters detector

The purpose of this is to identify unused EMR clusters and take appropriate action such as scaling down or terminating it.

#### Usage Instructions

You need to enter the input on the desired report age in hours and the script prints the list of EMR clusters in the account that were idle from that time along with the number of worker nodes. 
```
sh emr_isIdle_MRActiveNodes_reportgen.sh
```
![Alt text](images/reportgen.png?raw=true "Sample screenshot")
