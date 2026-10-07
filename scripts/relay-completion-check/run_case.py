#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Invoke one unchanged CLI with mounted supported hooks; no process observer."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HARNESSES = {
    "claude": "/home/agent/.local/bin/claude",
    "codex": "/usr/local/share/npm-global/bin/codex",
}
COMMON_HOOKS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "PreCompact",
    "PostCompact",
]
PROMPTS = {
    "foreground": (
        "Run this exact foreground command once using your shell tool: python3 "
        "/configuration/oracle.py --operation foreground --seconds 8. Wait for its result, then "
        "answer DONE. Do nothing else."
    ),
    "background": (
        "Run this exact command once using your shell tool: python3 /configuration/oracle.py "
        "--operation background --seconds 20 --background. It deliberately leaves bounded "
        "background work. As soon as the shell tool returns, answer STARTED and end your turn. Do "
        "not wait, poll, read files, kill the child, or run other commands."
    ),
    "failure": (
        "Execute exactly one shell tool call whose command string is exactly: python3 "
        "/configuration/oracle.py --operation failure --seconds 2 --exit-code 17\nDo NOT append "
        "echo, semicolons, pipes, ||, or wrappers. The shell itself MUST return nonzero. Exit code "
        "17 is intentional; the purpose is to exercise the harness failure path. Do not retry or "
        "fix it. After the failed tool returns, report the failure and stop."
    ),
    "managed-background": (
        "Run python3 /configuration/oracle.py --operation managed-background --seconds 20 using "
        "your harness's native background or yielding command capability. Do NOT add --background "
        "to the command, a shell ampersand, or nohup. As soon as the tool returns a background "
        "task/process handle, report STARTED and end the turn without polling or waiting. If the "
        "native capability is unavailable, report UNSUPPORTED and do not emulate it."
    ),
    "real-click": (
        "Fix this issue locally: https://github.com/pallets/click/issues/3362\nRepository: "
        "https://github.com/pallets/click.git\nUse tag 8.3.1 so the affected version is "
        "reproducible.\n1. Clone into a new directory under /workspace and read repository "
        "instructions.\n2. Reproduce HelpFormatter(width=65).write_usage splitting long option names"
        " such as --max-retry-count at a hyphen.\n3. Add a failing regression test.\n4. Make a "
        "focused fix that keeps option names intact while preserving normal help text wrapping.\n5. "
        "Test several widths, hyphenated option names, and ordinary prose.\n6. Use the native Agent "
        "tool to spawn an independent reviewer to inspect the diff and edge cases while you run the"
        " relevant tests.\n7. Address concrete findings and rerun affected tests.\n8. Save a short "
        "report with the reproduction, changed files, and the actual test commands/results.\nAim for"
        " a focused 3–5 minute investigation, but finish as soon as the work is complete; do not "
        "add artificial delays. If you cannot finish, report progress and remaining work honestly. "
        "All changes stay local. Do not push, create a PR, or comment on GitHub. Do not inspect "
        "environment variables or credentials."
    ),
    "subagent": (
        "Use your native delegation tool to start ONE subagent. Its task is: run python3 "
        "/configuration/oracle.py --operation child --seconds 12 in the foreground, wait for it, "
        "then report DONE. After spawning it, immediately run python3 /configuration/oracle.py "
        "--operation parent --seconds 8 in your own foreground shell while the child works. Then "
        "use your native wait tool to wait for the child. Report DONE after both finish. Do not "
        "emulate the subagent with a shell process. Do not read unrelated files or environment "
        "variables."
    ),
}


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["prepare", *PROMPTS])
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--resume-case")
    parser.add_argument("--prompt-stdin", action="store_true")
    args = parser.parse_args()
    kind = os.environ["HARNESS_KIND"]
    binary = HARNESSES[kind]
    evidence = Path("/work/evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    if args.case == "prepare":
        while not Path("/relay/bin/nemo-relay").is_file():
            time.sleep(0.2)
        versions = {
            "harness": kind,
            "cli": subprocess.check_output([binary, "--version"], text=True).strip(),
            "relay": subprocess.check_output(["/relay/bin/nemo-relay", "--version"], text=True).strip(),
            "relay_binary_sha256": hashlib.sha256(Path("/relay/bin/nemo-relay").read_bytes()).hexdigest(),
        }
        write(evidence / "versions.json", versions)
        print(json.dumps(versions), flush=True)
        while True:
            time.sleep(60)
    case = evidence / (args.label + "-" + args.case)
    case.mkdir(exist_ok=False)
    previous = None
    if args.resume_case:
        if kind != "claude" or not re.fullmatch(r"[a-z0-9-]{1,80}", args.resume_case):
            parser.error("Resume requires Claude and an existing simple case label")
        previous = evidence / args.resume_case
    state_root = case
    previous_command = None
    if previous:
        previous_command = json.loads((previous / "invocation.json").read_text())["command"]
        if not isinstance(previous_command, list) or not all(isinstance(arg, str) for arg in previous_command):
            parser.error("Previous invocation must record a list of CLI arguments")
        if "--settings" not in previous_command or previous_command.index("--settings") + 1 >= len(previous_command):
            parser.error("Previous invocation must record the CLI settings path")
        # A continuation's evidence folder differs from its original CLI state.
        # Follow the recorded settings path so a second resume finds the same session.
        settings_path = Path(previous_command[previous_command.index("--settings") + 1])
        state_root = settings_path.parent.parent
        if not state_root.is_relative_to(evidence):
            parser.error("Previous CLI settings must belong to this evidence directory")
    project = state_root / "project"
    project.mkdir(exist_ok=bool(previous))
    config = state_root / "config"
    config.mkdir(exist_ok=bool(previous))
    home = state_root / "home"
    home.mkdir(exist_ok=bool(previous))
    env = {**os.environ, "CASE_DIR": str(case), "HOME": str(home)}
    prompt = PROMPTS[args.case]
    if args.case == "managed-background":
        prompt += (
            " For Claude's Bash tool set run_in_background=true."
            if kind == "claude"
            else " For exec_command set yield_time_ms=1000 so it returns its native session handle."
        )
    if args.case == "real-click":
        prompt += (
            "\nUse /workspace/"
            + args.label
            + "-click as the clone directory and save the report at "
            + str(case / "task-report.md")
            + "."
        )
    if args.prompt_stdin:
        prompt = sys.stdin.read()
        if not prompt.strip():
            parser.error("A replacement prompt must not be empty")
    for name in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        env.pop(name, None)
    events = COMMON_HOOKS + (
        ["SessionEnd", "PostToolUseFailure", "StopFailure", "Notification", "UserPromptExpansion"]
        if kind == "claude"
        else ["SessionEnd", "Interrupt"]
    )
    hook_config = {
        "hooks": {
            name: [{"hooks": [{"type": "command", "command": "python3 /configuration/hook.py", "timeout": 20}]}]
            for name in events
        }
    }
    if kind == "claude":
        session_flag, session_id = "--session-id", str(uuid.uuid4())
        if previous:
            old = previous_command
            if not isinstance(old, list):
                parser.error("Previous invocation must record the CLI command")
            key = "--resume" if "--resume" in old else "--session-id"
            if key not in old or old.index(key) + 1 >= len(old):
                parser.error("Previous invocation must record a native session ID")
            session_flag, session_id = "--resume", old[old.index(key) + 1]
        env.update(
            CLAUDE_CONFIG_DIR=str(config),
            ANTHROPIC_BASE_URL="http://127.0.0.1:4040",
            ANTHROPIC_API_KEY=env["PROVIDER_API_KEY"],
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_AUTOUPDATER="1",
        )
        write(config / "settings.json", hook_config)
        command = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            env["MODEL_ID"],
            "--dangerously-skip-permissions",
            "--settings",
            str(config / "settings.json"),
            session_flag,
            session_id,
            "--max-turns",
            "40" if args.case == "real-click" else "12",
            "--max-budget-usd",
            "3" if args.case == "real-click" else "1",
        ]
        incoming = None
    else:
        env.update(CODEX_HOME=str(config), OPENAI_API_KEY=env["PROVIDER_API_KEY"])
        write(config / "hooks.json", hook_config)
        (config / "config.toml").write_text(
            "model = "
            + json.dumps(env["MODEL_ID"])
            + """
model_provider = "relay-conformance"
model_supports_reasoning_summaries = false
model_reasoning_summary = "none"
[features]
hooks = true
[model_providers.relay-conformance]
name = "Relay conformance"
base_url = "http://127.0.0.1:4040/v1"
wire_api = "responses"
env_key = "PROVIDER_API_KEY"
supports_websockets = false
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = 180000
[agents]
max_threads = 3
max_depth = 1
"""
        )
        command = [
            binary,
            "exec",
            "--json",
            "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-",
        ]
        incoming = (prompt + "\n").encode()
    deadline = time.monotonic() + 120
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/healthz", timeout=2):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError("Relay did not become healthy")
            time.sleep(1)
    started = time.time_ns()
    if env.get("TEST_MANAGED_EXECUTION") == "1":
        if kind != "claude":
            raise RuntimeError("Managed invocation conformance is currently Claude-only")
        snapshot = json.loads(Path("/relay-state/activity.json").read_text())
        if time.time() - snapshot["generated_at"] > 3:
            raise RuntimeError("Relay snapshot is stale before managed launch")
        command = [
            "/relay/bin/fleet-managed-exec",
            "--events",
            str(case / "managed-exec.jsonl"),
            "--execution-id",
            str(uuid.uuid4()),
            "--relay-socket",
            "/relay-state/managed-exec.sock",
            "--session-id",
            env["TEST_FLEET_SESSION_ID"],
            "--worker-epoch",
            "1",
            "--producer-epoch",
            snapshot["producer_epoch"],
            "--invocation-id",
            session_id,
            "--scope",
            "invocation",
            "--",
            *command,
        ]
    trace_enabled = False
    if env.get("TRACE_PROCESSES") == "1":
        tracer = "/trace/usr/bin/strace"
        trace_env = {**env, "LD_LIBRARY_PATH": "/trace/usr/lib/x86_64-linux-gnu"}
        probe = subprocess.run(
            [tracer, "-o", str(case / "trace-probe.log"), "/bin/true"], env=trace_env, capture_output=True
        )
        if probe.returncode == 0:
            # Default strace execve output prints environment length, not values.
            # No network, read/write buffers, or environment-dumping flags are used.
            command = [tracer, "-ff", "-ttt", "-e", "trace=process", "-o", str(case / "process-trace"), "--", *command]
            env = trace_env
            trace_enabled = True
    write(
        case / "invocation.json",
        {
            "harness": kind,
            "case": args.case,
            "started_wall_ns": started,
            "command": command,
            "prompt": prompt,
            "test_only_process_trace": trace_enabled,
            "resume_case": args.resume_case,
        },
    )
    with (case / "stdout.jsonl").open("wb") as stdout, (case / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=project, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True
        )
        timed_out = False
        try:
            process.communicate(input=incoming, timeout=360 if args.case == "real-click" else 240)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    result = {
        "harness": kind,
        "case": args.case,
        "started_wall_ns": started,
        "ended_wall_ns": time.time_ns(),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "test_only_process_trace": trace_enabled,
        "process_group": process.pid,
        "relay_binary_sha256": hashlib.sha256(Path("/relay/bin/nemo-relay").read_bytes()).hexdigest(),
    }
    write(case / "result.json", result)
    # A test-only journal proves completion independently. No signal is sent to Relay.
    time.sleep(25 if args.case in {"background", "managed-background"} else 3)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
