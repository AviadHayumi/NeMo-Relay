#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Test-only execution witness. Its journal never feeds Relay."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def record(path, **data):
    data.update(wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns(), pid=os.getpid())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(data) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()
    if not args.operation.replace("-", "").isalnum() or not 0 < args.seconds <= 40:
        parser.error("Operation must be alphanumeric; duration must be in (0, 40].")
    journal = Path(os.environ["CASE_DIR"]) / "oracle.jsonl"
    if args.background:
        command = [
            sys.executable,
            __file__,
            "--operation",
            args.operation,
            "--seconds",
            str(args.seconds),
            "--exit-code",
            str(args.exit_code),
        ]
        with open(os.devnull, "rb") as incoming, open(os.devnull, "wb") as outgoing:
            child = subprocess.Popen(
                command, stdin=incoming, stdout=outgoing, stderr=outgoing, start_new_session=True, close_fds=True
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if journal.exists() and any(
                json.loads(row).get("operation") == args.operation for row in journal.read_text().splitlines()
            ):
                print("Bounded background work started; command returned before that work finishes.")
                return 0
            if child.poll() is not None:
                return 1
            time.sleep(0.01)
        return 1
    cpu_started = time.process_time()
    record(journal, event="start", operation=args.operation)
    until = time.monotonic() + args.seconds
    checksum = 1
    while time.monotonic() < until:
        for value in range(10000):
            checksum = (checksum * 31 + value) % 1000000007
    record(
        journal,
        event="end",
        operation=args.operation,
        cpu_seconds=time.process_time() - cpu_started,
        exit_code=args.exit_code,
    )
    print("Bounded work finished; exit code", args.exit_code)
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
