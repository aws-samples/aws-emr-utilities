import sys
from pyspark.sql import SparkSession
import json, boto3
def load_schema_from_s3(s3_path, spark_session):
    """
    Loads schema from a JSON file located on S3.
    Parameters:
    s3_path: str. Path to the schema JSON file on S3.
    spark_session: SparkSession. Active SparkSession instance to read from S3.
    Returns:
    dict. Schema as a dictionary.
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
def load_full_table(full_file_path, user_schema, full_table_name):
    """
    Loads the full table into Iceberg.
    Parameters:
    full_file_path: str. Path to the input JSON file.
    user_schema: dict. Schema provided by the user.
    full_table_name: str. Name of the destination Iceberg table.
    """
    # Initialize Spark session with necessary configurations for Iceberg
    spark = SparkSession.builder \
        .appName("Load Full Table to Iceberg") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.jars", "/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar") \
        .config("spark.sql.catalog.dev", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.dev.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog") \
        .config("spark.hadoop.hive.metastore.client.factory.class", "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory") \
        .config("spark.sql.catalog.dev.warehouse", "s3://<YOUR-S3-ICEBERG-DATALAKE-BUCKET>/example-prefix/") \
        .getOrCreate()
    # Read JSON file into a DataFrame
    df_full = spark.read.json(full_file_path)
    # Register the DataFrame as a temporary table for SQL operations
    df_full.createOrReplaceTempView("tmp_full_table")
    # Generate SQL query to transform JSON structure based on provided schema
    queryFull = ", ".join([f"Item.{col}.{type} as {col}" for col, type in user_schema.items()])
    # Execute SQL query and create a new DataFrame
    df_full_result = spark.sql(f"SELECT {queryFull} FROM tmp_full_table")
    # Display the first few rows of the resulting DataFrame
    df_full_result.show()
    # Write the resulting DataFrame to an Iceberg table
    df_full_result.writeTo(f"dev.db.{full_table_name}").using("iceberg").createOrReplace()
if __name__ == "__main__":
    """
    Entry point of the script.
    Assumes the script is run with:
    Path to the input JSON file.
    Name of the destination Iceberg table.
    Path to the user schema JSON file on S3.
    """
    # Get command-line arguments
    full_file_path = sys.argv[1]
    full_table_name = sys.argv[2]
    schema_s3_path = sys.argv[3]
    # Initialize a Spark session. Since it's on EMR, IAM roles grant necessary S3 permissions.
    spark_init = SparkSession.builder \
        .appName("Load Schema from S3") \
        .getOrCreate()
    # Load schema from the provided S3 path
    user_schema = load_schema_from_s3(schema_s3_path, spark_init)
    # Process and load the full table into Iceberg using the provided schema
    load_full_table(full_file_path, user_schema, full_table_name)
    # Properly stop the Spark session to free up resources
    spark_init.stop()

