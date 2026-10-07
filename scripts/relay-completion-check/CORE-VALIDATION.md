<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Core validation: lifecycle fixes and the optional Claude rewrite bridge

Validated on October 7, 2026, on branch `codex/relay-only-lifecycle`, based on
`db4c6c520d66f15acf28b21be179a6d576254a1b`. The existing lifecycle changes and
optional rewrite bridge are still uncommitted. No upstream issue, PR, or push
was created by this validation.

The isolated builder was `cc-fleet/relay-bridge-builder` on `relay-play`.
Checksums of all 16 changed CLI source/test/hook files matched the builder's
source before the original result was accepted, and again after the final fix.

## Two separate reasons for changing Relay

- The lifecycle fixes preserve running children across parent turn completion
  and retain otherwise missing completion evidence. Their live reproduction is
  recorded in [REPORT.md](REPORT.md).
- The optional Claude bridge returns an application's tool argument rewrite
  through `PreToolUse.hookSpecificOutput.updatedInput`. This enables a possible
  Bash-wrapping integration. It is **not a requirement** for the alternative
  whole-CLI subreaper approach currently being tested. Do not describe every
  line in the combined patch as necessary for that approach.

The rewrite bridge preserves normal permission decisions, rejects non-object
rewrites before recording a tool start, and is available through both standalone
and managed-daemon hook ingress. Its tests validate HTTP response and event
behavior; they do not by themselves prove that an actual Claude invocation ran
a supervised shell command.

## A real regression found during review

Imagine an intercept replaces `pytest` with `supervisor -- pytest`.

1. The first `PreToolUse` response correctly contains the rewritten input.
2. If that response is lost and the same active hook is retried, the old code
   returns only `continue: true` because the tool ID already exists.
3. A caller receiving only the second response could execute the original
   command while Relay's tool span records the rewritten command.

The new regression failed before the fix: the first response had `updatedInput`
and the second did not. The fix saves the original input only for rewritten
active tools and replays their already-decided output. It does not invoke the
intercept again. An already-rewritten retry is not wrapped twice; conflicting
input under the same ID is rejected. This is active-call replay protection, not
a durable replay database for hooks arriving after a tool or daemon has ended.

## What passed

| Check | Actual result |
| --- | --- |
| New retry regression before the fix | Failed as expected: second response lost the rewrite |
| Focused Claude transformation tests after the fix | 5 passed; includes retry, conflicting input, guardrails, permission flow, and invalid rewrite behavior |
| Canonical `just test-rust` after the fix | 5,341 workspace tests + 10 native plugin + 13 worker plugin + 20 language-binding plugin = **5,384 passed**, zero skipped |
| `cargo fmt --all -- --check` | Passed |
| `cargo clippy -p nemo-relay-cli --all-targets -- -D warnings` | Passed |
| `cargo build -p nemo-relay-cli` | Passed; default-feature Linux x86_64 CLI |
| `git diff --check` | Passed |

The canonical suite was not a completely clean first attempt: the existing
core test `slow_trace_flush_does_not_block_other_subscribers_or_lifecycle_barriers`
hit its exporter-start timeout once and passed on the configured retry. The
same test had also retried in the preceding run, before this replay fix. This
remaining test flake was recorded rather than hidden or changed outside scope.

An earlier canonical attempt failed 11 architecture/embedded-file checks because
the copied builder source was incomplete. After restoring the required files,
the preceding clean suite passed 5,383 tests. The final totals above include the
new replay regression. Neither setup failure is evidence of correct application
behavior; the later complete-source results are the validation evidence.

## Evidence locations

The source and raw test logs remain in the isolated builder under `/work`:

- `bridge-replay-red.log` and `bridge-replay-green.log`
- `bridge-replay-canonical.log` and `bridge-replay-canonical.exit` (`0`)
- `bridge-replay-fmt.log`, `bridge-replay-clippy.log`, `bridge-replay-build.log`
- `bridge-replay-checks.exit` (`0`)
- `nemo-relay-bridge-replay`, SHA-256:
  `e76787d28f71d0fe8caab1747fe00cb4643aa268ef2b6e01795a3f88b452c8ff`

Local regression/canonical evidence and the 16 source hashes are saved under
`/private/tmp/relay-core-validation-20261007/`. These temporary paths are evidence
for this run, not portable build dependencies.

Python, Node, and Go binding suites were not run for these CLI-only changes.
Repository-wide pre-commit hooks were not run because the changes have not been
staged; run the normal staged checks before the requested commit. Live task
comparison and the application plugin's coverage remain separate verification
work. A passing Rust suite does not certify whole-sandbox idleness or safe
suspension.
