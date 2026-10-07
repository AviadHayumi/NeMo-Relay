#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render, deploy and collect an isolated two-harness Relay completion experiment."""

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGES = {
    "claude": (
        "docker.io/docker/sandbox-templates@sha256:868e3f5ef10579e06903d42e86448851ed59319ab8b9fba6dea2772c449bb2a6"
    ),
    "codex": (
        "docker.io/docker/sandbox-templates@sha256:a68b972a59148c6359ade441387159033f6d196d48aef9dda06d2d4bb26eb41e"
    ),
}


def selected_harnesses(args):
    return list(IMAGES) if args.harness == "both" else [args.harness]


def kubectl(args, action, *, body=None, capture=True, timeout=330):
    return subprocess.run(
        ["kubectl", "--context", args.context, "-n", args.namespace, *action],
        input=body,
        capture_output=capture,
        check=True,
        timeout=timeout,
    )


def manifests(args):
    labels = {"app": "relay-completion-check", "run-id": args.run_id}
    config_name = args.run_id + "-config"
    data = {name: (ROOT / name).read_text() for name in ("oracle.py", "hook.py", "run_case.py", "relay_service.py")}
    data["relay.toml"] = """version = 1
[[components]]
kind = "observability"
enabled = true
[components.config]
version = 4
[components.config.atof]
enabled = true
[[components.config.atof.sinks]]
type = "stream"
name = "completion-check"
url = "http://127.0.0.1:4080/events"
transport = "http_post"
timeout_millis = 3000
field_name_policy = "preserve"
"""
    plugin_dir = getattr(args, "activity_plugin_dir", None)
    fleet_id = str(uuid.uuid5(uuid.NAMESPACE_URL, args.run_id))
    if plugin_dir:
        data["relay.toml"] += (
            '\n[plugins.policy.overrides."fleet.relay_activity"]\nattestation = "integrity_only"\n'
            '[[plugins.dynamic]]\nmanifest = "/relay/plugin/relay-plugin.toml"\n'
            '[plugins.dynamic.config]\noutput_directory = "/relay-state"\n'
            f'session_id = "{fleet_id}"\nworker_epoch = 1\n'
            + ("managed_execution = true\n" if args.managed_exec_binary else "")
        )
    items = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": config_name, "namespace": args.namespace, "labels": labels},
            "data": data,
        }
    ]
    for harness in selected_harnesses(args):
        common_env = [
            {"name": "XDG_CONFIG_HOME", "value": "/relay-state"},
            {"name": "PYTHONUNBUFFERED", "value": "1"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "TRACE_PROCESSES", "value": "1" if args.trace_processes else "0"},
            {"name": "NEMO_RELAY_GATEWAY_URL", "value": "http://127.0.0.1:4040"},
        ]
        if plugin_dir:
            common_env += [
                {"name": "TEST_ACTIVITY_PLUGIN", "value": "1"},
                {"name": "TEST_FLEET_SESSION_ID", "value": fleet_id},
            ]
        if args.managed_exec_binary:
            common_env += [{"name": "TEST_MANAGED_EXECUTION", "value": "1"}]
        mounts = [
            {"name": "work", "mountPath": "/work"},
            {"name": "config", "mountPath": "/configuration", "readOnly": True},
            {"name": "relay", "mountPath": "/relay"},
            {"name": "relay-state", "mountPath": "/relay-state"},
        ]
        agent_mounts = mounts + [{"name": "workspace", "mountPath": "/workspace"}]
        if args.trace_processes:
            agent_mounts.append({"name": "trace", "mountPath": "/trace", "readOnly": True})
        security = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
        containers = [
            {
                "name": "agent",
                "image": IMAGES[harness],
                "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "/configuration/run_case.py", "prepare"],
                "env": common_env
                + [
                    {"name": "HARNESS_KIND", "value": harness},
                    {"name": "MODEL_ID", "value": args.model},
                    {
                        "name": "PROVIDER_API_KEY",
                        "valueFrom": {"secretKeyRef": {"name": args.provider_secret, "key": args.provider_key}},
                    },
                ],
                "volumeMounts": agent_mounts,
                "securityContext": security,
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
            },
            {
                "name": "relay",
                "image": "docker.io/library/python:3.12-slim",
                "command": ["python3", "/configuration/relay_service.py"],
                "env": common_env
                + [
                    {"name": "HOME", "value": "/tmp"},
                    {"name": "NEMO_RELAY_PLUGIN_CONFIG_PATH", "value": "/configuration/relay.toml"},
                    {"name": "NEMO_RELAY_OPENAI_BASE_URL", "value": args.upstream + "/v1"},
                    {"name": "NEMO_RELAY_ANTHROPIC_BASE_URL", "value": args.upstream},
                ],
                "volumeMounts": mounts,
                "securityContext": security,
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
            },
        ]
        init = {
            "name": "install-relay",
            "image": "docker.io/library/python:3.12-slim",
            "command": [
                "sh",
                "-c",
                "pip install --no-cache-dir --prefix=/relay nemo-relay-cli-bin=="
                + args.relay_version
                + " && /relay/bin/nemo-relay --version",
            ],
            "securityContext": {
                "runAsUser": 0,
                "runAsGroup": 0,
                "runAsNonRoot": False,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [{"name": "relay", "mountPath": "/relay"}],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        }
        if args.relay_binary:
            init["name"] = "prepare-relay-volume"
            init["command"] = ["sh", "-c", "mkdir -p /relay/bin"]
            init["securityContext"] = {**security, "runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True}
        inits = [init]
        if args.trace_processes:
            inits.append(
                {
                    "name": "prepare-test-tracer",
                    "image": "docker.io/library/debian:bookworm-slim",
                    "command": [
                        "sh",
                        "-c",
                        "set -eu; apt-get update -qq; cd /trace; apt-get download strace libunwind8; "
                        'for package in *.deb; do dpkg-deb -x "$package" /trace; done; chmod -R a+rX /trace',
                    ],
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "runAsNonRoot": False,
                        "allowPrivilegeEscalation": False,
                    },
                    "volumeMounts": [{"name": "trace", "mountPath": "/trace"}],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                }
            )
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": args.run_id + "-" + harness,
                    "namespace": args.namespace,
                    "labels": {**labels, "harness": harness},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 10,
                    "securityContext": {
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "fsGroup": 1000,
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": inits,
                    "containers": containers,
                    "volumes": [
                        {"name": "config", "configMap": {"name": config_name}},
                        *[{"name": name, "emptyDir": {}} for name in ("work", "workspace", "relay", "relay-state")],
                        *([{"name": "trace", "emptyDir": {}}] if args.trace_processes else []),
                    ],
                },
            }
        )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "deploy", "refresh", "run", "collect", "cleanup"))
    parser.add_argument("--context", default="relay-play")
    parser.add_argument("--namespace", default="cc-fleet")
    parser.add_argument("--run-id", default="relay-completion-20261007")
    parser.add_argument("--relay-version", default="0.9.4")
    parser.add_argument(
        "--relay-binary", type=Path, help="Deploy this locally built Linux amd64 binary in the shared volume"
    )
    parser.add_argument(
        "--activity-plugin-dir", type=Path, help="Packaged native activity plugin; requires a supplied Relay binary"
    )
    parser.add_argument(
        "--managed-exec-binary", type=Path, help="Linux helper for actual Relay-owned invocation lifetime checks"
    )
    parser.add_argument(
        "--trace-processes",
        action="store_true",
        help="Mount Debian's strace package for test-only child process lifetime evidence",
    )
    parser.add_argument("--upstream", default="https://inference-api.nvidia.com")
    parser.add_argument("--model", default="aws/anthropic/bedrock-claude-sonnet-4-6")
    parser.add_argument("--provider-secret", default="fleet-secrets")
    parser.add_argument("--provider-key", default="ANTHROPIC_API_KEY")
    parser.add_argument("--harness", choices=("claude", "codex", "both"), default="claude")
    parser.add_argument("--cases", default="foreground,background,failure,subagent,managed-background")
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--resume-case", help="Continue a saved Claude case using its native session ID")
    parser.add_argument("--prompt-file", type=Path, help="Send this explicit follow-up prompt via stdin")
    parser.add_argument("--output", type=Path, default=Path("/private/tmp/relay-completion-20261007"))
    args = parser.parse_args()
    if args.activity_plugin_dir and not args.relay_binary:
        parser.error("--activity-plugin-dir requires --relay-binary")
    if args.managed_exec_binary and not args.activity_plugin_dir:
        parser.error("--managed-exec-binary requires --activity-plugin-dir")
    for field in ("run_id", "label"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,45}", getattr(args, field)):
            parser.error(field + " must be a short lowercase Kubernetes-style name")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.-]*)", args.relay_version):
        parser.error("Invalid Relay version")
    if args.action == "render":
        print(json.dumps(manifests(args), indent=2))
    elif args.action == "refresh":
        rendered = json.dumps(manifests(args)["items"][0]).encode()
        print(kubectl(args, ["apply", "-f", "-"], body=rendered).stdout.decode(), end="")
    elif args.action == "deploy":
        binary = args.relay_binary.read_bytes() if args.relay_binary else None
        if binary is not None and (binary[:4] != b"\x7fELF" or binary[18:20] != b"\x3e\x00"):
            raise ValueError("The cluster requires a Linux ELF x86_64 Relay binary")
        rendered = json.dumps(manifests(args), indent=2).encode()
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "manifest.json").write_bytes(rendered)
        print(kubectl(args, ["apply", "-f", "-"], body=rendered).stdout.decode(), end="")
        print(
            kubectl(
                args,
                ["wait", "--for=condition=Ready", "pod", "-l", "run-id=" + args.run_id, "--timeout=180s"],
                timeout=200,
            ).stdout.decode(),
            end="",
        )
        if args.activity_plugin_dir:
            for name in ("relay-plugin.toml", "config.schema.json", "libfleet_relay_activity.so"):
                data = (args.activity_plugin_dir / name).read_bytes()
                source = (
                    "import pathlib,sys; d=pathlib.Path('/relay/plugin'); d.mkdir(exist_ok=True); "
                    "(d / " + repr(name) + ").write_bytes(sys.stdin.buffer.read())"
                )
                for harness in selected_harnesses(args):
                    kubectl(
                        args,
                        ["exec", "-i", args.run_id + "-" + harness, "-c", "agent", "--", "python3", "-c", source],
                        body=data,
                    )
        if args.managed_exec_binary:
            data = args.managed_exec_binary.read_bytes()
            if data[:4] != b"\x7fELF" or data[18:20] != b"\x3e\x00":
                raise ValueError("The managed helper must be a Linux x86_64 ELF binary")
            source = (
                "import pathlib,sys; p=pathlib.Path('/relay/bin/fleet-managed-exec'); "
                "p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o755)"
            )
            for harness in selected_harnesses(args):
                kubectl(
                    args,
                    ["exec", "-i", args.run_id + "-" + harness, "-c", "agent", "--", "python3", "-c", source],
                    body=data,
                )
        if args.relay_binary:
            source = (
                "import os,sys; p='/relay/bin/nemo-relay.incoming'; "
                "open(p,'wb').write(sys.stdin.buffer.read()); os.chmod(p,0o755); "
                "os.replace(p,'/relay/bin/nemo-relay')"
            )
            for harness in selected_harnesses(args):
                kubectl(
                    args,
                    ["exec", "-i", args.run_id + "-" + harness, "-c", "agent", "--", "python3", "-c", source],
                    body=binary,
                )
            print("Installed the supplied Linux amd64 Relay binary in the selected test Pods.")
    elif args.action == "run":
        cases = args.cases.split(",")
        if any(
            case not in {"foreground", "background", "failure", "subagent", "managed-background", "real-click"}
            for case in cases
        ):
            parser.error("Unknown case")
        harnesses = selected_harnesses(args)
        if args.resume_case and (harnesses != ["claude"] or len(cases) != 1):
            parser.error("Resume is supported for a single Claude case")
        prompt = args.prompt_file.read_bytes() if args.prompt_file else None

        def run(harness):
            for case in cases:
                command = [
                    "exec",
                    *(["-i"] if prompt else []),
                    args.run_id + "-" + harness,
                    "-c",
                    "agent",
                    "--",
                    "python3",
                    "/configuration/run_case.py",
                    case,
                    "--label",
                    args.label,
                ]
                if args.resume_case:
                    command += ["--resume-case", args.resume_case]
                if prompt:
                    command += ["--prompt-stdin"]
                result = kubectl(args, command, body=prompt, timeout=420 if case == "real-click" else 330)
                print(result.stdout.decode(), end="", flush=True)

        with ThreadPoolExecutor(max_workers=len(harnesses)) as pool:
            list(pool.map(run, harnesses))
    elif args.action == "collect":
        args.output.mkdir(parents=True, exist_ok=True)
        for harness in selected_harnesses(args):
            target = args.output / harness
            target.mkdir(exist_ok=True)
            # Collect only explicitly recorded evidence. Do not copy CLI home,
            # credential/config files, caches, or arbitrary workspace files.
            source = """import pathlib,sys,tarfile
root=pathlib.Path('/work/evidence')
names={'versions.json','relay-atof.jsonl','relay-activity.jsonl','relay.log','native-hooks.jsonl','hook-delivery.jsonl','oracle.jsonl','invocation.json','result.json','stdout.jsonl','stderr.log','trace-probe.log','task-report.md','independent-check.json','independent-check-corrected.json','regression-original.json','task-diff.patch','timeout-cleanup.json','managed-exec.jsonl'}
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as tar:
 for path in sorted(root.rglob('*')):
  selected=path.name in names or path.name.startswith('process-trace.')
  if path.is_file() and not path.is_symlink() and selected and len(path.relative_to(root).parts)<=2:
   tar.add(path,arcname=str(path.relative_to(root)),recursive=False)
"""
            result = kubectl(args, ["exec", args.run_id + "-" + harness, "-c", "agent", "--", "python3", "-c", source])
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                archive.extractall(target, filter="data")
        print("Raw local evidence collected at", args.output)
        print("Raw evidence can include prompts and model outputs. Review and redact before publishing.")
    else:
        print(
            kubectl(
                args,
                [
                    "delete",
                    "pod,configmap",
                    "-l",
                    "app=relay-completion-check,run-id=" + args.run_id,
                    "--wait=true",
                    "--timeout=90s",
                ],
            ).stdout.decode(),
            end="",
        )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.stderr.buffer.write(exc.stderr or b"")
        raise SystemExit(exc.returncode)
