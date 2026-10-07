<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Claude Code: completion evidence, 2026-10-07

This investigation targets Relay alone. The independent test journal is a test
oracle, not a new observer shipped alongside Relay. Relay currently exports
scope events and has an internal plugin-liveness check; neither is a public
whole-sandbox completion guarantee.

## Measured before this investigation

The private demo repository retains a real Kubernetes run in
`deploy/kubernetes/four-harness-e2e/claude/` (2026-10-05, Claude Code 2.1.265,
Relay 0.9.4). Its report records 36 successfully delivered hooks, no delivery
failures, three `Stop` events, and one reviewer subagent. The first successful
response explicitly says the background command and reviewer are still running.
The final response reports all 16 tests passed.

The retained Relay event summary records the reviewer's agent scope starting at
Unix time `1791198865.2988577` and ending at `1791198869.1207922`, immediately
before the first parent turn ends at `1791198869.1225328`. This is evidence that
trace closure cannot serve as the background reviewer's actual completion
signal. The summary does not retain the complete native `SubagentStop` payload
or timestamp, so it does not establish the exact early-close duration.

## Current source behavior

At the investigation's starting revision `db4c6c520d66f15acf28b21be179a6d576254a1b`:

1. Claude `Stop` becomes `TurnEnded`.
2. `Session::close_turn` closes active tools, LLM handles, and subagent scopes
   with a synthetic reason before it closes the turn scope.
3. `SessionManager::has_open_sessions` asks whether those active collections or
   a turn/gateway request still exist. It can therefore permit plugin shutdown
   after `Stop` while the native background child has not emitted its finish.
4. A later child stop can be ignored because the synthetic closure already put
   that child in `completed_subagents`.
5. The idle sweeper requires no tools/LLMs/gateway calls but does not check the
   subagent collection before closing an old turn.

These are telemetry and plugin-liveness semantics. Do not describe them as a
measured false sandbox-idle response from a public Relay status API.

## Signals already available from Claude

Checked against Anthropic's current [hook reference](https://code.claude.com/docs/en/hooks)
and [upstream changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md):

| Signal | What Relay can learn | Boundary to preserve |
| --- | --- | --- |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | The lifetime of a tool invocation | Returning a background task handle is not task completion. |
| `Agent` result with `status: async_launched`, `agentId` | A child moved into background execution | Relay already avoids treating this result as terminal. |
| `SubagentStart` / `SubagentStop`, `agent_id` | Child response lifecycle | Another stop hook may request continuation. |
| `Stop.background_tasks` | A snapshot of native tasks, with id/type/status | Not an OS process inventory; missing array does not mean empty. |
| `Stop.session_crons` | Scheduled session wakeups | A future wakeup is not current CPU activity. |
| `StopFailure` | An API error was reported | Current payload lacks reliable parent-versus-helper identity. |

`background_tasks` and `session_crons` were added in Claude Code **2.1.145**.
`StopFailure` was added in **2.1.78**, before Relay's minimum supported Claude
version 2.1.121. The pinned demo version 2.1.265 is new enough for both, but this
investigation must still capture its actual payloads before trusting a field.

