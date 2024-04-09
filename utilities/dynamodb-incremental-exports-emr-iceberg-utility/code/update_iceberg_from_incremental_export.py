# This script reads a DynamoDB incremental export and updates an Apache Iceberg table.
# It requires the schema that was detected using detect_schema_from_full_export.py
#
# Suggest using EMR Serverless with Apache Spark for parallel execution.
# 
# Script expects input_export_path, 
# iceberg_table_name (target iceberg table),
# schema_file, iceberg_bucket_with_prefix is bucket with prefix where iceberg 
# data lake is hosted
#
# Usage: update_iceberg_from_incremental_export.py <dynamodb_export_bucket_with_prefix> <iceberg_bucket_with_schema_file_name> <iceberg_table_name> <iceberg_bucket_with_prefix>
# Example: spark-submit update_iceberg_from_incremental_export.py "s3://dynamodb-export-bucket/optional-prefix/01234-export-folder/" "s3://iceberg-bucket/prefix/schema.json" "full_table_name" "s3://iceberg-bucket/example-prefix/"
# Note on schema evolution in Iceberg: if your schema is evolving frequently in DDB, consider updating iceberg table property to 'write.spark.accept-any-schema'='true' in your iceberg Catalog. Refer: https://iceberg.apache.org/docs/latest/spark-writes/#schema-merge

from pyspark.sql import SparkSession
import sys
import json, boto3, logging
from botocore.exceptions import ClientError
from pyspark.sql.utils import AnalysisException

# Function to validate S3 URI
def validate_s3_argument(s3_arg, arg_name):
    if not s3_arg.startswith("s3://"):
        print(f"The argument {arg_name} does not start with 's3://'. Please provide a valid S3 URI.")
        sys.exit(1)

# Check if the table name contains only alphanumeric characters or underscores
def is_valid_table_name(table_name):
    # Check if the table name contains only lowercase alphanumeric characters or underscores
    if all(char.isalnum() or char == "_" for char in table_name) and table_name.islower():
        return "Table name is valid."
    else:
        print("Invalid table name. Please use only numbers,lowercase characters, or underscores in table name.")
        sys.exit(1)

# Function to download and read a metadata file from S3
def read_metadata(s3_export_folder):
    """
    Extracts paths to data files from the manifest present in the provided S3 export folder.
    Parameters:
    s3_export_folder: str. Path to the S3 folder containing the export.
    Returns: List of S3 paths to the data files.
    """
    # Extract bucket and key prefix from s3_export_folder
    s3_parts = s3_export_folder.replace("s3://", "").split("/")
    s3_bucket = s3_parts[0]
    s3_key_prefix = "/".join(s3_parts[1:])
    # Automatically point to the manifest file in incremental exports
    metadata_file_key = f"{s3_key_prefix}manifest-files.json"
    # Fetch manifest file from S3 using boto3
    s3_client = boto3.client('s3')
    response = s3_client.get_object(Bucket=s3_bucket, Key=metadata_file_key)
    metadata_content = response['Body'].read().decode('utf-8')
    spark_paths = []
    for line in metadata_content.split("\n"):
        try:
            # Extract the data file path for each valid line
            data_file_path = json.loads(line).get('dataFileS3Key')
            if data_file_path:
                spark_path = f"s3://{s3_bucket}/{data_file_path}"
                spark_paths.append(spark_path)
        except json.JSONDecodeError:
            continue  # Ignore lines that are not valid JSON
    # Log the result and return the spark paths
    logging.info(f"Data files are located at: {', '.join(spark_paths)}")
    return spark_paths

def load_schema_from_s3(s3_path, spark_session):
  """
  Loads schema from a JSON file located on S3
  Parameters:
  s3_path: str. Path to the schema JSON file on S3.
  spark_session: SparkSession. Active SparkSession instance to read from S3.
  Returns:
  Schema.
  """
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

