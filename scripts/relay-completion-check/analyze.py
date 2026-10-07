#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Compare recorded Relay boundaries with native hooks and the independent witness."""

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def read_rows(path):
    if not path.exists():
        return []
    return [json.loads(row) for row in path.read_text().splitlines() if row.strip()]


def epoch_ns(event):
    # ATOF timestamps have nanoseconds; datetime truncates to microseconds.
    # That is sufficient for the declared 50 ms / 1 s comparison tolerances.
    return int(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp() * 1e9)


def comparison(native_start, native_end, spans):
    ends = [event for event in spans if event.get("scope_category") == "end"]
    starts = [event for event in spans if event.get("scope_category") == "start"]
    result = {
        "native_start_wall_ns": native_start,
        "native_end_wall_ns": native_end,
        "relay_starts": len(starts),
        "relay_ends": len(ends),
    }
    if native_start is None:
        result["boundary_status"] = "inconclusive_missing_native_start"
    elif not starts:
        result["boundary_status"] = "missing_relay_start"
    elif native_end is None:
        result["boundary_status"] = "inconclusive_missing_native_end"
    elif not ends:
        result["boundary_status"] = "missing_relay_end"
    else:
        earliest = min(ends, key=epoch_ns)
        delay = (epoch_ns(earliest) - native_end) / 1e9
        data = earliest.get("data")
        reason = (earliest.get("metadata") or {}).get("status")
        if reason is None and isinstance(data, dict):
            reason = data.get("status")
        result.update(
            relay_first_end_wall_ns=epoch_ns(earliest), end_minus_native_end_seconds=delay, relay_end_reason=reason
        )
        result["boundary_status"] = "early" if delay < -0.05 else "late" if delay > 1 else "aligned"
        result["duplicate_relay_spans"] = len(starts) > 1
    return result


def process_trace(case):
    processes = []
    for trace in sorted(case.glob("process-trace.*")):
        rows = trace.read_text(errors="replace").splitlines()
        end = next((row for row in reversed(rows) if re.search(r"\+\+\+ (?:exited|killed)", row)), None)
        executables = [
            Path(match.group(1)).name
            for row in rows
            if (match := re.search(r'execve\("([^"\n]+)"', row)) and row.endswith("= 0")
        ]
        first = re.match(r"([0-9]+\.[0-9]+)", rows[0]) if rows else None
        ending = re.match(r"([0-9]+\.[0-9]+)", end) if end else None
        # Exec names and exit times suffice for the committed overview. Raw
        # process arguments remain in the reviewed local trace, not this report.
        processes.append(
            {
                "pid": trace.name.rsplit(".", 1)[-1],
                "executables": executables,
                "first_trace_wall_seconds": float(first.group(1)) if first else None,
                "end_wall_seconds": float(ending.group(1)) if ending else None,
                "terminal_event": end.split(" ", 1)[-1] if end else None,
            }
        )
    return {
        "trace_files": len(processes),
        "terminal_events": sum(p["terminal_event"] is not None for p in processes),
        "executed_processes": [p for p in processes if p["executables"]],
    }


