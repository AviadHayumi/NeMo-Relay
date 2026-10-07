#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Log the stock harness payload, then forward it using Relay's real CLI."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def append(name, value):
    fd = os.open(Path(os.environ["CASE_DIR"]) / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(value) + "\n").encode())
    finally:
        os.close(fd)


body = sys.stdin.buffer.read()
payload = json.loads(body)
append("native-hooks.jsonl", {"wall_ns": time.time_ns(), "payload": payload})
metadata = []
if os.environ.get("TEST_FLEET_SESSION_ID"):
    # This standalone daemon is selected through NEMO_RELAY_GATEWAY_URL.
    # --forward-only instead requires an installed persistent gateway, including
    # its pinned TLS identity. It also ignores the environment URL default.
    metadata = [
        "--session-metadata",
        json.dumps(
            {
                "fleet_session_id": os.environ["TEST_FLEET_SESSION_ID"],
                "worker_epoch": 1,
                "invocation_id": payload.get("session_id"),
            }
        ),
    ]
try:
    result = subprocess.run(
        ["/relay/bin/nemo-relay", "hook-forward", os.environ["HARNESS_KIND"], "--fail-closed", *metadata],
        input=body,
        capture_output=True,
        timeout=15,
    )
except subprocess.TimeoutExpired:
    append(
        "hook-delivery.jsonl",
        {"wall_ns": time.time_ns(), "event": payload.get("hook_event_name"), "exit_code": None, "timed_out": True},
    )
    sys.stderr.write("Relay hook delivery timed out\n")
    raise SystemExit(2)
append(
    "hook-delivery.jsonl",
    {
        "wall_ns": time.time_ns(),
        "event": payload.get("hook_event_name"),
        "exit_code": result.returncode,
        "stderr": result.stderr.decode(errors="replace"),
    },
)
sys.stdout.buffer.write(result.stdout or b"{}\n")
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
