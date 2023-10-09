
# DynamoDB Incremental Exports EMR Spark Iceberg Utility

## Summary

Utility scripts for ingesting data into Iceberg tables via PySpark and AWS S3 are provided in this package. It contains three key scripts to facilitate the ingestion of incremental exports from DynamoDB on S3 into Iceberg tables hosted on AWS Glue or Hive Metastore, utilizing Amazon EMR.

a. The first script (`load_iceberg_full_table.py`) initializes your Iceberg data lake table on S3 with a full data export from DynamoDB.

b. The second script ( `generate_file_list_from_ddb_manifest.py` ) generates a list of data files from DynamoDB's manifest files for incremental exports. This list aids in updating the table created by the first script, script also include incmrental submit optionally, so you can execute this end-to-end by passing these arguments without having to separate incremental script.

c. The third script (`load_iceberg_incremental_data.py`) executes incremental updates on your target Iceberg data lake table and sets the stage for future updates.
Together, these scripts offer a comprehensive solution for managing full and incremental data loads into Iceberg tables.

## Description
Scripts to perform full table and incremental data loads from DynamoDB extracts. In incremental script, we will provide you 2 approaches to handle data and schema changes to the target table and list down pros/cons for each approach, but we will default the script to pick the robust approach, other one will commented within the script, so you can choose to pick what's right for your use-case.

## Usage
### Full Table Load
```bash
spark-submit load_iceberg_full_table.py [full_data_file_path] [full_iceberg_table_name]
```

### Generate File List using DynamoDB Manifests (Optional: Spark Submit Iceberg Job)
```bash
python3 generate_file_list_from_ddb_manifest.py --extractId [DYNAMODB-INCREMENTAL-EXTRACT-ID] --delta_table_name [delta_iceberg_table_name] --full_table_name [full_iceberg_table_name]
```

### Incremental Load
```bash
spark-submit load_iceberg_incremental_data.py [incremental_data_file_path] [delta_iceberg_table_name] [full_iceberg_table_name]
```

## Dependencies
* AWS CLI
* Amazon EMR Serverless Application or Amazon EMR on EC2 cluster
* PySpark (Iceberg enabled Spark Cluster For ex. [EMR on EC2](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-iceberg-use-spark-cluster.html), [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/using-iceberg.html))

## User Schema Definition
Define the `user_schema` dictionary in each script to specify the schema of the data.
In this package, rather than using default struct schema provided by DynamoDB exports, we are enforcing schema within the script and dynamically parsing based on this input. 

### Example Schema
```python
user_schema = {
    "product_id": "S",
    "quantity": "N",
    "remaining_count": "N",
    "inventory_date": "S",
    "price": "S",
    "product_name": "S"
}
```

## Scripts Details
### 1. How Full Table Load Works (`load_iceberg_full_table.py`)
Details about how the full table load script functions.

1. Expects 3 arguments
    a. S3 full data file path (For example: s3://{bucket_name}/{prefix}/{export_id}/). 
    It assumes you've provided the full S3 path as an argument. No metadata file is needed in the case of a full load, because this is one-time activity.
    b. Table Name to be created in Glue/Hive Metastore in iceberg format.
    c. User provided schema for target table in the script.

2. Read Data into DataFrame
    It reads the JSON data file into a Spark DataFrame.

3. Apply Defined Schema to Temporary View
    Using the user-provided schema, a query is constructed to create a temporary SQL view (`tmp_full_table`) of the DataFrame with the specified        columns and data types.

4. Apply Write Operation to the Target Table (Iceberg)
    The script then writes the DataFrame to the Iceberg table, effectively loading the full table.


#### Code Path

[load_iceberg_full_table.py](https://github.com/knkarthik01/aws-emr-utilities/blob/main/utilities/dynamodb-incremental-exports-emr-iceberg-utility/code/load_iceberg_full_table.py)

### 2. Generating Data File List for Incremental Load (`generate_file_list_from_ddb_manifest.py`)
Generates a list of data files from DynamoDB's manifest files for incremental exports. This list aids in updating the table created by the first script, script also include incmrental submit optionally, so you can execute this end-to-end by passing these arguments without having to separate incremental script.

#### Code Path

[generate_file_list_from_ddb_manifest.py](https://github.com/knkarthik01/aws-emr-utilities/blob/main/utilities/dynamodb-incremental-exports-emr-iceberg-utility/code/generate_file_list_from_ddb_manifest.py)


### 3. How Incremental Table Load Works (`load_iceberg_incremental_data.py`)
Details about how the incremental table load script functions.

1. Expects 4 Parameters
    a.  DynamoDB incremental extract path.
    b.  User-provided schema for the JSON table
    c.  Full Iceberg table name and Incremental Iceberg table name (Script expects both Iceberg full table name and Incremental table name to have     you decide on naming conventions you want to persist in your metastore)
    d.  Partition key and Sort key (option) to create merge queries dynamically.

2. Read Data into DataFrame
    It reads this incremental data file into a Spark DataFrame.

3. Apply Defined Schema to Temporary View
   Using the user-provided schema, a query is constructed to create a temporary SQL view (`tmp_stage_table`) of the DataFrame with the specified       columns and data types

4. Apply Write Operation to the Final Table using Merge statements (Iceberg)
    The script then merges this incremental DataFrame into the existing Iceberg table (dev.db.<YOUR_FULL_TABLE_NAME>), taking care of updates,         deletes, and inserts as specified.


#### Code Path

[load_iceberg_incremental_data.py](https://github.com/knkarthik01/aws-emr-utilities/blob/main/utilities/dynamodb-incremental-exports-emr-iceberg-utility/code/load_iceberg_incremental_data.py)


## Note
Ensure your PySpark environment is set up properly, and spark-submit is available in the terminal.
