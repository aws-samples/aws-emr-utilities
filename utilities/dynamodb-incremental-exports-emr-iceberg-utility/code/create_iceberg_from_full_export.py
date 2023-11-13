# This script reads a DynamoDB full export folder and creates an Apache Iceberg table.
# It requires the schema that was detected using detect_schema_from_full_export.py.
#
# Suggest using EMR Serverless with Apache Spark for parallel execution.
#
# Usage: spark-submit create_iceberg_from_full_export.py <dynamodb_export_bucket_with_prefix> <iceberg_bucket_with_schema_file_name> <iceberg_table_name> <iceberg_bucket_with_prefix>
# Example: spark-submit create_iceberg_from_full_export.py "s3://dynamodb-export-bucket/any-prefix/01234-export-folder/" "s3://iceberg-bucket/prefix/schema.json" "table_name" "s3://iceberg-bucket/example-prefix/"

from pyspark.sql import SparkSession
from botocore.exceptions import ClientError
import sys
import json, boto3

def validate_s3_argument(s3_arg, arg_name):
    if not s3_arg.startswith("s3://"):
        print(f"The argument {arg_name} does not start with 's3://'. Please provide a valid S3 URI.")
        sys.exit(1)

def is_valid_table_name(table_name):
    # Check if the table name contains only lowercase alphanumeric characters or underscores
    if all(char.isalnum() or char == "_" for char in table_name) and table_name.islower():
        return "Table name is valid."
    else:
        print("Invalid table name. Please use only numbers, lowercase characters, or underscores in table name.")
        sys.exit(1)

def load_schema_file_from_s3(s3_path, spark_session):
    """
    Loads the schema we're to use, based on a JSON file located on S3.
    Parameters:
    s3_path: str. Path to the schema JSON file on S3.
    spark_session: SparkSession. Active SparkSession instance to read from S3.
    Returns:
    dict. Schema as a dictionary.
    """
    try:
        # Extract bucket and key from the S3 path
        s3_parts = s3_path.replace("s3://", "").split("/")
        bucket = s3_parts[0]
        key = "/".join(s3_parts[1:])

        # Initialize the S3 client
        s3 = boto3.client('s3')

        # Get the file content
        response = s3.get_object(Bucket=bucket, Key=key)
        schema_content = response['Body'].read().decode('utf-8')

        # Convert the content to a dictionary and return
        return json.loads(schema_content) 
    except ClientError as e:
        print(f"Error fetching schema from S3: {e}")
        sys.exit(1)
    except json.JSONDecodeError:
        print("Error decoding the JSON schema.")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error loading schema: {str(e)}")
        sys.exit(1)

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


def load_full_table(dynamodb_export_bucket_with_prefix, user_schema, full_table_name, iceberg_datalake_bucket):
    """
    When pointing to an S3 directory (dynamodb_export_bucket_with_prefix), Spark will read all files within that directory.
    If the files are compressed, Spark will automatically decompress and treat them as a single dataset.
    Here, we're pointing at the export folder, not the data folder within the export, to ensure the behavior matches with incremental loads.
    """

    # Initializing Spark session with necessary configurations for Iceberg
    # If you prefer to use external Hive Meta store you can by enabling config --conf "spark.sql.catalog.dev.type=hadoop" and turning off Glue configs in your job (--conf spark.sql.catalog.dev.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog --conf spark.hadoop.hive.metastore.client.factory.class=com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory"). 
    spark = SparkSession.builder \
        .appName("Load full export to Apache Iceberg") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.jars", "/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar") \
        .config("spark.sql.catalog.dev", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.dev.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog") \
        .config("spark.hadoop.hive.metastore.client.factory.class", "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory") \
        .config("spark.sql.catalog.dev.warehouse", iceberg_datalake_bucket) \
        .getOrCreate()

    # Read JSON files from the specified S3 folder into a DataFrame, data/ is appended to look under full export path
    df_full = spark.read.json(f"{dynamodb_export_bucket_with_prefix}data/")

    # Register the DataFrame as a temporary table for SQL operations
    df_full.createOrReplaceTempView("tmp_full_table")

    # Generate a SQL query to transform the export JSON structure based on provided schema, we are using column name and type from schema items and creating the parsed query dynamically to join and form full load query
    queryFull = ", ".join([f"Item.{col}.{type} as {col}" for col, type in user_schema.items()])

    # Execute the generated SQL query to create a new DataFrame appropriate for Iceberg
    df_full_result = spark.sql(f"SELECT {queryFull} FROM tmp_full_table")

    # Write the resulting DataFrame to the target Iceberg table
    df_full_result.writeTo(f"dev.db.{full_table_name}").using("iceberg").createOrReplace()


