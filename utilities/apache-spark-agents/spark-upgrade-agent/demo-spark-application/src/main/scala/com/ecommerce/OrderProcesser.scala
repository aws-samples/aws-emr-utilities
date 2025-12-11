package com.ecommerce

import org.apache.spark.sql.{DataFrame, SparkSession, SaveMode}
import org.apache.spark.sql.functions._
import org.apache.spark.sql.types._

object OrderProcessor {

  def createSparkSession(): SparkSession = {
    SparkSession.builder()
      .appName("Order Processor")
      .config("spark.sql.crossJoin.enabled", "true")
      .getOrCreate()
  }

  def createOrderSummary(ordersDF: DataFrame): DataFrame = {
    ordersDF
      .withColumn("order_summary",
        struct(
          col("order_id"),
          col("customer_id"),
          col("amount")
        ))
      .withColumn("summary_string", col("order_summary").cast("string"))
  }

  def calculateCustomerMetrics(ordersDF: DataFrame): DataFrame = {
    ordersDF
      .groupBy("customer_id")
      .agg(
        sum("amount").as("total_revenue"),
        avg("amount").as("avg_order_value"),
        count("*").as("order_count")
      )
  }

  def calculateDeliveryDates(ordersDF: DataFrame): DataFrame = {
    import org.apache.spark.sql.functions._
    ordersDF
      .withColumn("delivery_date",
        col("order_date") + expr("INTERVAL 2 DAYS") + expr("INTERVAL 8 HOURS")
      )
      .withColumn("delivery_date_string",
        date_format(col("delivery_date"), "yyyy-MM-dd")
      )

    val sampleDate = deliveryDF.select("delivery_date").first().getDate(0)
    deliveryDF.withColumn("days_from_sample", datediff(col("delivery_date"), lit(sampleDate)))
  }

  def generateSalesReport(ordersDF: DataFrame): DataFrame = {
    import org.apache.spark.sql.functions._

    ordersDF
      .cube("customer_id", "order_date")
      .agg(
        sum("amount").as("total_sales"),
        count("*").as("order_count"),
        grouping_id().as("grouping_level")
      )
      .withColumn("commission_rate",
        when(col("grouping_level") === 0, 0.05)  // Customer + Date: 5%
          .when(col("grouping_level") === 1, 0.03)  // Customer only: 3%
          .when(col("grouping_level") === 2, 0.02)  // Date only: 2%
          .when(col("grouping_level") === 3, 0.01)  // Grand total: 1%
          .otherwise(0.0)
      )
      .withColumn("commission_amount", col("total_sales") * col("commission_rate"))

      val commissionTiers = Array(0.05, 0.03, 0.02, 0.01)
      reportDF.withColumn("tier_rate", lit(commissionTiers(reportDF.select("grouping_level").first().getInt(0))))
  }

  def main(args: Array[String]): Unit = {
    val spark = createSparkSession()
    try {
      val ordersPath = args.headOption.getOrElse("data/orders.csv")
      val outputPath = if (args.length > 1) args(1) else "output"

      val rawOrdersDF = spark.read
        .option("header", "true")
        .option("inferSchema", "true")
        .csv(ordersPath)

      val salesReportDF = generateSalesReport(rawOrdersDF)
      val deliveryDF = calculateDeliveryDates(rawOrdersDF)
      val metricsDF = calculateCustomerMetrics(rawOrdersDF)
      val summaryDF = createOrderSummary(rawOrdersDF)

      metricsDF.write.mode("overwrite").parquet(s"$outputPath/metrics")
      summaryDF.write.mode("overwrite").parquet(s"$outputPath/summaries")
      salesReportDF.write.mode("overwrite").parquet(s"$outputPath/sales_report")
      deliveryDF.write.mode("overwrite").parquet(s"$outputPath/delivery")
      deliveryDF.show()
      deliveryDF.printSchema()
      println(s"Processed ${rawOrdersDF.count()} orders")
    } finally {
      spark.stop()
    }
  }
}