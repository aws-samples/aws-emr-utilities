from pyspark.sql import SparkSession
spark = SparkSession.builder.appName("docker-numpy").getOrCreate()

import numpy as np
a = np.arange(15).reshape(3, 5)
print(a)