# Function to extract keys from the first line of delta file
def extract_keys_from_delta_file(spark, data_file_path):
    """
    Extracts primary and sort keys from a delta file.
    """
    first_line = spark.read.text(data_file_path).head()
    # Check if the file is empty or the first line is an empty JSON
    if not first_line or not first_line[0].strip() or first_line[0] == '{}':
        print("Incremental export includes no changes to process. Exiting gracefully.")
        sys.exit(0)
    first_record = json.loads(first_line[0])
    # Check if the record has "Keys" to determine its structure
    if "Keys" in first_record:
        # Extract all keys present in the "Keys" dictionary
        keys_list = list(first_record["Keys"].keys())
        if len(keys_list) == 0:
            raise ValueError("No keys found in the 'Keys' dictionary.")
        partition_key = keys_list[0]
        sort_key = keys_list[1] if len(keys_list) > 1 else None
    else:
        raise ValueError("Unexpected data structure in the delta file.")
    return partition_key, sort_key

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

# Function to load incremental data and update Iceberg tables
def load_incremental(spark, data_file_path, user_schema, delta_table_name, full_table_name, partition_key, sort_key=None):
    df = spark.read.json(data_file_path)
    df.createOrReplaceTempView("stg_table")

    # Function to get common columns between the inferred schema and user schema
    def get_common_columns(df, user_schema):
        inferred_columns = [field.name for field in df.schema["NewImage"].dataType.fields]
        user_columns = list(user_schema.keys())
        common_columns = set(inferred_columns).intersection(set(user_columns))
        return list(common_columns)
    common_columns = get_common_columns(df, user_schema)
    common_columns_with_types = {col: user_schema[col] for col in common_columns}

    queryDelta = ", ".join([f"NewImage.{col}.{dtype} as {col}" for col, dtype in common_columns_with_types.items()])

    # Create DataFrame using the user-provided schema
    # Dynamically construct the query based on schema and keys extracted

    keys_query = [f"Keys.{pk}.{user_schema[pk]} as Keys_{pk}" for pk in partition_key]
    if sort_key:
        for sk in sort_key:
            keys_query.append(f"Keys.{sk}.{user_schema[sk]} as Keys_{sk}")
    queryDelta = ', '.join(keys_query) + ", " + queryDelta
    df_stg_result = spark.sql(f"SELECT {queryDelta} FROM stg_table")

    # Write to Iceberg table 
    df_stg_result.writeTo(f"dev.db.{delta_table_name}").using("iceberg").createOrReplace()


    # Use the comprehensive dtype_mapping
    dtype_mapping = {
        'S': 'STRING',
        'N': 'DOUBLE',
        'B': 'BINARY',
        'BOOL': 'BOOLEAN',
        'NULL': 'STRING',
        'L': 'ARRAY<STRING>',
        'M': 'MAP<STRING, STRING>',
        'BS': 'BINARY',
        'NS': 'ARRAY<DOUBLE>',
        'SS': 'ARRAY<STRING>',
        'NUL': 'STRING',
        'BOOL': 'BOOLEAN',
    }

    # Get the existing columns in the target table
    existing_columns = [row.col_name for row in spark.sql(f"DESCRIBE dev.db.{full_table_name}").collect()]

    # Add new columns to the target table if they don't exist
    for col, dtype in user_schema.items():
        if col not in existing_columns:
            try:
                spark_dtype = dtype_mapping.get(dtype, dtype)
                spark.sql(f"ALTER TABLE dev.db.{full_table_name} ADD COLUMN {col} {spark_dtype}")
            except AnalysisException as e:
                print(f"Error while adding column '{col}' to the table '{full_table_name}': {str(e)}")
        else:
            print(f"Column '{col}' already exists in the table '{full_table_name}'. Skipping column addition.")

    # Merge logic, we will prepare join_conditions and delete conditions for final merge
    # We are looking up for sort_key existence to dynamically build conditions
    join_condition = ' AND '.join([f"target.{pk} = source.Keys_{pk}" for pk in partition_key])
    if sort_key:
        join_condition += ' AND ' + ' AND '.join([f"target.{sk} = source.Keys_{sk}" for sk in sort_key])
    
    delete_conditions = [f"source.{key} is null" for key in partition_key]
    if sort_key:
	    delete_conditions += [f"source.{key} is null" for key in sort_key]
    delete_condition_str = ' AND '.join(delete_conditions)

    update_columns = ", ".join([f"target.{col} = source.{col}" for col in common_columns])
    insert_columns_names = ", ".join(list(user_schema.keys()))
    insert_columns_values = ", ".join([f"source.{col}" if col in common_columns else "NULL" for col in list(user_schema.keys())])

    # Iceberg: MERGE INTO updates a table, called the target table(full_table_name), using a set of updates from another query, called the source(delta_table_name). The update for a row in the target table is found using the ON clause that is like a join condition.
    merge_query = f"""
    MERGE INTO dev.db.{full_table_name} AS target
    USING dev.db.{delta_table_name} AS source
    ON {join_condition}
    WHEN MATCHED AND {delete_condition_str} THEN DELETE
    WHEN MATCHED THEN UPDATE SET {update_columns}
    WHEN NOT MATCHED THEN INSERT ({insert_columns_names}) 
    VALUES ({insert_columns_values})
    """ 
    
    # Execute merge query
    spark.sql(merge_query)

