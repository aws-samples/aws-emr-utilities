#!/usr/bin/env bash

## update
## usage: emr update
##
## Update the emr-cli software on the local node using the S3 DEV bucket.
##
function update() {
	usage_function "update" "update" "$*"

	sudo $AWS_CLI s3 sync "$UPDATE_BUCKET" "$INSTALL_PATH"
	[ $? -ne 0 ] && error "failed to access $UPDATE_BUCKET"
	sudo chmod +x "$INSTALL_PATH/bin/emr"
	source /etc/profile.d/emr-cli.sh
	ok "software updated"
}
