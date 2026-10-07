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
| Dashboard, launcher and Kubernetes packaging | [Fleet application](https://github.com/AviadHayumi/idle-example-cc/blob/242723f10f4b8665cd2f89a52abdcf5663110f66/README.md) |
| Our native Rust plugin loaded inside Relay | `examples/idle-fleet/relay_activity_plugin/` |
| Rust helper that owns launched child processes | `examples/idle-fleet/managed_exec/` |
| Simple explanation of plugins and restart recovery | [Recovery walkthrough](https://github.com/AviadHayumi/idle-example-cc/blob/242723f10f4b8665cd2f89a52abdcf5663110f66/docs/relay-plugin-recovery.md) |
| Fixed problems, open gaps and proposed solutions | [Bug inventory](https://github.com/AviadHayumi/idle-example-cc/blob/242723f10f4b8665cd2f89a52abdcf5663110f66/docs/relay-only-bug-inventory.md) |
| Calculator story with rendered sequence diagrams | [Visual walkthrough](https://github.com/AviadHayumi/idle-example-cc/blob/242723f10f4b8665cd2f89a52abdcf5663110f66/docs/calculator-relay-stories/design.md) |
| Older research, alternative architectures and multi-harness work | `examples/idle-fleet/suggestions/` and `examples/idle-fleet/docs/` |

The optional tool-input rewrite bridge is included because it was part of the
investigation. The whole-CLI managed-execution approach does not require that
bridge. The app preserves the smaller lifecycle-only patch and its provenance
for the tested image recipe; do not silently replace one artifact with another.

## What was validated

Relay's latest combined source matches the saved 16-file hash record: 5,384 Rust
tests passed, with one documented flaky retry. The earlier lifecycle-only build
has its own 5,378-test record and different binary hash. Keep those stages separate.

The app's latest recorded checks include 35 plugin tests, 15 sender tests,
15 Linux ownership cases, four actual Relay restart cases, 705 application tests
with four platform skips, and 101 frontend tests. The restart canary used real
commands and a real Relay daemon, but no model or Claude invocation. See the
linked reports for the complete evidence and limits.

The [publication checks](scripts/relay-completion-check/evidence/publication-checks.json)
record staged hygiene/type/link checks and both Rust workspace checks from the
initial private publication. This fork was subsequently made public; the saved
check record describes that earlier stage. Rust
checks ran on Linux with Rust 1.96.1 because the local 1.91.1 compiler rejects
newer syntax already present upstream.

Publishing these repositories does not deploy a new Fleet image to the cluster.
Whole-sandbox coverage, lost native-hook reconciliation, durable response replay,
and real Substrate checkpoint/restore remain open. Relay state currently uses
`emptyDir`, so same-Pod daemon recovery is not Pod replacement recovery.

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
