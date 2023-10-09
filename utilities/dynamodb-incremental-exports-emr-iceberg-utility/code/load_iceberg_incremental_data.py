# Imports libraries for Spark, AWS operations, and logging.
from pyspark.sql import SparkSession
import sys
import json, boto3, logging, subprocess # nosec

def read_metadata(s3_delta_manifest_file_path):
    # Downloads and reads a metadata file from S3, extracting paths to the data files.
    # Returns: List of S3 paths to the data files.
    metadata_file = "manifest-files.json"
    aws_command = f"aws s3 cp '{s3_delta_manifest_file_path}/{metadata_file}' -"
    metadata_content = subprocess.getoutput(aws_command) # nosec
    spark_paths = []
    for line in metadata_content.split("\n"):
        data_file_path = json.loads(line).get('dataFileS3Key')
        if data_file_path:
            spark_paths.append(f"s3://{s3_bucket}/{data_file_path}")
    logging.info(f"Data files are located at: {', '.join(spark_paths)}")
    return spark_paths

def load_schema_from_s3(s3_path, spark_session):
    """
    Loads schema from a JSON file located on S3.
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

def extract_keys_from_delta_file(spark, data_file_path):
    # Read the first line from the delta file
    first_line = spark.read.text(data_file_path).head()[0]
    # Parse the first line (assuming it's in JSON format)
    first_record = json.loads(first_line)
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

def load_incremental(spark, data_file_path, user_schema, delta_table_name, full_table_name, partition_key, sort_key=None):
    df = spark.read.json(data_file_path)
    df.createOrReplaceTempView("stg_table")
    # Create DataFrame with user-provided schema
    queryDelta = ", ".join([f"NewImage.{col}.{type} as {col}" for col, type in user_schema.items()])
    keys_query = [f"Keys.{pk}.{user_schema[pk]} as Keys_{pk}" for pk in partition_key]
    if sort_key:
        for sk in sort_key:
            keys_query.append(f"Keys.{sk}.{user_schema[sk]} as Keys_{sk}")
    queryDelta = ', '.join(keys_query) + ", " + queryDelta
    df_stg_result = spark.sql(f"SELECT {queryDelta} FROM stg_table")
    df_stg_result.show()
    # Write to Iceberg tables
    df_stg_result.writeTo(f"dev.db.{delta_table_name}").using("iceberg").createOrReplace()
    # Merge logic
    join_condition = ' AND '.join([f"target.{pk} = source.Keys_{pk}" for pk in partition_key])
    if sort_key:
        join_condition += ' AND ' + ' AND '.join([f"target.{sk} = source.Keys_{sk}" for sk in sort_key])
    
    delete_conditions = [f"source.{key} is null" for key in partition_key]
    if sort_key:
	    delete_conditions += [f"source.{key} is null" for key in sort_key]
    delete_condition_str = ' AND '.join(delete_conditions)
    merge_query = f"""
    MERGE INTO dev.db.{full_table_name} AS target
    USING dev.db.{delta_table_name} AS source
    ON {join_condition}
    WHEN MATCHED AND {delete_condition_str} THEN DELETE
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """ 
    
    print(merge_query)

    spark.sql(merge_query)

if __name__ == "__main__":
    s3_delta_manifest_file_path = sys.argv[1]
    delta_table_name = sys.argv[2]
    full_table_name = sys.argv[3]
    schema_s3_path = sys.argv[4]
    s3_bucket = sys.argv[5]
    
    # Initialize a Spark session. Since it's on EMR, IAM roles grant necessary S3 permissions.
    spark = SparkSession.builder \
        .appName("Load Full Table to Iceberg") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.jars", "/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar") \
        .config("spark.sql.catalog.dev", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.dev.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog") \
        .config("spark.hadoop.hive.metastore.client.factory.class", "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory") \
        .config("spark.sql.catalog.dev.warehouse", "s3://{s3_bucket}/example-prefix/") \
        .getOrCreate()

    # Load schema from the provided S3 path
    user_schema = load_schema_from_s3(schema_s3_path, spark)

    # Read from manifest file and get all data file paths
    data_file_paths = read_metadata(s3_delta_manifest_file_path)
    if data_file_paths:
        # Call the function to extract keys from the first delta file
        partition_key, sort_key = extract_keys_from_delta_file(spark, data_file_paths[0])
        partition_key = [partition_key]
        sort_key = [sort_key] if sort_key else None
        # Loop through all data files for incremental load
        for data_file_path in data_file_paths:
            load_incremental(spark, data_file_path, user_schema, delta_table_name, full_table_name, partition_key, sort_key)
    else:
        logging.warning("No data files found in the manifest.")


