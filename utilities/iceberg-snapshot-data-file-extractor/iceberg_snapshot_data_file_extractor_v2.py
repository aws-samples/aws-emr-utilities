from pyspark.sql import SparkSession
from pyspark.sql.functions import col
import sys

# Initialize Spark session
spark = SparkSession.builder.appName("Iceberg Snapshot Files").getOrCreate()

# Function to get data file paths, sizes (in bytes), and total size from a snapshot
def get_data_files_from_snapshot(table_name, snapshot_id):
    # Step 1: Get the manifest list for the provided snapshot ID from the specified table
    manifest_list = spark.sql(f"""
        SELECT manifest_list 
        FROM {table_name}.snapshots 
        WHERE snapshot_id = {snapshot_id}
    """).collect()[0][0]

    # Step 2: Read the manifest file as a DataFrame
    manifest_df = spark.read.format("avro").load(manifest_list)

    # Step 3: Extract all the manifest paths
    manifest_paths = [row['manifest_path'] for row in manifest_df.select("manifest_path").collect()]

    total_files_count = 0  # Initialize total file count
    total_size_in_bytes = 0  # Initialize total size counter in bytes
    data_files_list = []  # List to store file paths and sizes
    manifest_files = []  # List to store manifest file names

    # Step 4: Loop through each manifest file and extract file paths and sizes
    for path in manifest_paths:
        manifest_files.append(path)  # Store manifest file name

        # Load each manifest file as Avro format
        data_files_df = spark.read.format("avro").load(path)

        # Select relevant fields such as file path and file size
        data_files = data_files_df.select(col("data_file.file_path"), col("data_file.file_size_in_bytes")).collect()

        # Add each file path and size (in bytes) to the list, and sum the sizes
        for data_file in data_files:
            file_size_bytes = data_file['file_size_in_bytes']  # Keep size in bytes
            data_files_list.append((data_file['file_path'], file_size_bytes))
            total_size_in_bytes += file_size_bytes

        # Count the number of data files in this manifest and add to the total count
        manifest_file_count = data_files_df.count()
        total_files_count += manifest_file_count

    # Step 5: Generate the output file name based on the table name, snapshot ID, and file count
    output_filename = f"{table_name.replace('.', '_')}_{snapshot_id}_{total_files_count}_datafiles.txt"

    # Convert the total size to MB for the summary
    total_size_in_mb = total_size_in_bytes / (1024 * 1024)

    # Step 6: Write the table name, snapshot ID, manifest file names, data file paths, sizes (in bytes), and total size to the output file
    with open(output_filename, "w") as output_file:
        # Write the table name and snapshot ID
        output_file.write(f"Table Name: {table_name}\n")
        output_file.write(f"Snapshot ID: {snapshot_id}\n\n")

        # Write the manifest list file name
        output_file.write(f"Manifest List File: {manifest_list}\n\n")

        # Write each manifest file name
        output_file.write("Manifest Files:\n")
        for manifest_file in manifest_files:
            output_file.write(f"- {manifest_file}\n")

        output_file.write("\nData Files (with sizes in bytes):\n")
        # Write each data file path and size (in bytes)
        for file_path, file_size_bytes in data_files_list:
            output_file.write(f"{file_path} (Size: {file_size_bytes} bytes)\n")

        # Write the total number of data files and total size
        output_file.write(f"\nTotal number of data files in snapshot {snapshot_id}: {total_files_count}\n")
        output_file.write(f"Total size of all data files: {total_size_in_mb:.2f} MB\n")

    print(f"Output written to {output_filename}")

# Main execution
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: get_snapshot_files.py <table_name> <snapshot_id>")
        sys.exit(1)

    # Get table name and snapshot ID from command-line arguments
    table_name = sys.argv[1]
    snapshot_id = int(sys.argv[2])

    # Call the function with the provided table name and snapshot ID
    get_data_files_from_snapshot(table_name, snapshot_id)

    # Stop the Spark session
    spark.stop()

