
# DynamoDB Incremental Exports EMR Spark Iceberg Utility

## Summary

Utility scripts for ingesting data into Iceberg tables via PySpark and AWS S3 are provided in this package. It contains three key scripts to facilitate the ingestion of incremental exports from DynamoDB on S3 into Iceberg tables hosted on AWS Glue or Hive Metastore, utilizing Amazon EMR.

a. The first script (`detect_schema_from_full_export.py`) reads data from Amazon DynamoDB full export on S3 and detects the schema for Table creation in Hive Metastore compatible catalog.

b. The second script (`create_iceberg_from_full_export.py`) initializes your Iceberg data lake table on S3 with a full data export from DynamoDB.

c. The third script (`update_iceberg_from_incremental_export.py`) executes incremental updates on your target Iceberg data lake table and sets the stage for future updates.
Together, these scripts offer a comprehensive solution for managing full and incremental data loads into Iceberg tables.

<img src="img/Screenshot 2023-10-10 at 8.12.45 AM.png" width="600">

## Description
Scripts to perform full table and incremental data loads from DynamoDB extracts. In incremental script, we will provide you 2 approaches to handle data and schema changes to the target table and list down pros/cons for each approach, but we will default the script to pick the robust approach, other one will commented within the script, so you can choose to pick what's right for your use-case.

## Usage

### Detect Schema from Full table export (Optional: Spark Submit Schema detection job)

Iceberg tables are columnar so it's necessary to construct them by enumerating all the columns that might exist across all items in the DynamoDB table. To make this easy, you can run a Spark job that analyzes the full table export and calculates a schema that can fully represent all attributes that exist across all the items in the DynamoDB table. You could also just define the schema manually.

```bash
spark-submit detect_schema_from_full_export.py [input_s3_path_to_export_folder] [input_s3_path_to_schema_file]
```

```
Example:
spark-submit detect_schema_from_full_export.py s3://my-bucket/any-prefix/01234-export-folder/ s3://my-bucket/output-schema.json
```

### Full Table Load
```bash
spark-submit create_iceberg_from_full_export.py [input_s3_path_to_export_folder] [input_s3_path_to_schema_file] [desired_iceberg_table_name] [s3_path_to_iceberg_datalake_bucket]
```

### Incremental Load
```bash
spark-submit update_iceberg_from_incremental_export.py [input_s3_path_to_export_folder] [iceberg_table_name] [input_s3_path_to_schema_file] [s3_path_to_iceberg_datalake_bucket]
```

## Dependencies
* AWS CLI
* Amazon EMR Serverless Application or Amazon EMR on EC2 cluster
* PySpark (Iceberg enabled Spark Cluster For ex. [EMR on EC2](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-iceberg-use-spark-cluster.html), [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/using-iceberg.html))

## User Schema Definition
Define the `user_schema` dictionary in each script to specify the schema of the data.
In this package, rather than using default struct schema provided by DynamoDB exports, we are enforcing schema within the script and dynamically parsing based on this input. You could use the detect schema utility to derive schema based on your input file or define manually per your needs.

### Example Schema
```python
{
    "product_id": "S",
    "quantity": "N",
    "remaining_count": "N",
    "inventory_date": "S",
    "price": "S",
    "product_name": "S"
}
```

## Scripts Details

### 1. Detecting Schema based on DynamoDB JSON for Hive Metastore/Glue Catalog (`detect_schema_from_full_export.py`)
Generates a list of data files from DynamoDB's manifest files for incremental exports. This list aids in updating the table created by the first script, script also include incmrental submit optionally, so you can execute this end-to-end by passing these arguments without having to separate incremental script.

#### Code Path

[generate_file_list_from_ddb_manifest.py](https://github.com/aws-samples/aws-emr-utilities/blob/main/utilities/dynamodb-incremental-exports-emr-iceberg-utility/code/detect_schema_from_full_export.py)


### 2. How Full Table Load Works (`create_iceberg_from_full_export.py`)
Details about how the full table load script functions.

1. Expects 4 arguments
    a. [input_s3_path_to_export_folder] S3 full data file path (For example: s3://{bucket_name}/{prefix}/{export_id}/). 
    It assumes you've provided the full S3 path as an argument. No metadata file is needed in the case of a full load, because this is one-time activity.
    b. [input_s3_path_to_schema_file] S3 path with file name for the schema.
    c. [desired_iceberg_table_name] Table Name to be created in Glue/Hive Metastore in iceberg format.
    d. [s3_path_to_iceberg_datalake_bucket] S3 prefix where iceberg data lake table will be created.
     
3. Read Data into DataFrame
    It reads the JSON data file into a Spark DataFrame.

4. Apply Defined Schema to Temporary View
    Using the user-provided schema, a query is constructed to create a temporary SQL view (`tmp_full_table`) of the DataFrame with the specified        columns and data types.

5. Apply Write Operation to the Target Table (Iceberg)
    The script then writes the DataFrame to the Iceberg table, effectively loading the full table.


#### Code Path

[create_iceberg_from_full_export.py](https://github.com/aws-samples/aws-emr-utilities/blob/main/utilities/dynamodb-incremental-exports-emr-iceberg-utility/code/create_iceberg_from_full_export.py)


### 3. How Incremental Table Load Works (`update_iceberg_from_incremental_export.py`)
Details about how the incremental table load script functions.

1. Expects 4 arguments
    a.  [input_s3_path_to_export_folder] DynamoDB incremental export S3 path.
    b.  [input_s3_path_to_schema_file] Generated or User-provided schema for the JSON table
    c.  [iceberg_table_name] Full Iceberg table name (Scripts automatically appends _stage to the full table name for incremental data in your Hive Metastore for troubleshooting needs)
    d. [s3_path_to_iceberg_datalake_bucket] S3 prefix where iceberg data lake table will be created.

2. Read Data into DataFrame
    It reads this incremental data file into a Spark DataFrame.

3. Apply Defined Schema to Temporary View
   Using the user-provided schema, a query is constructed to create a temporary SQL view (`tmp_stage_table`) of the DataFrame with the specified       columns and data types

4. Apply Write Operation to the Final Table using Merge statements (Iceberg)
    The script then merges this incremental DataFrame into the existing Iceberg table (dev.db.<YOUR_FULL_TABLE_NAME>), taking care of updates,deletes, and inserts as specified.


#### Code Path

[update_iceberg_from_incremental_export.py](https://github.com/aws-samples/aws-emr-utilities/blob/main/utilities/dynamodb-incremental-exports-emr-iceberg-utility/code/update_iceberg_from_incremental_export.py)


## Note
Ensure your PySpark environment is set up properly, and spark-submit is available in the terminal.
