#!/bin/bash
# Example: Run pipeline on sample event logs

python3 pipeline_wrapper.py \
  --input-bucket my-event-logs-bucket \
  --input-prefix spark-event-logs/ \
  --staging-prefix staging/ \
  --output recommendations.json \
  --limit 50

echo "✓ Pipeline complete!"
echo "  - Recommendations: recommendations.json"
echo "  - Summary: recommendations.csv"
