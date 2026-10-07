<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Our Relay idle-visibility work

This development repository keeps our NeMo Relay changes and the exact Fleet
application used to investigate agent activity. It is based on NVIDIA's commit
`db4c6c520d66f15acf28b21be179a6d576254a1b`.

This repository is a public fork of `NVIDIA/NeMo-Relay`.
The working branch is `codex/relay-only-lifecycle`. This is experimental work,
not an NVIDIA release and not a claim that safe sandbox suspension is solved.

## Get the complete project

```sh
git clone https://github.com/AviadHayumi/NeMo-Relay.git
cd NeMo-Relay
```

The Relay code, tests and experiment reports are public. The
`examples/idle-fleet` submodule still points to an exact commit in the private
`AviadHayumi/idle-example-cc` repository. Making this Relay fork public does not
publish the Fleet app. To also download the app, your GitHub account needs access
to that repository. Then run:

```sh
git submodule update --init --recursive
```

Both repositories remain independently usable. A submodule records a commit,
not a moving branch, so future app changes cannot silently change this checkout.

## Find the code and explanation

| What you want | Where it is |
| --- | --- |
| Relay lifecycle and optional tool-rewrite fixes | `crates/cli/` and `integrations/coding-agents/claude-code/` |
| Real task runner, regression report and sanitized measurements | [Relay experiments](scripts/relay-completion-check/README.md) |
| Latest matching Relay source hashes and test results | [Core validation](scripts/relay-completion-check/CORE-VALIDATION.md) |
| Dashboard, launcher and Kubernetes packaging | [Fleet application](https://github.com/AviadHayumi/idle-example-cc/blob/4da0c3b88c954da4e503e682d548cf88986764e9/README.md) |
| What is actually deployed, its hashes, and real task results | [7 October cluster rollout](https://github.com/AviadHayumi/idle-example-cc/blob/4da0c3b88c954da4e503e682d548cf88986764e9/deploy/kubernetes/relay-ack-fix-20261007/README.md) |
| Our native Rust plugin loaded inside Relay | `examples/idle-fleet/relay_activity_plugin/` |
| Rust helper that owns launched child processes | `examples/idle-fleet/managed_exec/` |
| Simple explanation of plugins and restart recovery | [Recovery walkthrough](https://github.com/AviadHayumi/idle-example-cc/blob/4da0c3b88c954da4e503e682d548cf88986764e9/docs/relay-plugin-recovery.md) |
| Fixed problems, open gaps and proposed solutions | [Bug inventory](https://github.com/AviadHayumi/idle-example-cc/blob/4da0c3b88c954da4e503e682d548cf88986764e9/docs/relay-only-bug-inventory.md) |
| Calculator story with rendered sequence diagrams | [Visual walkthrough](https://github.com/AviadHayumi/idle-example-cc/blob/4da0c3b88c954da4e503e682d548cf88986764e9/docs/calculator-relay-stories/design.md) |
| Older research, alternative architectures and multi-harness work | `examples/idle-fleet/suggestions/` and `examples/idle-fleet/docs/` |

The optional tool-input rewrite bridge is included because it was part of the
investigation. The whole-CLI managed-execution approach does not require that
bridge. The app preserves the smaller lifecycle-only patch and its provenance
for the tested image recipe; do not silently replace one artifact with another.

## What was validated

Relay's latest combined source matches the saved 16-file hash record: 5,384 Rust
tests passed, with one documented flaky retry. The earlier lifecycle-only build
has its own 5,378-test record and different binary hash. Keep those stages separate.

The app's receipt fix passed 43 plugin tests, four actual Relay restart cases
and 111 frontend tests. The later heartbeat, snapshot-flood and SSE delivery
fixes ran an application suite of 729 tests: 725 passed and four platform cases
were skipped. The unchanged
sender/helper retain their earlier 15 sender tests and 15 Linux ownership cases.
The restart fault tests used real commands and a real Relay daemon, but no model
or Claude invocation. The earlier live Fleet rollout used real Claude model
requests, a reviewer subagent and 24 calculator tests. A detached-child follow-up
stayed Running in all 11 sampled post-Claude-exit observations until the child
ended, then showed scoped readiness. These are bounded checks, not a guarantee
that every activity path is covered.

The receipt and UI follow-up completed nine real Claude invocations across three
canary sessions. The initial calculators passed 73 independently rerun tests.
The final browser check recorded 215 samples with no browser-only evidence
expiration and no flicker in the three pre-existing idle sessions. Completion
briefly remained Unknown until a fresh Relay challenge confirmed the result.
The live report preserves the intermediate delivery failure and its SSE fix.

The [publication checks](scripts/relay-completion-check/evidence/publication-checks.json)
record staged hygiene/type/link checks and both Rust workspace checks from the
initial private publication. This fork was subsequently made public; the saved
check record describes that earlier stage. Rust
checks ran on Linux with Rust 1.96.1 because the local 1.91.1 compiler rejects
newer syntax already present upstream.

The separate 7 October rollout deployed the lifecycle-only Relay artifact,
recovery-aware plugin and managed helper to `relay-play`. The controller uses
the verified source release recorded in the report; new Claude sessions default
to Relay-only with proxy routing and do not install the process observer. Other
harnesses retain their observer profiles. The optional tool-rewrite bridge in
this repository is not included in that deployed artifact.

Whole-sandbox coverage, lost native-hook reconciliation, durable response replay,
and real Substrate checkpoint/restore remain open. Relay state currently uses
`emptyDir`, so same-Pod daemon recovery is not Pod replacement recovery. Fleet
controller upgrades also stop active workers under its current shutdown policy.
The newer report records the delayed receipt fix, separate activity and evidence
indicators, recovered readiness after a no-prompt restart, and the redundant
token-snapshot flood found during live validation. Native event identity remains
an open gap; old corruption history is not erased.

## Upstream and private configuration

NVIDIA remains the upstream source. Existing local remote URLs can be inspected
with `git remote -v`; a fresh clone can add it with:

```sh
git remote add upstream https://github.com/NVIDIA/NeMo-Relay.git
git fetch upstream
```

Secrets, provider credentials, TLS keys, raw private task logs, build caches and
machine-local artifacts are excluded. The Fleet README explains how to supply
configuration on another computer. Repository publication creates no upstream
issue or pull request and does not publish package releases.
