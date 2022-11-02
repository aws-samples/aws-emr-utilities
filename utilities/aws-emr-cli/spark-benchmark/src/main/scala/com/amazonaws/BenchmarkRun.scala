package com.amazonaws

import com.databricks.spark.sql.perf.tpcds.{TPCDS, TPCDSTables}
import org.apache.hadoop.fs.Path
import org.apache.log4j.{Level, LogManager}
import org.apache.spark.sql.SparkSession
import org.apache.spark.sql.functions._

import scala.util.Try

object BenchmarkRun {

  def main(args: Array[String]): Unit = {

    val datasetLocation = args(0)
    val resultLocation = args(1)
    val dsdgenLocation = args(2)
    val format = Try(args(3)).getOrElse("parquet")
    val scaleFactor = Try(args(4)).getOrElse("1")
    val iterations = args(5).toInt
    val filterQueries = Try(args(6)).getOrElse("")
    val inputTasks = Try(args(7)).getOrElse("100")

    val logger = LogManager.getRootLogger

    val databaseName = if(scaleFactor.toInt >= 1000) {
      s"tpcds_${format}_${scaleFactor.toInt/1000}tb"
    } else s"tpcds_${format}_${scaleFactor.toInt}gb"

    val timeout = 24 * 60 * 60
    val spark = SparkSession
      .builder
      .appName(s"TPCDS SQL Benchmark - $scaleFactor GB")
      .getOrCreate()

    val tables = new TPCDSTables(spark.sqlContext,
      dsdgenDir = dsdgenLocation,
      scaleFactor = scaleFactor,
      useDoubleForDecimal = false,
      useStringForDate = false)

    val datasetPath = new Path(datasetLocation)
    val fs = datasetPath.getFileSystem(spark.sparkContext.hadoopConfiguration)
    if (! fs.exists(datasetPath)) {
      tables.genData(
        location = datasetLocation,
        format = format,
        overwrite = false,
        partitionTables = true,
        clusterByPartitionColumns = false,
        filterOutNullPartitionValues = false,
        tableFilter = "",
        numPartitions = inputTasks.toInt)
    }

    spark.sql(s"CREATE DATABASE IF NOT EXISTS $databaseName LOCATION '$datasetLocation'")
    tables.createTemporaryTables(datasetLocation, format)

    val tpcds = new TPCDS(spark.sqlContext)
    var query_filter: Seq[String] = Seq()
    if (filterQueries.nonEmpty) {
      logger.info(s"Running only queries: $filterQueries")
      query_filter = filterQueries.split(",").toSeq
    }

    val filtered_queries = query_filter match {
      case Seq() => tpcds.tpcds2_4Queries
      case _ => tpcds.tpcds2_4Queries.filter(q => query_filter.contains(q.name))
    }

    val experiment = tpcds.runExperiment(
      filtered_queries,
      iterations = iterations,
      resultLocation = resultLocation,
      forkThread = true)

    experiment.waitForFinish(timeout)

    // Collect general results
    val resultPath = experiment.resultPath
    val specificResultTable = spark.read.json(resultPath)
    specificResultTable.show()

    // Summarize results
    val result = specificResultTable
      .withColumn("result", explode(col("results")))
      .withColumn("executionSeconds", col("result.executionTime") / 1000)
      .withColumn("queryName", col("result.name"))
    result.select("iteration", "queryName", "executionSeconds").show()

    val aggResults = result.groupBy("queryName").agg(
      callUDF("percentile", col("executionSeconds").cast("double"), lit(0.5)).as('medianRuntimeSeconds),
      callUDF("min", col("executionSeconds").cast("double")).as('minRuntimeSeconds),
      callUDF("max", col("executionSeconds").cast("double")).as('maxRuntimeSeconds)
    ).orderBy(col("queryName"))
    aggResults.repartition(1).write.csv(s"$resultPath/summary.csv")
    aggResults.show(10)

    logger.info(s"Results stored in $resultPath")
  }
}
