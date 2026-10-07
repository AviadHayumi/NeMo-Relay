#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render a local, self-contained command timeline from collected test evidence."""

import argparse
import html
import json
from pathlib import Path


def render(summary, case_name, raw_case):
    case = next(case for harness in summary["harnesses"] for case in harness["cases"] if case["case"] == case_name)
    invocation = case["invocation"]
    start = invocation["started_wall_ns"]
    duration = (invocation["ended_wall_ns"] - start) / 1e9
    commands = {}
    if raw_case:
        for line in (raw_case / "native-hooks.jsonl").read_text().splitlines():
            payload = json.loads(line)["payload"]
            if payload["hook_event_name"] == "PreToolUse":
                data = payload.get("tool_input", {})
                file_path = data.get("file_path", "")
                description = data.get("description", "")
                if not description and file_path:
                    description = f"{payload.get('tool_name', 'File')} {Path(file_path).name}"
                commands[payload["tool_use_id"]] = (description, data.get("command", "") or file_path)
    rows = []
    operations = sorted(
        [("tool", row) for row in case["tools"]] + [("reviewer", row) for row in case["subagents"]],
        key=lambda pair: pair[1]["native_start_wall_ns"],
    )
    for kind, row in operations:
        began = (row["native_start_wall_ns"] - start) / 1e9
        ended_ns = row.get("native_end_wall_ns")
        elapsed = (ended_ns - row["native_start_wall_ns"]) / 1e9 if ended_ns else None
        status = row["boundary_status"]
        description, command = commands.get(row.get("tool_id"), ("", ""))
        name = row.get("tool_name", "Native subagent")
        title = html.escape(description or name)
        details = f"<pre>{html.escape(command)}</pre>" if command else ""
        timing = f"{elapsed:.3f}s" if elapsed is not None else "No native end recorded"
        delay = row.get("end_minus_native_end_seconds")
        comparison = f"Relay end: {delay:+.3f}s after native end" if delay is not None else status
        left = max(0, min(100, began / max(duration, 0.001) * 100))
        width = max(0.4, min(100 - left, (elapsed or 0) / max(duration, 0.001) * 100))
        color = "#8ddca1" if status == "aligned" else "#ffb175"
        rows.append(
            f"<article><details><summary><b>{title}</b><span>{html.escape(name)} · "
            f"start {began:.1f}s · duration {timing}</span></summary>{details}</details>"
            f'<div class="track"><div style="left:{left:.3f}%;width:{width:.3f}%;'
            f'background:{color}"></div></div><small>{html.escape(comparison)} · '
            f"{html.escape(kind)}</small></article>"
        )
    counts = case["native_hook_counts"]
    verdict = {
        True: "Measured completion check passed",
        False: "Incomplete evidence or a completion mismatch — this run did not pass",
        None: "Run this evidence through the current analyzer for a completion verdict",
    }[case.get("conformance_passed")]
    body = "\n".join(rows)
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Claude + Relay: measured command timeline</title>
<style>
body{{margin:0;background:#101923;color:#eaf0f5;font:16px/1.5 system-ui,sans-serif}}
main{{max-width:1100px;margin:40px auto;padding:0 24px}} h1{{font-size:32px}}
p,small{{color:#b8c9d7}} .note{{border-left:4px solid #ffcf75;padding:12px 20px;background:#1d2b3a}}
article{{padding:16px 0;border-bottom:1px solid #344453}}summary{{cursor:pointer}}
summary span{{float:right;color:#b8c9d7}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;background:#091119}}
.track{{position:relative;background:#233242;height:10px;border-radius:5px;margin:12px 0}}
.track div{{position:absolute;height:10px;border-radius:5px}} small{{font-size:13px}}
@media(max-width:700px){{summary span{{float:none;display:block}}}}
</style><main>
<p>REAL KUBERNETES EXPERIMENT · {html.escape(case_name)}</p>
<h1>What Claude ran, and when Relay saw it finish</h1>
<p><b>{verdict}</b>. This checks execution timing, not whether the coding patch is correct.</p>
<p>{duration:.1f} seconds · {counts.get("PreToolUse", 0)} tools ·
{counts.get("SubagentStart", 0)} native subagents · {case["hook_failures"]} hook-delivery failures.</p>
<div class="note">Bars show native tool/subagent lifetimes within this invocation.
Green means Relay's ending matched within the experiment's tolerance (50 ms early / 1 s late).
Gaps between bars are <b>unclassified</b>: they can include model requests or local harness work.
This chart does not label those gaps idle or prove suspension is safe.</div>
<p>Expand a command to inspect it. Commands, when included, come from the local hook log;
review this file before sharing it.</p>
{body}
</main></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--raw-case", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(json.loads(args.summary.read_text()), args.case, args.raw_case))
    args.output.chmod(0o600)


if __name__ == "__main__":
    main()
