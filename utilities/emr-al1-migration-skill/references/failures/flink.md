# Flink Failures (1.x → 1.18)

### FLINK_YARN_CHANGES

- **Cause**: Per-job deployment mode (`-m yarn-cluster`) deprecated in Flink 1.15, effectively removed in favor of application mode.
- **Detection**: `IllegalArgumentException: Unknown execution target`, `Could not find a valid target`, YARN session start failure, `Deployment mode is not supported`.
- **Fix**:
  - `flink run -m yarn-cluster <jar>` → `flink run-application -t yarn-application <jar>`
  - `flink run -m yarn-cluster -yn 4` → `flink run-application -t yarn-application -Dtaskmanager.numberOfTaskSlots=4`
  - For session mode: `yarn-session.sh` launch unchanged, then `flink run -t yarn-session`

### FLINK_MEMORY_MODEL

- **Cause**: Flink 1.10+ unified memory model replaces legacy heap-based config.
- **Detection**: `IllegalConfigurationException` referencing memory, `TaskManager failed to start`, `OutOfMemoryError` with Flink metaspace.
- **Fix**:

| Legacy (Flink 1.x) | New (Flink 1.10+) |
|-----|--------|
| `taskmanager.heap.mb=4096` | `taskmanager.memory.process.size=4096m` |
| `jobmanager.heap.mb=2048` | `jobmanager.memory.process.size=2048m` |
| (none) | `taskmanager.memory.managed.fraction=0.4` |
| (none) | `taskmanager.memory.network.fraction=0.1` |

### FLINK_STATE_BACKEND

- **Cause**: State backend configuration property renamed in Flink 1.13+.
- **Detection**: `Could not find state backend`, `Unknown state backend`, warnings about deprecated config.
- **Fix**:
  - `state.backend: rocksdb` → `state.backend.type: rocksdb`
  - `state.backend: filesystem` → `state.backend.type: hashmap`
  - Verify RocksDB native library loads on AL2023 (x86_64 and ARM compatible)

### FLINK_CONNECTOR_VERSION

- **Cause**: Flink connectors decoupled from Flink core in 1.15+; must use matching connector version.
- **Detection**: `ClassNotFoundException` for connector classes, `NoSuchMethodError` in Kafka/Kinesis connector code.
- **Fix**:
  - Update Flink-Kafka connector to version matching Flink 1.18 (uses Kafka client 3.4+)
  - Update Kinesis connector: replace legacy `flink-connector-kinesis` with the modern AWS SDK v2-based `flink-connector-aws-kinesis-streams`
  - Download updated connector JARs and place in `/usr/lib/flink/lib/` or submit with `-C` classpath

### FLINK_JAVA_COMPATIBILITY

- **Cause**: Flink 1.18 on EMR 7.x runs Java 17; reflection-heavy serialization may break.
- **Detection**: `InaccessibleObjectException`, `IllegalAccessError` in serialization paths, `java.lang.reflect` errors.
- **Fix**: Add to `flink-conf.yaml`:
  ```
  env.java.opts: "--add-opens java.base/java.util=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED"
  ```
