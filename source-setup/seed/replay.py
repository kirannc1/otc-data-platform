"""Replay exactly one captured Kafka JSON value through the in-cluster CLI."""
import json
import subprocess
import sys
from pathlib import Path

event = json.loads(Path(sys.argv[1]).read_text())
if event.get("event_id") != "P-E1" or event.get("order_id") != "P-S1":
    raise SystemExit("Expected captured P-E1 / P-S1")
subprocess.run(
    ["kubectl", "-n", "otc-sources", "exec", "-i", "kafka-0", "--",
     "/opt/kafka/bin/kafka-console-producer.sh", "--bootstrap-server", "kafka:9092",
     "--topic", "parent.orders.v1", "--property", "parse.key=true",
     "--property", "key.separator=|"],
    input="P-S1|" + json.dumps(event, separators=(",", ":")) + "\n",
    text=True, check=True,
)