def analyze(harness, folder):
    rows = read_rows(folder / "relay-atof.jsonl")
    events = [row["event"] for row in rows]
    tool_spans, child_spans = defaultdict(list), defaultdict(list)
    for event in events:
        if event.get("kind") != "scope":
            continue
        if event.get("category") == "tool":
            tool_id = (event.get("category_profile") or {}).get("tool_call_id")
            if tool_id:
                session_id = (event.get("metadata") or {}).get("session_id")
                tool_spans[(session_id, tool_id)].append(event)
        if event.get("category") == "agent":
            agent_id = (event.get("metadata") or {}).get("agent_id")
            if agent_id:
                child_spans[agent_id].append(event)
    results = []
    for case in sorted(folder.iterdir()):
        if not case.is_dir() or not (case / "result.json").exists():
            continue
        invocation = json.loads((case / "result.json").read_text())
        hooks = read_rows(case / "native-hooks.jsonl")
        delivery = read_rows(case / "hook-delivery.jsonl")
        oracle = read_rows(case / "oracle.jsonl")
        cli_output = read_rows(case / "stdout.jsonl")
        cli_results = [
            {key: row.get(key) for key in ("subtype", "is_error", "num_turns")}
            for row in cli_output
            if row.get("type") == "result"
        ]
        native_tools, native_children = {}, {}
        for row in hooks:
            payload = row["payload"]
            event_name = payload["hook_event_name"]
            if event_name == "PreToolUse":
                native_tools[payload["tool_use_id"]] = {"start": row, "end": None}
            elif event_name in {"PostToolUse", "PostToolUseFailure"}:
                native_tools.setdefault(payload["tool_use_id"], {"start": None})["end"] = row
            elif event_name == "SubagentStart":
                native_children[payload["agent_id"]] = {"start": row, "end": None}
            elif event_name == "SubagentStop":
                native_children.setdefault(payload["agent_id"], {"start": None})["end"] = row
        tools = []
        for tool_id, pair in native_tools.items():
            payload = (pair["start"] or pair["end"])["payload"]
            entry = comparison(
                (pair["start"] or {}).get("wall_ns"),
                (pair["end"] or {}).get("wall_ns"),
                tool_spans[(payload.get("session_id"), tool_id)],
            )
            entry.update(tool_id=tool_id, tool_name=payload.get("tool_name"), agent_id=payload.get("agent_id"))
            command = payload.get("tool_input", {}).get("command", "")
            # Only store the synthetic test command, never arbitrary model text.
            if command.startswith("python3 /configuration/oracle.py "):
                entry["test_command"] = command
                for witness in oracle:
                    if witness["event"] == "end" and ("--operation " + witness["operation"]) in command:
                        entry["oracle_end_wall_ns"] = witness["wall_ns"]
                        entry["oracle_cpu_seconds"] = witness["cpu_seconds"]
                        if "relay_first_end_wall_ns" in entry:
                            entry["relay_end_minus_work_end_seconds"] = (
                                entry["relay_first_end_wall_ns"] - witness["wall_ns"]
                            ) / 1e9
                        entry["intentionally_detached"] = "--background" in command
            tools.append(entry)
        children = []
        for agent_id, pair in native_children.items():
            entry = comparison(
                (pair["start"] or {}).get("wall_ns"), (pair["end"] or {}).get("wall_ns"), child_spans[agent_id]
            )
            children.append({"agent_id": agent_id, **entry})
        witness_pairs = {}
        for row in oracle:
            witness_pairs.setdefault(row["operation"], {})[row["event"]] = row
        complete_witness = bool(witness_pairs) and all(
            {"start", "end"} <= pair.keys() for pair in witness_pairs.values()
        )
        last_work_end = max((pair["end"]["wall_ns"] for pair in witness_pairs.values() if "end" in pair), default=None)
        hook_failures = sum(row.get("exit_code") != 0 for row in delivery)
        result = {
            "case": case.name,
            "invocation": invocation,
            "native_hook_counts": dict(Counter(row["payload"]["hook_event_name"] for row in hooks)),
            "hook_deliveries": len(delivery),
            "hook_failures": hook_failures,
            "cli_results": cli_results,
            "witness_operations": sorted(witness_pairs),
            "witness_complete": complete_witness,
            "native_subagent_exercised": bool(native_children),
            "tools": tools,
            "subagents": children,
        }
        result["setup_valid"] = (
            invocation["exit_code"] == 0
            and not invocation["timed_out"]
            and bool(hooks)
            and hook_failures == 0
            and len(delivery) == len(hooks)
            and complete_witness
            and (invocation["case"] != "subagent" or bool(native_children))
        )
        if invocation.get("test_only_process_trace"):
            trace = process_trace(case)
            result["process_trace"] = trace
            result["process_trace_complete"] = (
                trace["trace_files"] > 0 and trace["trace_files"] == trace["terminal_events"]
            )
        if invocation["case"] == "real-click":
            result["task_report_present"] = (case / "task-report.md").is_file()
            complete_children = bool(native_children) and all(
                pair.get("start") is not None and pair.get("end") is not None for pair in native_children.values()
            )
            result["native_subagents_complete"] = complete_children
            result["setup_valid"] = (
                invocation["exit_code"] == 0
                and not invocation["timed_out"]
                and bool(hooks)
                and hook_failures == 0
                and len(delivery) == len(hooks)
                and complete_children
                and invocation.get("test_only_process_trace") is True
                and result.get("process_trace_complete") is True
                and bool(trace["executed_processes"])
                and result["task_report_present"]
                and bool(cli_results)
                and cli_results[-1].get("subtype") == "success"
                and cli_results[-1].get("is_error") is False
            )
            result["workload_completion_qualification"] = (
                "Inspect the actual report and process trace; CLI exit alone is not proof of a correct Click fix."
            )
        if invocation["case"] == "failure":
            exact_command = "python3 /configuration/oracle.py --operation failure --seconds 2 --exit-code 17"
            exact_tools = {
                tool_id
                for tool_id, pair in native_tools.items()
                if pair["start"] and pair["start"]["payload"].get("tool_input", {}).get("command") == exact_command
            }
            failed_tools = {
                row["payload"].get("tool_use_id")
                for row in hooks
                if row["payload"]["hook_event_name"] == "PostToolUseFailure"
            }
            for row in cli_output:
                content = row.get("message", {}).get("content", [])
                if isinstance(content, list):
                    failed_tools.update(
                        item.get("tool_use_id")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error") is True
                    )
            codex_nonzero = any(
                row.get("type") == "item.completed"
                and row.get("item", {}).get("type") == "command_execution"
                and row["item"].get("exit_code") == 17
                and exact_command in row["item"].get("command", "")
                for row in cli_output
            )
            failure_exercised = bool(exact_tools) and (bool(exact_tools & failed_tools) or codex_nonzero)
            result["native_failed_tool_exercised"] = failure_exercised
            result["setup_valid"] = result["setup_valid"] and failure_exercised
        if invocation["case"] == "managed-background":
            managed_command = "python3 /configuration/oracle.py --operation managed-background --seconds 20"
            result["native_background_requested"] = (
                any(
                    pair["start"]
                    and pair["start"]["payload"].get("tool_input", {}).get("command") == managed_command
                    and pair["start"]["payload"].get("tool_input", {}).get("run_in_background") is True
                    for pair in native_tools.values()
                )
                if harness == "claude"
                else None
            )
        if last_work_end is not None:
            result["invocation_end_minus_work_end_seconds"] = (invocation["ended_wall_ns"] - last_work_end) / 1e9
        result["scenario_coverage_passed"] = True
        if invocation["case"] == "subagent":
            parent, child = witness_pairs.get("parent", {}), witness_pairs.get("child", {})
            complete_pair = all("start" in pair and "end" in pair for pair in (parent, child))
            overlap = complete_pair and max(parent["start"]["wall_ns"], child["start"]["wall_ns"]) < min(
                parent["end"]["wall_ns"], child["end"]["wall_ns"]
            )
            stops_during_child = [
                row["wall_ns"]
                for row in hooks
                if complete_pair
                and row["payload"]["hook_event_name"] == "Stop"
                and child["start"]["wall_ns"] < row["wall_ns"] < child["end"]["wall_ns"]
            ]
            stop_during_child = bool(stops_during_child)
            result["concurrency_coverage"] = {
                "parent_child_cpu_intervals_overlap": overlap,
                "parent_stop_during_child_cpu_work": stop_during_child,
                "parent_stop_during_child_wall_ns": stops_during_child,
                "child_cpu_end_wall_ns": child.get("end", {}).get("wall_ns"),
            }
            result["scenario_coverage_passed"] = overlap and (harness != "claude" or stop_during_child)
        result["boundary_check_passed"] = bool(tools) and all(
            item["boundary_status"] == "aligned" and not item.get("duplicate_relay_spans")
            for item in [*tools, *children]
        )
        result["conformance_passed"] = (
            result["setup_valid"] and result["boundary_check_passed"] and result["scenario_coverage_passed"]
        )
        result["whole_sandbox_completion_validated"] = False
        results.append(result)
    return {
        "harness": harness,
        "versions": json.loads((folder / "versions.json").read_text()),
        "raw_atof_event_count": len(events),
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--harness", choices=("claude", "codex", "both"), default="claude")
    args = parser.parse_args()
    report = {
        "scope": "Live stock CLI + Relay; no Fleet observer or suspension test",
        "tolerances": {"early_seconds": 0.05, "late_seconds": 1},
        "interpretation": (
            "A closed tool scope means that tool boundary closed. "
            "It does not by itself establish whole-sandbox completion."
        ),
        "harnesses": [
            analyze(kind, args.evidence / kind)
            for kind in (("claude", "codex") if args.harness == "both" else [args.harness])
        ],
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
