# Import necessary libraries
from pyspark.sql import SparkSession
import sys
import json
import boto3
from collections import OrderedDict

def generate_simple_dynamodb_schema(field):
    types = []
    # Check if it's a nested struct type.
    if 'StructType' in str(field.dataType):
        for nested_field in field.dataType.fields:
            nested_field_name = nested_field.name
            if nested_field_name == "S":
                types.append("S")
            elif nested_field_name == "N":
                types.append("N")
    elif "BooleanType" in str(field.dataType):
        types.append("BOOL")
    elif "DateType" in str(field.dataType) or "TimestampType" in str(field.dataType):
        types.append("S")  # Represent dates/timestamps as strings in DynamoDB
    elif "MapType" in str(field.dataType):
        types.append("M")  # Represent maps in DynamoDB
    elif "ArrayType" in str(field.dataType):
        types.append("L")  # Represent lists in DynamoDB
    else:
        types.append("S")  # Default to string type for any other unhandled type
    # If only one type, return it as a string. Otherwise, return a list.
    return types if len(types) > 1 else types[0]

# Check if the correct number of command-line arguments are provided
if len(sys.argv) != 3:
    print("Usage: generate_schema.py <input_s3_path> <output_s3_path>")
    sys.exit(1)
# Extract input S3 path and output S3 path from command-line arguments
input_s3_path = sys.argv[1]
output_s3_path = sys.argv[2]
# Initialize a Spark session
spark = SparkSession.builder.appName("Nested JSON Schema Inference").getOrCreate()
# Read the JSON data from the given S3 path into a DataFrame
df = spark.read.json(input_s3_path)
df.printSchema()
# Assuming the "Item" field is always present, let's extract the nested schema
item_schema = df.schema["Item"].dataType
print(item_schema)

# Generate a simplified DynamoDB-compatible schema from the DataFrame's schema for the nested "Item" attributes
# Using OrderedDict to retain the original order of columns
simple_schema = OrderedDict()
alerts = []
for field in item_schema.fields:
    field_type = generate_simple_dynamodb_schema(field)
    if len(field_type) > 1:
        alerts.append(f"'{field.name}' has multiple data types: {', '.join(field_type)}. Pick the correct one for your data lake!")
        simple_schema[field.name] = ",".join(field_type)
    else:
        simple_schema[field.name] = field_type[0]

# Convert the simple schema to JSON string
schema_str = json.dumps(simple_schema, indent=4)
# Print the inferred schema
print(f"Inferred schema: \n{schema_str}")

# Print alerts if there are any
if alerts:
    print("\nAlerts:")
    for alert in alerts:
        print(f"- {alert}")

# Extract bucket and key for output path
output_bucket = output_s3_path.split('/')[2]
output_key = '/'.join(output_s3_path.split('/')[3:])
# Use boto3 to upload the schema string to the specified S3 path
s3 = boto3.client('s3')
s3.put_object(Bucket=output_bucket, Key=output_key, Body=schema_str)
# Print a success message with the output path
print(f"Schema saved to {output_s3_path}")
# Gracefully stop the Spark session
spark.stop()