if __name__ == "__main__":
    # Check the correct number of command-line arguments are provided
    if len(sys.argv) != 5:
        print("Usage: update_iceberg_from_incremental_export.py <dynamodb_export_bucket_with_prefix> <iceberg_table_name> <iceberg_bucket_with_schema_file_name> <iceberg_bucket_with_prefix>")
        sys.exit(1)

    s3_delta_manifest_file_path = sys.argv[1]
    schema_s3_path = sys.argv[2]
    full_table_name = sys.argv[3]
    iceberg_bucket_with_prefix = sys.argv[4]
    delta_table_name = f"{full_table_name}_stage"
   
    print(f"Provided Arguments:")
    print(f"  s3_delta_manifest_file_path: {s3_delta_manifest_file_path}")
    print(f"  schema_s3_path: {schema_s3_path}")
    print(f"  iceberg_table_name: {full_table_name}")
    print(f"  iceberg_bucket_with_prefix: {iceberg_bucket_with_prefix}")
    print("-----------------------")
 
    # Validate S3 arguments
    validate_s3_argument(s3_delta_manifest_file_path, "dynamodb_export_bucket_with_prefix")
    validate_s3_argument(schema_s3_path, "iceberg_bucket_with_schema_file_name")
    validate_s3_argument(iceberg_bucket_with_prefix, "iceberg_bucket_with_prefix")

    # Ensure the input path is to the export folder and not the data folder
    if s3_delta_manifest_file_path.endswith('data/'):
        print("Validation Failed: Please point to the export folder, not the data folder within the export.")
        sys.exit(1)

    # Sanity check: input path is a ddb incremental export that has successfully completed,verify if manifest exists
    if not check_manifest_file_in_s3_path(s3_delta_manifest_file_path):
        print("Validation Failed: The input path either isn't a ddb incremental export or hasn't completed successfully.")
        sys.exit(1)

    # Sanity check to ensure the iceberg_bucket_with_prefix path points to a folder and not a specific file
    if not iceberg_bucket_with_prefix.endswith('/'):
        print("Validation Failed: The iceberg_bucket_with_prefix should point to a folder. Ensure it ends with a '/'")
        sys.exit(1)

    # Ensure the provided schema path is a file and not a folder (prefix)
    try:
      s3_parts = schema_s3_path.replace("s3://", "").split("/")
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
    spark = SparkSession.builder \
        .appName("Load incremental export to Iceberg") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.jars", "/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar") \
        .config("spark.sql.catalog.dev", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.dev.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog") \
        .config("spark.hadoop.hive.metastore.client.factory.class", "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory") \
        .config("spark.sql.catalog.dev.warehouse", f"{iceberg_bucket_with_prefix}") \
        .getOrCreate()

    try:
        # Load schema from the provided S3 path
        user_schema = load_schema_from_s3(schema_s3_path, spark) 
    except Exception as e:
        print(f"Error loading schema: {str(e)}")
        spark.stop()
        sys.exit(1)
        
    # Read from the manifest file and get all the exact data file paths
    data_file_paths = read_metadata(s3_delta_manifest_file_path)
    if data_file_paths:
        # Call the function to extract keys from the first delta file
        partition_key, sort_key = extract_keys_from_delta_file(spark, data_file_paths[0])
        partition_key = [partition_key]
        sort_key = [sort_key] if sort_key else None
        # Loop through all data files for incremental load
        load_incremental(spark, data_file_paths, user_schema, delta_table_name, full_table_name, partition_key, sort_key)
    else:
        logging.warning("No data files found in the manifest.")
    
    # Gracefully shutdown Spark
    spark.stop()
