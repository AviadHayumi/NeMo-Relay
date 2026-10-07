#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Test-only full ATOF receiver plus the unchanged Relay daemon."""

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path("/work/evidence")
root.mkdir(parents=True, exist_ok=True)
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 16 * 1024 * 1024:
            self.send_error(413)
            return
        raw = self.rfile.read(size)
        try:
            decoded = json.loads(raw)
            events = decoded if isinstance(decoded, list) else [decoded]
        except ValueError:
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        with lock:
            with (root / "relay-atof.jsonl").open("a") as stream:
                for event in events:
                    stream.write(json.dumps({"received_wall_ns": time.time_ns(), "event": event}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


server = ThreadingHTTPServer(("127.0.0.1", 4080), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def record_plugin_snapshots():
    """Test witness of Relay publications, never input to the activity plugin."""
    previous = None
    while True:
        try:
            snapshot = json.loads(Path("/relay-state/activity.json").read_text())
            key = (snapshot["producer_epoch"], snapshot["sequence"])
            if key != previous:
                with (root / "relay-activity.jsonl").open("a") as stream:
                    stream.write(json.dumps({"received_wall_ns": time.time_ns(), "snapshot": snapshot}) + "\n")
                previous = key
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.05)


if os.environ.get("TEST_ACTIVITY_PLUGIN") == "1":
    threading.Thread(target=record_plugin_snapshots, daemon=True).start()
while not Path("/relay/bin/nemo-relay").is_file():
    time.sleep(0.2)
if os.environ.get("TEST_ACTIVITY_PLUGIN") == "1":
    shutil.copyfile("/configuration/relay.toml", "/relay-state/plugins.toml")
    os.environ["NEMO_RELAY_PLUGIN_CONFIG_PATH"] = "/relay-state/plugins.toml"
    with (root / "relay.log").open("a") as log:
        subprocess.run(
            [
                "/relay/bin/nemo-relay",
                "--plugin-config-path",
                "/relay-state/plugins.toml",
                "plugins",
                "enable",
                "fleet.relay_activity",
            ],
            stdout=log,
            stderr=log,
            check=True,
        )
with (root / "relay.log").open("a") as log:
    process = subprocess.Popen(
        ["/relay/bin/nemo-relay", "--bind", "127.0.0.1:4040"], stdout=log, stderr=subprocess.STDOUT
    )
    code = process.wait()
raise SystemExit(code)
