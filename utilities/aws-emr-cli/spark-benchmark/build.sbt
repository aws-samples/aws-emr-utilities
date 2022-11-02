name := "spark-benchmark"

version := "0.1"

scalaVersion := "2.12.8"

val sparkVersion = "3.1.2"

unmanagedBase := baseDirectory.value / "libs"

libraryDependencies ++=Seq(
  "org.apache.spark" %% "spark-core" % sparkVersion % "provided",
  "org.apache.spark" %% "spark-sql" % sparkVersion% "provided"
)
