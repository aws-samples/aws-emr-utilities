sudo ln -s `ls /usr/share/aws/emr/emrfs/lib/emrfs-hadoop-assembly*` /usr/lib/spark/jars/emrfs.jar
sudo ln -s `ls /usr/lib/hadoop/hadoop-aws-*` /usr/lib/spark/jars/hadoop-aws.jar
sudo ln -s `ls /usr/share/aws/aws-java-sdk/aws-java-sdk-bundle-*` /usr/share/aws/aws-java-sdk/aws-java-sdk-bundle.jar

aws s3api put-object —-bucket <yourbucketname> —-key sparkhistory/
sudo systemctl stop spark-history-server
sudo systemctl start spark-history-server
