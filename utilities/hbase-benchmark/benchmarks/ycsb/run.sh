#!/bin/bash
set -e

# Parse arguments passed via EMR step
while [[ $# -gt 0 ]]; do
  case $1 in
    --record-count) RECORD_COUNT="$2"; shift 2;;
    --operation-count) OPERATION_COUNT="$2"; shift 2;;
    --threads) THREADS="$2"; shift 2;;
    --n-splits) N_SPLITS="$2"; shift 2;;
    --s3-results-path) S3_RESULTS_PATH="$2"; shift 2;;
    *) shift;;
  esac
done

RECORD_COUNT=${RECORD_COUNT:-1000000}
OPERATION_COUNT=${OPERATION_COUNT:-200000}
THREADS=${THREADS:-64}
N_SPLITS=${N_SPLITS:-50}
DB="hbase2"
COLUMN_FAMILY="family"
TABLE="usertable"
RESULTS_DIR="/home/hadoop/ycsb_results"

mkdir -p ${RESULTS_DIR}

# Install and build YCSB if not present
if [ ! -d "/home/hadoop/YCSB" ]; then
  echo "Installing YCSB..."
  yes | sudo yum install git maven
  cd /home/hadoop
  git clone https://github.com/brianfrankcooper/YCSB.git
  cd YCSB
  mvn -pl site.ycsb:hbase2-binding -am clean package -DskipTests=True
fi

# Create HBase table (ignore if exists)
hbase shell <<EOF || true
create 'usertable', 'family', {SPLITS => (1..${N_SPLITS}).map {|i| "user#{1000+i*(9999-1000)/${N_SPLITS}}"}}
exit
EOF

cd /home/hadoop/YCSB/

retry() {
  local n=1 max=3 delay=5
  while true; do
    "$@" && break || {
      if [[ $n -lt $max ]]; then ((n++)); echo "Attempt $n/$max:"; sleep $delay
      else echo "Failed after $n attempts."; return 1; fi
    }
  done
}

# Load in parallel batches
BATCHES=$((RECORD_COUNT / OPERATION_COUNT))
for i in $(seq 0 $((BATCHES - 1))); do
  INSERT_START=$((i * OPERATION_COUNT))
  echo "Loading batch $i from $INSERT_START..."
  retry bin/ycsb load ${DB} -P workloads/workloada \
    -p recordcount=${OPERATION_COUNT} -p insertstart=${INSERT_START} \
    -p threadcount=${THREADS} -p table=${TABLE} -p columnfamily=${COLUMN_FAMILY} \
    -s > ${RESULTS_DIR}/load_batch_${i}.txt 2>&1 &
done
wait
echo "Load phase completed."

# Run workloads sequentially
for WORKLOAD in workloada workloadb workloadc workloadf workloade workloadd; do
  echo "Running ${WORKLOAD}..."
  retry bin/ycsb run ${DB} -P workloads/${WORKLOAD} \
    -p threadcount=${THREADS} -p recordcount=${RECORD_COUNT} \
    -p operationcount=${OPERATION_COUNT} -p table=${TABLE} \
    -p columnfamily=${COLUMN_FAMILY} -s > ${RESULTS_DIR}/${WORKLOAD}.txt 2>&1
done

echo "YCSB benchmark completed."

# Upload results to S3
if [[ -n "$S3_RESULTS_PATH" ]]; then
  aws s3 cp ${RESULTS_DIR}/ ${S3_RESULTS_PATH}/ --recursive
  echo "Results uploaded to ${S3_RESULTS_PATH}"
fi
