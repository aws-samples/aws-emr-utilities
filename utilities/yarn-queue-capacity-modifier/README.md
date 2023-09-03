# Yarn Queue Modifier

This is a script that helps administrators and developers to efficiently manage YARN queues.

## Why Yarn Queue Modifier

* If you wish to modify queues without causing disruptions to the running applications.
* If you prefer to avoid utilizing the reconfiguration API, which would restart the YARN services when the queue properties are modified.
* If you are using Instance Fleets where you cannot use the reconfiguration API.
* When you seek to modify queues without the necessity of comprehending individual property names within the YARN capacity scheduler.

With the current available solutions, there are some limitations. For example,

*Reconfiguration API*
* The running applications will be impacted as it will restart yarn services.
* You cannot use it for Instance Fleets
* You need to know all the Yarn queue properties


### Supported Features
* Add a new queue
* Remove an existing queue
* When modifying queues or generating XML, the script will create a backup of the existing XML file(/etc/hadoop/conf/capacity-scheduler.xml) and refresh the YARN admin.

# Instructions
#### How to use
1)Download the Script
*yarn_queue_config_tool.py*

2)Run the script using the command: python YARN_Config_Manager.py
You will see a menu with options:
![Alt text](images/ScriptMenu.png?raw=true "Script Menu")

* Select option 1 to add or remove queues and set their properties.
* Select option 2 from the menu to generate an XML representation of the properties from the capacity-scheduler.properties file and refresh the yarn queues(you need to do this if you want the changes what you made in option 1 to be reflected in the yarn queues )
* Select option 3 from the menu to create a new capacity-scheduler.properties file based on the XML configuration. This is a one-time activuty for a cluster and you should opt for this choice during the initial execution of the script as the script assumes the presence of the 'capacity-scheduler.properties' file as a necessary prerequisite.
* Select option 4 to exit the script.

### Next version
Add option to modify existing queue