Fresh cluster capture on 2026-10-07 verified both fields on Claude
2.1.280 (from the new capture's `versions.json`). At the first `Stop`, `background_tasks` contains the still-running
reviewer. At that reviewer's `SubagentStop`, the snapshot still lists the same
reviewer as `running`; the final parent `Stop` has empty arrays. This ordering
matters: the explicit child completion follows the snapshot and must settle
that child rather than re-open it from the earlier registry state.

`TaskCompleted` is a task-list / teammate task-update hook. It is **not** a
generic background Bash process exit signal.

## Upstream reports that must be tested, not assumed fixed

- [Claude issue 85955](https://github.com/anthropics/claude-code/issues/85955)
  reports that a teammate stays `running` in `background_tasks` even while
  waiting for a message (version 2.1.228). `SubagentStart` and `TeammateIdle`
  offer activity events, but a snapshot alone can overstate activity. This is
  an upstream report, not a fresh local reproduction.
- [Claude issue 91419](https://github.com/anthropics/claude-code/issues/91419)
  reports `StopFailure` from internal helper requests and failed background
  children carrying the parent's `session_id` (version 2.1.258). Mapping every
  such event to parent `TurnEnded` could close a still-working parent. This
  is an upstream report, not a fresh local reproduction.
- `Stop` / `SubagentStop` run before all hook decisions are known. A second
  hook can block the stop and continue the harness. `stop_hook_active` describes
  a previous continuation; it does not prove whether the present stop wins.

## Focused regression probes

`crates/cli/tests/coverage/agents/claude_completion_tests.rs` feeds documented
native payloads through the actual Claude adapter and session manager.

- Normal foreground tool followed by `Stop`: should release active work.
- Background reviewer, parent `Stop`, later `SubagentStop`: must retain the
  child obligation until the child signal arrives.
- `Stop` with a running shell in `background_tasks`: must retain that task
  obligation after ending the parent's response span.

The child test now requires Relay to retain the background child after parent
`Stop` and release it after the child stop. The shell snapshot case is still an
explicit diagnostic of the current trace/plugin-liveness behavior: a captured
snapshot does not yet create an active shell execution handle. This is not a
whole-session completion guarantee. Run the probes with
`cargo test -p nemo-relay-cli --lib claude_completion_tests`.
Initial local compilation did not reach Rust tests because the sandbox could
not resolve `index.crates.io`, and the available compiler was 1.91.1 while the
repository pins 1.96.1.

The initial patch installs `StopFailure` and keeps it as an observation-only
mark. Regression cases check that it cannot close an active parent tool or open
a phantom turn when an internal helper fails at an idle prompt. This restores
missing error evidence; it does not claim to resolve API-error ownership.

The adapter also emits a passive background snapshot mark before a native
parent or child boundary closes. This prevents `last_turn_llm_output` from
replacing and hiding the task arrays in the turn-end output. Tests capture the
actual subscriber-facing mark and verify that it precedes the turn end even
when a model response supplies the final output. The mark retains missing
fields, parent identity, and child identity without declaring completion.

## Smallest Relay-only direction

Keep the existing trace tree separate from native task obligations. A synthetic
span end can close a trace without deleting the unresolved child/task from the
Relay activity ledger. Feed that ledger the existing hooks, task identifiers,
and supported native snapshots. Add expiry only as an `unknown` transition;
elapsed silence cannot certify successful completion. Preserve an explicit
unknown state for uncorrelated events and unsupported task types.

Do not clear all work on `StopFailure`, `TaskCompleted`, a parent `Stop`, or a
synthetic idle-timeout close. First establish which operation the signal ends.

## Implemented lifecycle direction in this working tree

The shared session patch keeps native child scopes and known child-owned tools
across parent `Stop` and a new parent prompt. A native child finish closes that
child; its real tool result closes the matching tool. The idle sweeper does
not expire a still-tracked child just because no request is currently flowing.
Deterministic child request affinity remains usable across parent boundaries.

Missing or reordered child tool results remain explicit obligations. A child
stop followed by a late tool result must settle the original tool without
inventing a parent turn. If no tool result ever arrives, keeping that tool open
is conservative unresolved evidence, not a proven running process. The change
does not infer completion from a timeout or silently treat missing events as
successful execution. Explicit session shutdown still finalizes traces and
must not be read as independent proof that background OS processes exited.

## Additional review finding: a late result with a changed tool ID

The final source review found a second way to create a false parent turn. Relay
already supports matching a tool result by unique name and arguments when its
call ID differs from the pre-tool hook. But `end_tool` checked the exact ID
before that reconciliation and could open a parent turn unnecessarily.

The focused sequence was: child tool starts with ID A, parent stops, child stops,
then the retained tool's result arrives with ID B and the same name/arguments.
The result correctly removed A, but an empty parent turn remained active. This
is a deterministic hook regression, not a newly measured live Click mismatch.

`late_child_tool_result_with_changed_id_does_not_reopen_parent_turn` reproduced
the defect against unchanged source in the Linux cluster builder. All four CI
attempts failed on the false parent turn, after confirming the tool itself was
removed. The fix reconciles an existing handle before deciding whether a new
turn is needed. Unknown or ambiguous post-only results retain their previous
behavior.

The new regression runs the fixed sequence for both Claude and Codex. It and
seven related tests passed: unique/global and explicit-owner matching,
post-only behavior for Claude/Codex/Pi, out-of-order endings, and the original
same-ID late-child case. The red and green logs are retained at
`/private/tmp/relay-completion-late-post-red.log` and
`/private/tmp/relay-completion-late-post-green.log`. The subsequent canonical
rerun passed all 5,378 tests; see [REPORT.md](REPORT.md) and
[source-validation.json](evidence/source-validation.json).
