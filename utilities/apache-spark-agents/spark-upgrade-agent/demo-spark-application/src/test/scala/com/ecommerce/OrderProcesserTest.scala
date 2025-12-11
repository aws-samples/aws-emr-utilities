package com.ecommerce

import org.apache.spark.sql.{SparkSession, Row}
import org.apache.spark.sql.types._
import org.scalatest.funsuite.AnyFunSuite
import org.scalatest.BeforeAndAfterAll
import scala.collection.JavaConverters._

class OrderProcessorTest extends AnyFunSuite with BeforeAndAfterAll {

  var spark: SparkSession = _

  override def beforeAll(): Unit = {
    spark = SparkSession.builder()
      .appName("OrderProcessorTest")
      .master("local[*]")
      .getOrCreate()
  }

  override def afterAll(): Unit = {
    if (spark != null) {
      spark.stop()
    }
  }

  private def createTestOrders(): org.apache.spark.sql.DataFrame = {
    val schema = StructType(Seq(
      StructField("order_id", StringType),
      StructField("customer_id", StringType),
      StructField("amount", DoubleType),
      StructField("order_date", StringType)
    ))

    val data = Seq(
      Row("ORD-001", "CUST-100", 150.00, "2024-01-15"),
      Row("ORD-002", "CUST-100", 75.50, "2024-01-20"),
      Row("ORD-003", "CUST-200", 200.00, "2024-01-18")
    )

    spark.createDataFrame(data.asJava, schema)
  }

  test("generateSalesReport should create commission report with grouping_id") {
    val ordersDF = createTestOrders()
    val result = OrderProcessor.generateSalesReport(ordersDF)

    val totalRows = result.count()
    assert(totalRows > 3, s"Expected more than 3 rows from CUBE aggregation, got $totalRows")

    val groupingLevels = result.select("grouping_level").distinct().collect().map(_.getInt(0)).sorted
    assert(groupingLevels.contains(0), "Should contain grouping level 0 (customer + date)")
    assert(groupingLevels.contains(3), "Should contain grouping level 3 (grand total)")
  }

  test("grouping_id should return Int type (Spark 3.0) vs Long type (Spark 3.5+)") {
    val ordersDF = createTestOrders()
    val result = OrderProcessor.generateSalesReport(ordersDF)

    val firstRow = result.first()
    val groupingValue = firstRow.getAs[Any]("grouping_level")

    assert(groupingValue.isInstanceOf[Int],
      s"Expected Int type for grouping_id in Spark 3.0, but got ${groupingValue.getClass.getSimpleName}")
  }

  test("calculateDeliveryDates should return date type (Spark 3.0) vs timestamp type (Spark 3.5+)") {
    val schema = StructType(Seq(
      StructField("order_id", StringType),
      StructField("customer_id", StringType),
      StructField("amount", DoubleType),
      StructField("order_date", DateType)
    ))

    val data = Seq(
      Row("ORD-001", "CUST-100", 150.00, java.sql.Date.valueOf("2024-01-15")),
      Row("ORD-002", "CUST-100", 75.50, java.sql.Date.valueOf("2024-01-20"))
    )

    val ordersDF = spark.createDataFrame(data.asJava, schema)
    val result = OrderProcessor.calculateDeliveryDates(ordersDF)

    val deliveryDateField = result.schema.fields.find(_.name == "delivery_date").get

    assert(deliveryDateField.dataType == DateType,
      s"Expected DateType for delivery_date in Spark 3.0, but got ${deliveryDateField.dataType}. " +
      s"In Spark 3.5+, date + interval returns TimestampType instead of DateType.")
  }

  test("createOrderSummary should format struct with square brackets") {
    val ordersDF = createTestOrders()
    val result = OrderProcessor.createOrderSummary(ordersDF)

    val order1 = result.filter("order_id = 'ORD-001'").first()
    val summaryString = order1.getAs[String]("summary_string")

    assert(summaryString.startsWith("["), s"Expected '[' at start, got: $summaryString")
    assert(summaryString.endsWith("]"), s"Expected ']' at end, got: $summaryString")
  }

  test("calculateCustomerMetrics should aggregate correctly") {
    val ordersDF = createTestOrders()
    val result = OrderProcessor.calculateCustomerMetrics(ordersDF)

    val cust100 = result.filter("customer_id = 'CUST-100'").first()
    val totalRevenue = cust100.getAs[Double]("total_revenue")
    val orderCount = cust100.getAs[Long]("order_count")

    assert(Math.abs(totalRevenue - 225.50) < 0.01, s"Expected 225.50, got $totalRevenue")
    assert(orderCount == 2, s"Expected 2 orders, got $orderCount")
  }
}