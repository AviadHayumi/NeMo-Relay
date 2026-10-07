<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Relay completion check: Claude Code and Codex

This experiment asks two separate questions:

1. Did Relay close a tool or child-agent scope before that operation ended?
2. Did Relay leave that scope open after the operation ended?

It does **not** equate closed scopes with safe suspension. Relay does not currently
publish a general “the whole sandbox has finished” guarantee. A bounded detached
child tests the difference between a completed shell invocation and completed
sandbox work.

The only product signal under test is Relay's ATOF event stream. The test records
the unmodified CLI's supported hooks and a separate command witness to check that
stream. Neither the witness nor a Fleet process observer sends data to Relay.

## Run in the test cluster

```bash
python3 scripts/relay-completion-check/run.py render > /tmp/relay-check-manifest.json
python3 scripts/relay-completion-check/run.py deploy
python3 scripts/relay-completion-check/run.py run
python3 scripts/relay-completion-check/run.py collect
python3 scripts/relay-completion-check/analyze.py /private/tmp/relay-completion-20261007
```

The default run creates one distinctly labeled Claude Pod and one ConfigMap in
`cc-fleet` on `relay-play`. It references the existing `fleet-secrets` Secret,
without reading or copying its value into a manifest. Existing Fleet agents are
not changed. Override `--context`, `--namespace`, `--run-id`, and the Secret
reference when running elsewhere.

Use `--harness both` explicitly to repeat the earlier two-harness comparison;
the current real-issue work focuses on Claude Code.

The unchanged Docker-published Claude/Codex images contain the stock CLIs. They
are pinned by image digest. An init container installs published Relay 0.9.4 in
a shared volume. The test runner invokes the CLI with supported hook settings;
the hook command calls Relay's authenticated `hook-forward` binary. The Relay
sidecar also hosts a test-only local ATOF receiver that saves full event payloads.

These sandboxed fixtures bypass interactive approvals so their bounded commands
can execute unattended. They receive no Kubernetes service-account token. This
configuration is for the test, not a suggested authorization policy for customers.

## Cases and evidence

| Case | Actual work | What to compare |
|---|---|---|
| foreground | Eight seconds of CPU work | Oracle end, native PostToolUse, and Relay tool end |
| background | Shell returns while a bounded 20-second child continues | Shell completion versus actual child completion; a scope end alone does not prove the sandbox finished |
| failure | Two seconds of CPU work, then exit 17 | Failure hook/response and eventual Relay scope closure |
| subagent | Native child works for 12 seconds while parent works for eight | Parent tool and child scope must not close when the child's prompt arrives |
| managed-background | Harness-owned background shell/process handle | Supported task metadata and real cleanup; a missing witness end is inconclusive |
| real-click | Fix Click issue #3362 locally with independent native review | Real clone, reproduction, test, diff, and process-lifetime evidence |

Each command writes start/end time and consumed CPU seconds into its own journal.
This is test evidence, never a production idle detector. Keep full hook payloads,
delivery results, ATOF events, CLI stdout/stderr, versions, and invocation timing.
Do not use only event counters: counters hide premature closure and duplicate
synthetic spans.

Raw logs default to `/private/tmp/relay-completion-20261007`. They can contain model
outputs and prompts. Review and redact them before committing summaries. A CLI
failure or missing hook is an inconclusive test setup, not a passing completion
check. Subagent assertions require evidence that a native subagent actually ran. The
concurrency regression also requires overlapping parent/child command intervals;
for Claude, a parent Stop must occur during the child's measured CPU work. A
synchronous child followed by a parent command does not pass that coverage check.

To rerun a case, give it a fresh label:

```bash
python3 scripts/relay-completion-check/run.py run --harness claude --cases subagent --label repeat-2
```

To compare current source with the release, use separate Pods and the same
harness images. Supply an already built Linux x86_64 CLI binary:

```bash
python3 scripts/relay-completion-check/run.py deploy \
  --run-id relay-completion-head-20261007 \
  --relay-binary /absolute/path/to/nemo-relay \
  --output /private/tmp/relay-completion-head-20261007
python3 scripts/relay-completion-check/run.py run --run-id relay-completion-head-20261007
python3 scripts/relay-completion-check/run.py collect \
  --run-id relay-completion-head-20261007 \
  --output /private/tmp/relay-completion-head-20261007
```

This option uploads the binary through authenticated Kubernetes exec into the
test's shared volume. The daemon and hook forwarder use that same binary. It
does not serve a download endpoint or rebuild the harness image.

When evidence has been collected, delete only this run's resources:

```bash
python3 scripts/relay-completion-check/run.py cleanup
```

No checkpoint, freeze, resume, Fleet UI, or Substrate behavior is tested here.

## Real issue and process-lifetime witness

For the requested real task, select only Claude and the supplied Relay binary:

```bash
python3 scripts/relay-completion-check/run.py deploy \
  --harness claude --run-id relay-completion-head-20261007 \
  --relay-binary /absolute/path/to/nemo-relay --trace-processes
python3 scripts/relay-completion-check/run.py run \
  --harness claude --run-id relay-completion-head-20261007 \
  --cases real-click --label head
```

The prompt uses Click tag `8.3.1`, asks for a failing regression test before a
focused fix, and delegates independent review using Claude's Agent tool. It asks
for actual tests and a saved report. It contains no artificial delay and never
authorizes a push, PR, or issue comment.

`--trace-processes` extracts Debian's published `strace` and `libunwind8`
packages into a read-only mount. It does not rebuild or install into the harness
image. The harness container tests whether it can trace its own child first.
When permitted, strace records process creation, exec, and exit events, without
network payloads or environment values. This is independent **test evidence**,
not an additional component of the proposed Relay-only product.

## Continue a real task without discarding its history

Save a concrete follow-up prompt in a local text file, then resume the earlier
case. The runner follows the original settings path and native session ID, so
repeated continuations share the same CLI history and installed dependencies.
Each invocation still has its own evidence directory:

```bash
python3 scripts/relay-completion-check/run.py run \
  --run-id relay-completion-patched-20261007 --cases real-click \
  --label followup --resume-case patched-real-click \
  --prompt-file /absolute/path/to/followup.txt
```

The real task is bounded to 360 seconds per invocation, with an outer runner
limit of 420 seconds. On timeout, the runner signals the invocation's process
group. A timeout remains inconclusive: successful cleanup of that group does not
prove that an escaped descendant stopped. A successful case requires native
reviewer completion, a task report, a successful structured CLI result, and
terminal events for every captured process trace. Independently validate the
actual source diff and tests before saying the coding task is correct.

## Read the measured command timeline

The [investigation report](REPORT.md) explains the findings and remaining limits.
To render an expandable local HTML timeline from one collected invocation:

```bash
python3 scripts/relay-completion-check/analyze.py /path/to/collected-evidence \
  --harness claude --output /tmp/relay-summary.json
python3 scripts/relay-completion-check/timeline.py /tmp/relay-summary.json \
  --case YOUR-LABEL-real-click \
  --raw-case /path/to/collected-evidence/claude/YOUR-LABEL-real-click \
  --output /tmp/relay-command-timeline.html
```

Each bar is a native tool or subagent lifetime, with Relay's measured end delay.
The optional `--raw-case` adds recorded command text. The HTML contains no external
scripts or assets; review its command text before sharing. Gaps between operations
are unclassified, not automatically idle.