if __name__ == "__main__":
    # Check the correct number of command-line arguments are provided
    if len(sys.argv) != 5:
        print("Usage: create_iceberg_from_full_export.py <dynamodb_export_bucket_with_prefix> <iceberg_bucket_with_schema_file_name> <iceberg_table_name> <iceberg_bucket_with_prefix>")
        sys.exit(1)

    # Loading command-line arguments
    dynamodb_export_bucket_with_prefix = sys.argv[1]
    iceberg_bucket_with_schema_file_name = sys.argv[2]
    full_table_name = sys.argv[3]
    iceberg_datalake_bucket = sys.argv[4]

    print(f"Provided Arguments:")
    print(f"  dynamodb_export_bucket_with_prefix: {dynamodb_export_bucket_with_prefix}")
    print(f"  iceberg_bucket_with_schema_file_name: {iceberg_bucket_with_schema_file_name}")
    print(f"  iceberg_table_name: {full_table_name}")
    print(f"  iceberg_bucket_with_prefix: {iceberg_datalake_bucket}")
    print("-----------------------")

    # Validate S3 arguments
    validate_s3_argument(dynamodb_export_bucket_with_prefix, "dynamodb_export_bucket_with_prefix")
    validate_s3_argument(iceberg_bucket_with_schema_file_name, "iceberg_bucket_with_schema_file_name")
    validate_s3_argument(iceberg_datalake_bucket, "iceberg_bucket_with_prefix")

    # Ensure the input path is to the export folder and not the data folder
    if dynamodb_export_bucket_with_prefix.endswith('data/'):
        print("Validation Failed: Please point to the export folder, not the data folder within the export.")
        sys.exit(1)

    # Sanity check that the input path is a full export that has successfully completed,verify if manifest exists
    if not check_manifest_file_in_s3_path(dynamodb_export_bucket_with_prefix):
        print("Validation Failed: The input path either isn't a full export or hasn't completed successfully.")
        sys.exit(1) 

    # Sanity check to ensure the iceberg_datalake_bucket path points to a folder and not a specific file
    if not iceberg_datalake_bucket.endswith('/'):
        print("Validation Failed: The iceberg_bucket_with_prefix should point to a folder. Ensure it ends with a '/'")
        sys.exit(1)

    # Ensure the provided schema path is a file and not a folder (prefix)
    try:
      s3_parts = iceberg_bucket_with_schema_file_name.replace("s3://", "").split("/")
      bucket = s3_parts[0]
      key = "/".join(s3_parts[1:])
      boto3.client('s3').head_object(Bucket=bucket, Key=key)
    except ClientError:
      print("Validation Failed: The iceberg_bucket_with_schema_file_name should point to a file, not a folder.")
      sys.exit(1)
    
    #check if table name is valid per Hive Meta store standards (alphanumeric,underscore only)
    is_valid_table_name(full_table_name)

    print("All parameter validations passed.")

    # Initialize a Spark session. Since it's on EMR, IAM roles grant necessary S3 permissions
    spark_init = SparkSession.builder \
        .appName("Load Schema from S3") \
        .getOrCreate()

    try:
        # Load schema from the provided S3 path
        user_schema = load_schema_file_from_s3(iceberg_bucket_with_schema_file_name, spark_init)
    except Exception as e:
        print(f"Error loading schema: {str(e)}")
        spark_init.stop()
        sys.exit(1)
    
    try:
        # Process and load the full table into Iceberg using the provided schema
        load_full_table(dynamodb_export_bucket_with_prefix, user_schema, full_table_name, iceberg_datalake_bucket)
    except Exception as e:
        print(f"Error processing and loading table into Iceberg: {str(e)}")
        spark_init.stop()
        sys.exit(1)

    # Gracefully stop the Spark session
    spark_init.stop()
