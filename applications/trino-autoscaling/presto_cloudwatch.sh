#!/bin/bash
set -x -e
REGION=$(cat /tmp/aws-region)

IS_MASTER=false
if grep isMaster /mnt/var/lib/info/instance.json | grep true; then
  IS_MASTER=true
fi

if [ -f /var/log/presto/server.log ]; then
  CLUSTER_ID=$(jq -r .jobFlowId /mnt/var/lib/info/job-flow.json)

  # CPU Utilization Percent
  CPU_UTIL_PERCENT=$(echo $((100 - $(vmstat -n 1 2 | tail -1 | tr -s ' ' | cut -d ' ' -f 16))))

  if [ "$IS_MASTER" = true ]; then # Master node
    # Presto Metrics
    STATS_RESULT=$(presto-cli --execute 'select "abandonedqueries.totalcount", "canceledqueries.totalcount", "completedqueries.totalcount", "executiontime.fiveminutes.avg", "failedqueries.totalcount", "queuedqueries", "queuedtime.fiveminutes.avg", "runningqueries" from "presto.execution:name=querymanager"' --catalog jmx --schema current --output-format TSV)
    ABANDONED_QUERIES_TOTAL_COUNT=$(echo "$STATS_RESULT" | cut -d$'\t' -f1)
    CANCELED_QUERIES_TOTAL_COUNT=$(echo "$STATS_RESULT" | cut -d$'\t' -f2)
    COMPLETED_QUERIES_TOTAL_COUNT=$(echo "$STATS_RESULT" | cut -d$'\t' -f3)
    EXECUTION_TIME_FIVE_MINUTES_AVG=$(echo "$STATS_RESULT" | cut -d$'\t' -f4)
    FAILED_QUERIES_TOTAL_COUNT=$(echo "$STATS_RESULT" | cut -d$'\t' -f5)
    QUEUED_QUERIES=$(echo "$STATS_RESULT" | cut -d$'\t' -f6)
    QUEUED_TIME_FIVE_MINUTES_AVG=$(echo "$STATS_RESULT" | cut -d$'\t' -f7)
    RUNNING_QUERIES=$(echo "$STATS_RESULT" | cut -d$'\t' -f8)

    NUM_NODES=$(presto-cli --execute 'select "activecount" from "presto.failuredetector:name=heartbeatfailuredetector"' --catalog jmx --schema current --output-format TSV)

    # Subtract total stats queries from completed count
    # We fire 2 stats queries per trigger
    STATS_QUERY_COUNT=0
    if [ -f /tmp/presto_stats_query_count ]; then
      STATS_QUERY_COUNT=$(cat /tmp/presto_stats_query_count)
    fi
    STATS_QUERY_COUNT="$(($STATS_QUERY_COUNT + 2))"
    COMPLETED_QUERIES_TOTAL_COUNT="$(($COMPLETED_QUERIES_TOTAL_COUNT - $STATS_QUERY_COUNT))"
    echo $STATS_QUERY_COUNT >/tmp/presto_stats_query_count

    PREV_AVG_QUERY_TIME=0
    AVG_QUERY_TIME_INC=0
    if [ -f /tmp/presto_avg_query_time ]; then
      PREV_AVG_QUERY_TIME=$(cat /tmp/presto_avg_query_time)
    fi
    if [ ! "$PREV_AVG_QUERY_TIME" = 0 ]; then
      AVG_QUERY_TIME_INC=$(bc <<<"scale=2;100*($EXECUTION_TIME_FIVE_MINUTES_AVG-$PREV_AVG_QUERY_TIME)/$PREV_AVG_QUERY_TIME")
    fi
    echo $EXECUTION_TIME_FIVE_MINUTES_AVG >/tmp/presto_avg_query_time

    aws cloudwatch put-metric-data --metric-name MasterCpuUtilization --namespace AWS/ElasticMapReduce --unit Percent --value $CPU_UTIL_PERCENT --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumQueuedQueries --namespace AWS/ElasticMapReduce --unit Count --value $QUEUED_QUERIES --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoAvgQueryTime5m --namespace AWS/ElasticMapReduce --unit Milliseconds --value $EXECUTION_TIME_FIVE_MINUTES_AVG --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoAvgQueryTime5mInc --namespace AWS/ElasticMapReduce --unit Percent --value $AVG_QUERY_TIME_INC --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoAvgQueuedTime5m --namespace AWS/ElasticMapReduce --unit Milliseconds --value $QUEUED_TIME_FIVE_MINUTES_AVG --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumWorkerNodes --namespace AWS/ElasticMapReduce --unit Count --value $NUM_NODES --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumRunningQueries --namespace AWS/ElasticMapReduce --unit Count --value $RUNNING_QUERIES --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumCompletedQueries --namespace AWS/ElasticMapReduce --unit Count --value $COMPLETED_QUERIES_TOTAL_COUNT --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumAbandonedQueries --namespace AWS/ElasticMapReduce --unit Count --value $ABANDONED_QUERIES_TOTAL_COUNT --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumCanceledQueries --namespace AWS/ElasticMapReduce --unit Count --value $CANCELED_QUERIES_TOTAL_COUNT --dimensions JobFlowId=$CLUSTER_ID --region $REGION
    aws cloudwatch put-metric-data --metric-name PrestoNumFailedQueries --namespace AWS/ElasticMapReduce --unit Count --value $FAILED_QUERIES_TOTAL_COUNT --dimensions JobFlowId=$CLUSTER_ID --region $REGION

    # pump metrics into Ganglia if installed
    if [ -f /etc/ganglia/gmond.conf ]; then
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumQueuedQueries --value $QUEUED_QUERIES --type int32 --unit count
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoAvgQueryTime5m --value $EXECUTION_TIME_FIVE_MINUTES_AVG --type float --unit=ms
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoAvgQueryTime5mInc --value $AVG_QUERY_TIME_INC --type float --unit=%
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoAvgQueuedTime5m --value $QUEUED_TIME_FIVE_MINUTES_AVG --type float --unit=ms
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumWorkerNodes --value $NUM_NODES --type int32 --unit count
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumRunningQueries --value $RUNNING_QUERIES --type int32 --unit count
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumCompletedQueries --value $COMPLETED_QUERIES_TOTAL_COUNT --type int32 --unit count
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumAbandonedQueries --value $ABANDONED_QUERIES_TOTAL_COUNT --type int32 --unit count
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumCanceledQueries --value $CANCELED_QUERIES_TOTAL_COUNT --type int32 --unit count
      /usr/bin/gmetric -c /etc/ganglia/gmond.conf --group presto --name PrestoNumFailedQueries --value $FAILED_QUERIES_TOTAL_COUNT --type int32 --unit count
    fi
  else # Worker nodes
    aws cloudwatch put-metric-data --metric-name WorkerCpuUtilization --namespace AWS/ElasticMapReduce --unit Percent --value $CPU_UTIL_PERCENT --dimensions JobFlowId=$CLUSTER_ID --region $REGION
  fi
fi
