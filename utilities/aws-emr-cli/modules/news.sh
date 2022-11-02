#!/usr/bin/env bash

EMR_WHATS_NEW="https://docs.aws.amazon.com/emr/latest/ReleaseGuide/amazon-emr-release-notes.rss"
LIMIT=5

## news
## usage: emr news
##
## Display latest EMR versions and features released.
##
## https://docs.aws.amazon.com/emr/latest/ReleaseGuide/amazon-emr-release-notes.rss
##
function news() {

	usage_function "news" "news" "$*"

	readarray -t NEWS < <(curl $EMR_WHATS_NEW 2>/dev/null | tr -d '[\r\n]' | tr -s ' ' | xmlstarlet sel -T -t -m //rss/channel/item -n -v "concat(pubDate,'##',title,'##',description,'##',link)" | head -n $LIMIT)

	for i in "${NEWS[@]}"; do
		if [[ ! -z $i ]]; then
			published=$(echo "$i" | awk -F'##' '{print $1}' | grep -o  '[A-Za-z]\+, [0-9]\+ [A-Za-z]\+ [0-9]\+')
			title=$(echo "$i" | awk -F'##' '{print $2}')
			description=$(echo "$i" | awk -F'##' '{print $3}' | sed 's/<[^>]*>//g' )
			link=$(echo "$i" | awk -F'##' '{print $4}')

			header "$published - $title"
			print_fixed_len "$description"; echo
			echo "Reference: $link"; echo

		fi
	done ; exit 0
}
