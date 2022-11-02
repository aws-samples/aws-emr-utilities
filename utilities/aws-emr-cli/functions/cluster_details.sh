#!/bin/bash

details_cluster() {
	report_header "Cluster Info"
	report_entry "Id" "$cluster_id"
	report_entry "Name" "$cluster_name"
	report_entry "Release" "$cluster_emr_release"
	report_entry "Applications" "$cluster_apps_formatted"
	echo
	report_entry "Composition" "$cluster_composition"
	report_entry "Multi Master" "$cluster_is_multimaster"
	report_entry "Managed Scaling" "$cluster_managed_scaling"
	report_entry "Auto Terminate" "$cluster_auto_terminate"
	report_entry "Scale-Down Behavior" "$cluster_scale_down_behaviour"
	report_entry "Termination Protected" "$cluster_termination_protected"
	echo
	report_entry "Created" "$cluster_creation_date"
	report_entry "Ready" "$cluster_ready_date"
	report_entry "Startup Time" "$cluster_startup_time"
	report_entry "Running Time" "$cluster_running_time"
	echo
	report_entry "Status" "$cluster_status"
	report_entry "State Change Reason" "$cluster_last_state"
	echo
	report_entry "Log Encrypted" "$cluster_logs_encrypted"
	report_entry "Log Encryption Key" "$cluster_logs_encryption_key"
	report_entry "Log Uri" "$cluster_log_path"
	echo
	report_entry "Tags" "$cluster_tags_formatted"
}

details_cluster_network() {
	report_header "Cluster Networking"
	report_entry "Vpc" "$vpc_id"
	report_entry "Subnet" "$subnet_id"
	report_entry "Cidr" "$subnet_cidr"
	report_entry "Available Ip" "$subnet_available_ip"
	report_entry "Availability Zone" "$availability_zone"
	report_entry "S3 Gateway Endpoint" "$subnet_has_s3_gw ($vpc_s3_gw)"
	echo
	report_entry "DNS" "$dns"
	report_entry "Domain" "$domain_name"
	echo
	report_entry "MasterPublicDnsName" "$master_public_hostname"
	echo
}
