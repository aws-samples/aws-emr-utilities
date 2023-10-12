# This script reads a DynamoDB full export and returns a schema definition.
# Remember that not all items in DynamoDB must have the same schema, so the
# output here is a union of all attributes across all items.
#
# Suggest using EMR Serverless with Apache Spark for parallel execution.
#
# Example Usage:
# spark-submit detect_schema_from_full_export.py s3://dynamodb-export-bucket/any-prefix/01234-export-folder/ s3://iceberg-bucket/output-schema.json

from botocore.exceptions import ClientError
from pyspark.sql import SparkSession
from collections import OrderedDict
import sys
import json
import boto3

# Function to generate schema based on ddb and maps to spark datatype
def generate_simple_dynamodb_schema(field):
    types = []
    # Check if it's a nested struct type.
    if 'StructType' in str(field.dataType):
        for nested_field in field.dataType.fields:
            nested_field_name = nested_field.name
            # Check directly in a list of known DynamoDB types
            dynamodb_types = ["S", "N", "B", "BOOL", "L", "M", "NULL", "SS", "NS", "BS"]
            if nested_field_name in dynamodb_types:
                    # If the type is "BOOL" or "NULL", we append the whole type and break out of the loop
                if nested_field_name in ["BOOL", "NULL"]:
                    types = [nested_field_name]
                    break
                else:
                    types.append(nested_field_name)
    elif "DateType" in str(field.dataType) or "TimestampType" in str(field.dataType):
        types.append("S")  # Represent dates/timestamps as strings in DynamoDB
    else:
        types.append("S")  # Default to string type for any other unhandled type
    # If only one type, return it as a string. Otherwise, return a list.
    return types if len(types) > 1 else types[0]

# Function to list manifest json file within S3 export folder
def check_manifest_file_in_s3_path(s3_path):
    # Parse the S3 path to get bucket and prefix
    path_parts = s3_path.replace("s3://", "").split("/")
    bucket = path_parts[0]
    prefix = "/".join(path_parts[1:])
    # Initialize S3 client
    s3 = boto3.client('s3')
    # Check for manifest-files.json in the given path
    result = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}manifest-files.json")
    for content in result.get('Contents', []):
        if content['Key'].endswith('manifest-files.json'):
            return True
    return False

# Function to read from S3
def read_from_s3_with_spark(spark_session, s3_path):
    try:
        df = spark_session.read.json(s3_path)
        return df
    except Exception as e:
        print(f"Error reading data from S3: {str(e)}")
        sys.exit(1)

# Function to upload file to S3
def upload_to_s3(bucket, key, data):
    s3 = boto3.client('s3')
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=data)
    except ClientError as e:
        print(f"Error uploading data to S3: {e}")
        sys.exit(1)

# Check the correct number of command-line arguments are provided
if len(sys.argv) != 3:
    print("Usage: detect_schema_from_full_export.py <dynamodb_export_bucket_with_prefix> <iceberg_bucket_with_schema_file_name>")
    sys.exit(1)

# Extract the params
dynamodb_export_bucket_with_prefix = sys.argv[1]
iceberg_bucket_with_schema_file_name = sys.argv[2]

print(f"Provided Arguments:")
print(f"  dynamodb_export_bucket_with_prefix: {dynamodb_export_bucket_with_prefix}")
print(f"  iceberg_bucket_with_schema_file_name: {iceberg_bucket_with_schema_file_name}")
print("-----------------------")

# Ensure the input path is to the export folder and not the data folder
if dynamodb_export_bucket_with_prefix.endswith('data/'):
    print("Validation Failed: Please point to the export folder, not the data folder within the export.")
    sys.exit(1)

# Sanity check to ensure the input path points to a folder and not a specific file
if not dynamodb_export_bucket_with_prefix.endswith('/'):
  print("Validation Failed: The input path should point to a folder. Ensure it ends with a '/'")
  sys.exit(1)

# Sanity check that the input path is a full export that has successfully completed,verify if manifest exists
if not check_manifest_file_in_s3_path(dynamodb_export_bucket_with_prefix):
    print("Validation Failed: The input path either isn't a full export or hasn't completed successfully.")
    sys.exit(1)

# Sanity check to ensure the output path points to an object and not a folder
if iceberg_bucket_with_schema_file_name.endswith('/'):
  print("Validation Failed: The output path should point to a file object and not a folder. Ensure it doesn't end with a '/'")
  sys.exit(1)

print("All parameter validations passed.")

# Initialize a Spark session
spark = SparkSession.builder.appName("DynamoDB JSON Schema Inference for Spark").getOrCreate()

# Read all the JSON data from the input path into a DataFrame, data/ is appended to look under full export path
df = read_from_s3_with_spark(spark,f"{dynamodb_export_bucket_with_prefix}data/")
df.printSchema()

# Full exports use an "Item" field, so let's extract the nested schema
item_schema = df.schema["Item"].dataType

# Now generate a schema from the DataFrame's schema for the nested "Item" attributes
# Use OrderedDict to retain the original order of columns
simple_schema = OrderedDict()
alerts = []
for field in item_schema.fields:
  field_type = generate_simple_dynamodb_schema(field)
  if isinstance(field_type, list):
    if "BOOL" in field_type or "NULL" in field_type:
      simple_schema[field.name] = field_type[0]
    else:
      alerts.append(f"'{field.name}' has multiple data types in different items: {', '.join(field_type)}. Pick the correct one for your data lake!")
      simple_schema[field.name] = ",".join(field_type)
  else:
    simple_schema[field.name] = field_type

# Convert the simple schema to a JSON string
schema_str = json.dumps(simple_schema, indent=4)

# Print the inferred schema
print(f"Inferred schema: \n{schema_str}")

# Print any alerts
if alerts:
    print("\nAlerts:")
    for alert in alerts:
        print(f"- {alert}")

# Extract bucket and key from the output path
output_bucket = iceberg_bucket_with_schema_file_name.split('/')[2]
output_key = '/'.join(iceberg_bucket_with_schema_file_name.split('/')[3:])

# Upload the schema string to the output S3 location
upload_to_s3(output_bucket, output_key, schema_str)

# Print a success message with the output path
print(f"Schema saved to {iceberg_bucket_with_schema_file_name}")

# Gracefully stop the Spark session
spark.stop()
