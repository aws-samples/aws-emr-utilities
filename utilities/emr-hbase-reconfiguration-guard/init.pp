# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

class hadoop_hbase {

  class deploy ($roles, $auxiliary = true) {
    if ("hbase-server" in $roles) {
      include hadoop_hbase::server
    }

    if ("hbase-master" in $roles) {
      if ($auxiliary == true) {
        include hadoop_zookeeper::server
      }

      include hadoop_hbase::master
      Exec <| title == 'init hdfs' |> -> Service['hbase-master']
    }

    if ("hbase-client" in $roles) {
      include hadoop_hbase::client
      include hbase_operator_tools::library
      include emr_wal_cli::library
    }

    if ("hbase-thrift-server" in $roles) {
      include hadoop_hbase::thrift_server
    }

    if ("hbase-rest-server" in $roles) {
      include hadoop_hbase::rest_server
    }

  }

  class client_package  {
    package { "hbase":
      ensure => present,
    }
  }

  class common_config (
    $hdfsdir = hiera('bigtop::hadoop_namenode_uri'),
    $rootdir,
    $zookeeper_quorum,
    $kerberos_realm = "",
    $heap_size = "1024",
    $hbase_site_overrides = {},
    $hbase_env_overrides = {},
    $hbase_log4j2_overrides = {},
    $hbase_metrics_overrides = {},
    $hbase_policy_overrides = {},
    $on_s3 = false,
    $hbase_data_dirs = suffix(hiera('emr::apps_mounted_dirs'), "/hbase"),
    $use_phoenix_query_server = false,
  ) {
    include hadoop_hbase::client_package
    if ($kerberos_realm and $kerberos_realm != "") {
      require kerberos::client
      kerberos::host_keytab { "hbase": 
        spnego => true,
        require => Package["hbase"],
      }

      file { "/etc/hbase/conf/jaas.conf":
        content => template("hadoop_hbase/jaas.conf"),
        require => Package["hbase"],
      }
    }

    bigtop_file::site { "/etc/hbase/conf/hbase-site.xml":
      content => template("hadoop_hbase/hbase-site.xml"),
      overrides => $hbase_site_overrides,
      require => Package["hbase"],
    }

    bigtop_file::env { "/etc/hbase/conf/hbase-env.sh":
      content => template("hadoop_hbase/hbase-env.sh"),
      overrides => $hbase_env_overrides,
      require => Package["hbase"],
    }

    bigtop_file::properties { "/etc/hbase/conf/log4j2.properties":
      source => '/etc/hbase/conf/log4j2.properties',
      overrides => $hbase_log4j2_overrides,
      require => Package["hbase"],
    }

    bigtop_file::properties { "/etc/hbase/conf/hadoop-metrics2-hbase.properties":
      source => '/etc/hbase/conf/hadoop-metrics2-hbase.properties',
      overrides => $hbase_metrics_overrides,
      require => Package["hbase"],
    }

    bigtop_file::site { "/etc/hbase/conf/hbase-policy.xml":
      source => '/etc/hbase/conf/hbase-policy.xml',
      overrides => $hbase_policy_overrides,
      require => Package["hbase"],
    }

    hadoop::create_storage_dir { $hadoop_hbase::common_config::hbase_data_dirs: } ->
    file { $hadoop_hbase::common_config::hbase_data_dirs:
      ensure => directory,
      owner => hbase,
      group => hbase,
      mode => "0644",
      require => [Package["hbase"]],
    }
  }

  class client {
    include hadoop_hbase::common_config
  }

  class server(
    $use_adot_java_agent = false
  ) {
    include hadoop_hbase::common_config

    unless !$hadoop_hbase::common_config::on_s3 and hiera('emr::node_type') == "task" {
      package { "hbase-regionserver":
        ensure => present,
      }

      if ($use_adot_java_agent) {
        include emr_cluster_metrics::adot_java_agent
        Bigtop_file::Properties[
          "/etc/emr-cluster-metrics/adot-java-agent/conf/hbase-regionserver-java-agent.properties"] -> Bigtop_file::Env[
          "/etc/hbase/conf/hbase-env.sh"]
      }

      service { "hbase-regionserver":
        provider    => systemd,
        ensure      => running,
        enable      => true,
        require     => [Package["hbase-regionserver"], Class["Hadoop_hbase::Common_config"],
                      Bigtop_file::Site["/etc/hbase/conf/hbase-site.xml"],
                      Bigtop_file::Env["/etc/hbase/conf/hbase-env.sh"],
                      Bigtop_file::Properties["/etc/hbase/conf/log4j2.properties"],
                      Bigtop_file::Properties["/etc/hbase/conf/hadoop-metrics2-hbase.properties"]],
        hasrestart  => true,
        hasstatus   => true,
      }
      Kerberos::Host_keytab <| title == "hbase" |> -> Service["hbase-regionserver"]
      Bigtop_file::Site <| tag == "hadoop-plugin" |> ~> Service["hbase-regionserver"]
    }
  }

