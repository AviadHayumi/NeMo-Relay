#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Compare recorded plugin publications with independent real-work intervals.

This reports the plugin's scoped claims. It does not relabel tracked_settled or
model_wait as whole-sandbox Idle. Raw prompts, command strings and credentials
are never copied into this report. The process witness is test-only.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def analyze(root):
    snapshots = sorted(rows(root / "relay-activity.jsonl"), key=lambda r: r["received_wall_ns"])
    cases = []
    for case in sorted(root.iterdir()):
        if not case.is_dir() or not (case / "result.json").exists():
            continue
        hooks = rows(case / "native-hooks.jsonl")
        deliveries = rows(case / "hook-delivery.jsonl")
        sessions = {r["payload"]["session_id"] for r in hooks if r["payload"].get("session_id")}
        operations = {}
        for row in rows(case / "oracle.jsonl"):
            operations.setdefault(row["operation"], {})[row["event"]] = row
        scored = []
        for name, pair in sorted(operations.items()):
            end_source = "work_journal"
            if "start" in pair and "end" not in pair:
                trace = case / ("process-trace." + str(pair["start"]["pid"]))
                if trace.exists():
                    matches = re.findall(
                        r"(?m)^([0-9]+\.[0-9]+) \+\+\+ (killed by [A-Z0-9]+|exited with [0-9]+) \+\+\+$",
                        trace.read_text(),
                    )
                    if matches:
                        seconds, terminal = matches[-1]
                        pair["end"] = {"wall_ns": int(float(seconds) * 1e9)}
                        end_source = "process_trace: " + terminal
            if not {"start", "end"} <= pair.keys():
                scored.append({"operation": name, "valid": False, "reason": "witness_incomplete"})
                continue
            begin, end = pair["start"]["wall_ns"], pair["end"]["wall_ns"]
            duration = Counter()
            managed_duration = Counter()
            prior = None
            position = begin
            for row in snapshots:
                when = row["received_wall_ns"]
                if when <= begin:
                    prior = row
                    continue
                stop = min(when, end)
                state = "unobserved"
                owned = "unobserved"
                if prior is not None and prior["snapshot"].get("invocation_id") in sessions:
                    snapshot = prior["snapshot"]
                    state = snapshot["state"]
                    executions = snapshot.get("execution_counts", {})
                    if (
                        snapshot.get("managed_coverage", {}).get("current_invocation_drained")
                        and executions.get("outstanding") == 0
                    ):
                        owned = "drained"
                    elif executions.get("root_exited_awaiting_descendants", 0):
                        owned = "descendants_open"
                    elif executions.get("outstanding", 0):
                        owned = "invocation_open"
                    else:
                        owned = "not_managed"
                interval = (stop - position) / 1e9
                duration[state] += interval
                managed_duration[owned] += interval
                position = stop
                prior = row
                if when >= end:
                    break
            if position < end:
                # An unbounded last sample is not a promise that the producer stayed alive.
                duration["unobserved"] += (end - position) / 1e9
                managed_duration["unobserved"] += (end - position) / 1e9
            scored.append(
                {
                    "operation": name,
                    "valid": True,
                    "end_evidence": end_source,
                    "work_seconds": (end - begin) / 1e9,
                    "cpu_seconds": pair["end"].get("cpu_seconds"),
                    "plugin_state_seconds_during_work": dict(duration),
                    "managed_execution_fact_seconds_during_work": dict(managed_duration),
                    "managed_drained_while_work_alive_seconds": managed_duration.get("drained", 0),
                    "tracked_settled_while_work_alive_seconds": duration.get("tracked_settled", 0),
                }
            )
        relevant = [r["snapshot"] for r in snapshots if r["snapshot"].get("invocation_id") in sessions]
        cases.append(
            {
                "case": case.name,
                "native_hooks": len(hooks),
                "delivery_records": len(deliveries),
                "delivery_failures": sum(r.get("exit_code") != 0 for r in deliveries),
                "missing_delivery_records": max(0, len(hooks) - len(deliveries)),
                "observed_plugin_gaps": sorted({gap for s in relevant for gap in s.get("gaps", [])}),
                "operations": scored,
                "last_tracked_state": relevant[-1]["state"] if relevant else None,
                "whole_sandbox_completion_validated": False,
            }
        )
    return {
        "source": "actual Relay plugin publications and independent CPU-work journal",
        "sampling": "50 ms polling of atomic snapshots; timings use receipt time, not inferred missing events",
        "qualification": "tracked_settled means no outstanding instrumented work; it is not sandbox idle",
        "cases": cases,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness_evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.harness_evidence), indent=2) + "\n")
