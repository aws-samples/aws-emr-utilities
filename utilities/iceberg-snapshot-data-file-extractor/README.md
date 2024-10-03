# Iceberg_Snapshot_Data_File_Extractor

This is a pyspark script which is designed to extract and analyze data file paths from a specified Iceberg table and snapshot ID. It retrieves essential metadata, including the manifest list and individual data file paths, and outputs this information to a text file.

### Why Iceberg_Snapshot_Data_File_Extractor?

- If you wish to get the files associated with a snapshot.
- If you want to replicate a scenario with the data files of a specific snapshot.

### How to use

1. **Download the Script:** `iceberg_snapshot_data_file_extractor_v2.py`

2. **Run the script using the command**:
   ```bash
   spark-submit --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
   --conf spark.sql.catalog.iceberg=org.apache.iceberg.spark.SparkCatalog \
   --conf spark.sql.catalog.iceberg.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog \
   iceberg_snapshot_file_extractor_v2.py <table_name> <snapshot_id>

##### Example:

```bash
spark-submit --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
--conf spark.sql.catalog.iceberg=org.apache.iceberg.spark.SparkCatalog \
--conf spark.sql.catalog.iceberg.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog \
iceberg_snapshot_file_extractor_v2.py iceberg.default.iceberg_mor_table 501388696554197986
```
This will generate the output file in the current directory, named as `<table_name>_<snapshot_id>_datafiles.txt`.

The content of the file will look like:

```ruby
Table Name: <table_name>
Snapshot ID: <snapshot_id>

Manifest List File: s3://bucket/path/manifest_list_file.avro

Manifest Files:
- s3://bucket/path/manifest_file1.avro
- s3://bucket/path/manifest_file2.avro

Data Files:
s3://bucket/path/data_file1.parquet
s3://bucket/path/data_file2.parquet

Total number of data files in snapshot <snapshot_id>: <number>
Total size of all data files: <number> MB
```

##### Example:

See the image below:

![Alt text](images/OutputSample.png?raw=true "OutputSample")
