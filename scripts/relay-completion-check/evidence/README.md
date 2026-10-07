<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Live experiment log — 7 October 2026

Scope: real Claude Code and Codex CLIs in isolated Pods on `relay-play`, namespace
`cc-fleet`. Real Sonnet requests passed through Relay to the configured NVIDIA
inference endpoint. The Fleet controller, Fleet model proxy, and Fleet process
observer were not involved.

Raw local evidence is under `/private/tmp/relay-completion-20261007-raw`.
The committed JSON contains selected timing, IDs, counts, and synthetic fixture
commands only. Full prompts, model content, CLI config/home directories, and
provider credentials are not included in the committed evidence.

## Published Relay 0.9.4 baseline

Stock images contain Claude Code 2.1.280 and Codex 0.149.1. The pinned image
digests and fixture definitions are in the runner. See
[the machine-readable comparison](baseline-0.9.4.json).

| Case | Claude Code | Codex |
|---|---|---|
| Foreground CPU command | Relay end follows native end by about 12 ms | About 12 ms |
| Detached child | Shell scope closes correctly, but CPU work continues about 18.4 seconds after CLI exit | Same limitation |
| Native subagent with overlapping parent command | Relay closes the child tool 3.835 seconds before its CPU work ends | Relay closes the parent tool 8.073 seconds before its CPU work ends |
| Intentional tool failure, first attempt | Inconclusive: model appended `echo`, masking exit 17 | Same problem; analyzer rejects failure-path coverage |
| Strict failure rerun | Actual failure path confirmed; Relay boundary aligned | Actual shell exit 17 confirmed; Relay boundary aligned |
| Native managed background | Task ID and Stop snapshot report an active shell task; witness did not record eventual exit | Command yielded, but no native tool-end hook or witness end was captured |

All recorded hooks in these first attempts were delivered successfully. The
premature boundaries therefore reproduce despite a working event path.

### Why the child cases matter

Claude's first parent `Stop` explicitly included an active child in
`background_tasks`. Relay nevertheless closed the child's tool and subagent
scope with `closed_by_turn_end`. The command witness recorded nearly 12 seconds
of child CPU execution and a later genuine `PostToolUse`/`SubagentStop`.

Codex emitted a child `UserPromptSubmit` with the parent's session ID and the
child's own agent ID. Relay treated it as another parent turn and closed the
parent command and child scope with `superseded_by_next_turn`. The parent CPU
command had only just been launched.

These are lifecycle-correlation problems that can be addressed inside Relay.
They do not demonstrate that a separate process observer is necessary.

The managed-background cases remain inconclusive about whether the CLI cancelled
work during exit. Absence of an oracle end is not proof that the command remained
alive. The later real-task run adds an independent process-lifetime trace to
check that distinction.

### What the detached-child case does and does not prove

The shell command intentionally creates a child and returns. Native tool end
and Relay tool end are correctly aligned. The child then performs CPU work for
20 seconds. Interpreting that completed tool or CLI invocation as “the whole
sandbox finished” would be false.

The oracle exists only to measure that gap. No product detection fix or safe
suspension guarantee is claimed by this case. Native managed-background tasks
are tested separately so we can find information Relay can obtain through
supported harness hooks before considering any additional signal.

## Real Click task, current source and first patch

See [unmodified current-source results](baseline-head-real-click.json) and
[patched task invocations](patched-real-click.json). These use the same
Claude Code 2.1.280 image and real Sonnet requests, with a test-only `strace`
process witness. The first unmodified-source and patched attempts both registered
14 Claude events; the later continuations register all 15 official events,
including `UserPromptExpansion`. That registration difference is recorded;
not every registered event necessarily fires.

The original-source task finished in 162 seconds with 36 aligned tools and a
completed reviewer. Its Click patch and report are preserved with explicit
quality limitations in `click-baseline.*` / `click-baseline-report.md`.

The first patched attempt hit the experiment's time/turn limits and did not
finish review. A continuation finished in 320 seconds, with 46 aligned tools,
a completed reviewer, and 841/841 traced processes terminating. Independent
checks found two new tests fail against the original tag; the fixed formatting
tests passed (19), as did the full suite with the existing pytest warning
filtered (1316 passed, 21 skipped, 1 xfailed). That first completed patch still
changed ordinary prose wrapping. Its artifacts are `click-patched-first.*` and
`click-patched-first-report.md`; they are evidence, not an approved Click fix.
A further native-Claude correction was requested to preserve ordinary prose;
its verified final result is below.

### Experiment-runner issues found while testing

The initial outer command deadline was shorter than the real-task deadline.
Also, terminating the strace wrapper alone did not immediately terminate its
traced CLI. The runner now uses a longer outer deadline and an invocation
process group. The original capped case remains inconclusive; this cleanup
does not prove that an escaped descendant stopped.

A second continuation initially looked for Claude state under the previous
continuation's evidence folder. It exited immediately without doing task work.
The runner now follows the settings path recorded by the preceding invocation,
which preserves the original native session across repeated continuations.
That failed setup is retained, rather than counted as a Relay success.

## Final Relay binary: actual concurrent-child trigger

[The final regression evidence](final-subagent.json) records two native runs.
The first child was synchronous, so the analyzer explicitly rejects it as proof
of the concurrency regression even though its boundaries aligned.

The explicit native-background rerun did exercise the bug's trigger:

- Parent command ran for eight seconds, overlapping a 20-second child command.
- Parent Stop arrived at **13:26:47.612 UTC**, with `background_tasks` reporting
  the native child as running.
- Child CPU work continued until **13:26:58.604 UTC**, nearly 11 seconds later.
- Relay kept the child's tool open until its real completion; the child scope
  ended 97 ms after native `SubagentStop`.
- All three tool boundaries aligned; no hook delivery failed. All captured
  process traces terminated.

The binary SHA-256 is
`db3295a74c7cf73c5fd7e9160d26d5bfc73989d54da7f9f9a79dc66722dcb9e9`.
This proves the measured native-child case, not arbitrary detached work or
safe sandbox suspension.

## Final real-task correction: preserve prose wrapping

The final native-Claude continuation completed in **232.866 seconds**, with
**35 aligned tool boundaries**, one completed reviewer, no failed hook delivery,
and a complete process trace. It changed `wrap_text` to keep the original default
and opt out of hyphen breaking only in `write_usage`.

Independent checks, performed after Claude finished, confirmed:

- Fixed formatting suite: **21 passed**.
- Unmodified tag 8.3.1 with the new tests: **2 failed, 19 passed**.
- Fixed full suite: **1318 passed, 21 skipped, 1 xfailed**, with the existing
  pytest deprecation warning filtered for collection.
- **72** prose outputs matched original 8.3.1 exactly across six widths and
  three formatting methods.

Read the [reviewed task report](click-final-report.md), [actual patch](click-final.patch),
and [independent command results](click-final-red-green.json). The change remains
local; no PR, issue comment, or push to Click was made. Extremely narrow widths
can still trigger character-level wrapping, as the task report explains.

The real-task continuations used first-patched Relay SHA
`25b16fcaa026e836778524528d92be6d4a23d4c177dc9f766507763ea0b7bbed`.
The later final binary includes the additional late-tool-ID cleanup correction;
its live validation is the separate native-background case above. Do not report
that the real-task runs used the later binary.
