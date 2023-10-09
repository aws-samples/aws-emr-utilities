import subprocess # nosec
import argparse
import json
import logging
logging.basicConfig(level=logging.INFO)
parser = argparse.ArgumentParser(description="Find Incremental Exports and Submit Spark Job")
parser.add_argument('--extractId', required=True, help='Incremental Exports extractId')
parser.add_argument('--delta_table_name', required=True, help='Delta table name')
parser.add_argument('--full_table_name', required=True, help='Full table name')
args = parser.parse_args()
extractId = args.extractId
delta_table_name = args.delta_table_name
full_table_name = args.full_table_name
# Define S3 bucket and path
s3_bucket = "jason-exports"
s3_path = f"{s3_bucket}/20230919-prefix/AWSDynamoDB/{extractId}"
def read_metadata(s3_path):
  metadata_file = "manifest-files.json"
  aws_command = f"aws s3 cp 's3://{s3_path}/{metadata_file}' -"
  metadata_content = subprocess.getoutput(aws_command) # nosec
  spark_paths = []
  for line in metadata_content.split("\n"):
    data_file_path = json.loads(line).get('dataFileS3Key')
    if data_file_path:
      spark_paths.append(f'"s3://{s3_bucket}/{data_file_path}"')
  spark_final_path = f"[{','.join(spark_paths)}]"
  logging.info(f"Data files are located at: {spark_final_path}")
  # Directly submit the PySpark job using spark-submit
  spark_submit_cmd = [
    "spark-submit",
    "ingest_data.py",
    spark_final_path,
    delta_table_name,
    full_table_name
  ]
  subprocess.run(spark_submit_cmd) # nosec
if __name__ == "__main__":
  read_metadata(s3_path)