  class master(
    $use_adot_java_agent = false
  ) {
    include hadoop_hbase::common_config

    package { "hbase-master":
      ensure => present,
    }

    if ($use_adot_java_agent) {
      include emr_cluster_metrics::adot_java_agent
      Bigtop_file::Properties[
        "/etc/emr-cluster-metrics/adot-java-agent/conf/hbase-master-java-agent.properties"] -> Bigtop_file::Env[
        "/etc/hbase/conf/hbase-env.sh"]
    }

    service { "hbase-master":
      provider  => systemd,
      ensure    => running,
      enable    => true,
      require   => [Package["hbase-master"], Class["Hadoop_hbase::Common_config"],
                    Bigtop_file::Site["/etc/hbase/conf/hbase-site.xml"],
                    Bigtop_file::Env["/etc/hbase/conf/hbase-env.sh"],
                    Bigtop_file::Properties["/etc/hbase/conf/log4j2.properties"],
                    Bigtop_file::Properties["/etc/hbase/conf/hadoop-metrics2-hbase.properties"]],
      hasrestart => true,
      hasstatus  => true,
    } 
    Kerberos::Host_keytab <| title == "hbase" |> -> Service["hbase-master"]
    Bigtop_file::Site <| tag == "hadoop-plugin" |> ~> Service["hbase-master"]
  }

  class thrift_server(
    $use_adot_java_agent = false
  ) {
    include hadoop_hbase::common_config

    package { "hbase-thrift":
      ensure => present,
    }

    if ($use_adot_java_agent) {
      include emr_cluster_metrics::adot_java_agent
      Bigtop_file::Properties[
        "/etc/emr-cluster-metrics/adot-java-agent/conf/hbase-thrift-java-agent.properties"] -> Bigtop_file::Env[
        "/etc/hbase/conf/hbase-env.sh"]
    }

    service { "hbase-thrift":
        provider    => systemd,
        ensure      => running,
        enable      => true,
        require     => [Package["hbase-thrift"],
                      Bigtop_file::Site["/etc/hbase/conf/hbase-site.xml"],
                      Bigtop_file::Env["/etc/hbase/conf/hbase-env.sh"],
                      Bigtop_file::Properties["/etc/hbase/conf/log4j2.properties"],
                      Bigtop_file::Properties["/etc/hbase/conf/hadoop-metrics2-hbase.properties"]],
        hasrestart  => true,
        hasstatus   => true,
    }
    Kerberos::Host_keytab <| title == "hbase" |> -> Service["hbase-thrift"]
    Bigtop_file::Site <| tag == "hadoop-plugin" |> ~> Service["hbase-thrift"]
  }

  class rest_server(
    $use_adot_java_agent = false
  ) {
    include hadoop_hbase::common_config

    package { "hbase-rest":
      ensure => present,
    }

    if ($use_adot_java_agent) {
      include emr_cluster_metrics::adot_java_agent
      Bigtop_file::Properties[
        "/etc/emr-cluster-metrics/adot-java-agent/conf/hbase-rest-java-agent.properties"] -> Bigtop_file::Env[
        "/etc/hbase/conf/hbase-env.sh"]
    }

    service { "hbase-rest":
        provider    => systemd,
        ensure      => running,
        enable      => true,
        require     => [Package["hbase-rest"],
                      Bigtop_file::Site["/etc/hbase/conf/hbase-site.xml"],
                      Bigtop_file::Env["/etc/hbase/conf/hbase-env.sh"],
                      Bigtop_file::Properties["/etc/hbase/conf/log4j2.properties"],
                      Bigtop_file::Properties["/etc/hbase/conf/hadoop-metrics2-hbase.properties"]],
        hasrestart  => true,
        hasstatus   => true,
    }
    Kerberos::Host_keytab <| title == "hbase" |> -> Service["hbase-rest"]
    Bigtop_file::Site <| tag == "hadoop-plugin" |> ~> Service["hbase-rest"]
  }
}
