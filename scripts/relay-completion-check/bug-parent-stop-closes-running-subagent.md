<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# [Bug]: Claude parent Stop closes Relay scopes for a subagent that is still running

Local issue draft. Recorded on 7 October 2026. Not posted to GitHub.

Affected areas: framework integrations; observability or exporters.

This report explains the parent Stop bug first. The
[additional fixed findings](#other-bugs-and-gaps-we-fixed-locally) are recorded
at the end, with their evidence and remaining limits.

## the problem

Claude starts a background subagent. The subagent runs a command. While that
command is still running, the parent finishes its response and sends `Stop`.

Relay handles that parent `Stop` by closing the active child tool and subagent
scopes too. In our Kubernetes reproduction, Relay ended the child tool scope
**3.835 seconds before its command finished**.

The parent finished its response. The child had not finished its work.

## what we expected

The parent turn can end while the child keeps running. Relay should keep the
child tool scope open until its own completion event, and keep the subagent
scope open until its own completion event. A parent `Stop` alone should not
finish either one.

Explicit session termination, cancellation, or daemon shutdown needs its own
cleanup policy. Those are different events from a normal parent `Stop`.

## what we actually saw

These are measured times from one run, all UTC on 7 October 2026:

| Time | What happened |
| --- | --- |
| 12:35:45.310931 | Parent sends `Stop` while the child command is still running. |
| 12:35:45.322822 | Relay ends the child tool scope with `closed_by_turn_end`. |
| 12:35:45.322861 | Relay ends the subagent scope with the same reason. |
| 12:35:49.158120 | The child command actually finishes its CPU work. |
| 12:35:49.208241 | Claude sends the child `PostToolUse` completion hook. |
| 12:35:50.716254 | Claude sends `SubagentStop`. |

All **14 hook deliveries succeeded**. The parent `Stop` payload also reported
the child as running in `background_tasks`. This was reproduced with a working
hook delivery path.

The child command performed about 11.97 seconds of CPU work. The independent
command journal measured that work; it did not send observations to Relay.

Evidence: [baseline results](evidence/baseline-0.9.4.json), under
`harnesses[harness=claude].cases[case=baseline-subagent]`.

## how to reproduce

Use Claude Code with the supported Relay hook integration and record the native
hooks alongside Relay lifecycle events. The test fixture below uses the
attached [oracle.py](oracle.py), which runs bounded CPU work and records its
start and end. Mount it at `/configuration/oracle.py`; set `CASE_DIR` to an
existing writable evidence directory inherited by the CLI and its commands.

1. Ask Claude to start a native background subagent using its `Agent` tool.
2. Have the child run this **foreground** command:

   ```bash
   python3 /configuration/oracle.py --operation child --seconds 12
   ```

3. While the child works, have the parent run:

   ```bash
   python3 /configuration/oracle.py --operation parent --seconds 8
   ```

4. Let the parent finish its response while the native background child continues.
5. Compare the parent `Stop`, the child command journal, the child completion
   hooks, and the Relay child tool/subagent end events.

**The trigger must actually occur:** a parent `Stop` must arrive before the
child command finishes. A synchronous child followed by the parent command does
not exercise this bug. Model scheduling can change between runs.

<details>
<summary>Run the existing Kubernetes fixture</summary>

The [runner instructions](README.md#run-in-the-test-cluster) describe the
required cluster, provider Secret, pinned images, and cleanup. Configure those
for the test environment first. From the repository root:

```bash
python3 scripts/relay-completion-check/run.py deploy --harness claude
python3 scripts/relay-completion-check/run.py run --harness claude --cases subagent --label repro
python3 scripts/relay-completion-check/run.py collect --harness claude
python3 scripts/relay-completion-check/analyze.py /private/tmp/relay-completion-20261007
python3 scripts/relay-completion-check/run.py cleanup --harness claude
```

This uses published Relay 0.9.4 unless a different binary is supplied. Inspect
the analyzer output: `parent_child_cpu_intervals_overlap` and
`parent_stop_during_child_cpu_work` must both be true. Otherwise the run has
not exercised this concurrency trigger.

The [original prompt](run_case.py) is under `PROMPTS["subagent"]`.
The fixture uses real model requests and the native Agent and Bash tools.
It does not modify Claude Code.

</details>

## what we do today in Relay

Source links use published commit
`db4c6c520d66f15acf28b21be179a6d576254a1b`, our unmodified starting revision.
This identifies the source path; the timed failing run above used release
0.9.4. The local patch is not published.

1. The Claude adapter classifies `Stop` as a turn end:
   [Claude adapter, lines 41-45](https://github.com/NVIDIA/NeMo-Relay/blob/db4c6c520d66f15acf28b21be179a6d576254a1b/crates/cli/src/agents/claude/adapter.rs#L41-L45).
2. For a parent turn end, `end_turn` calls `close_turn` with
   `closed_by_turn_end`:
   [session handling, lines 2145-2155](https://github.com/NVIDIA/NeMo-Relay/blob/db4c6c520d66f15acf28b21be179a6d576254a1b/crates/cli/src/sessions/mod.rs#L2145-L2155).
3. `close_turn` closes all active LLM, tool, and subagent scopes before ending
   the parent turn:
   [close_turn, lines 2184-2194](https://github.com/NVIDIA/NeMo-Relay/blob/db4c6c520d66f15acf28b21be179a6d576254a1b/crates/cli/src/sessions/mod.rs#L2184-L2194).
4. The tool closer drains the active tools and emits synthetic end results.
   The child closer closes every tracked subagent with the same reason:
   [scope cleanup, lines 2284-2319](https://github.com/NVIDIA/NeMo-Relay/blob/db4c6c520d66f15acf28b21be179a6d576254a1b/crates/cli/src/sessions/mod.rs#L2284-L2319).

The missing distinction is ownership: the parent turn ending does not establish
that independently running child work ended.

## impact

The trace says the child tool and subagent ended earlier than they did.
A consumer counting open scopes could therefore incorrectly conclude that the
tracked work has finished. Until corrected, consumers must distinguish these
synthetic parent-turn closures from actual child completion.

We did not observe Relay returning an authoritative whole-sandbox `Idle`
decision, and we did not suspend a sandbox. This report establishes incorrect
lifecycle boundaries, not a suspension failure.

<details>
<summary>Environment and local fix validation</summary>

- Failing live run: published NeMo Relay **0.9.4**; stock Claude Code **2.1.280**.
- Platform: Linux amd64 in Kubernetes.
- Model: `aws/anthropic/bedrock-claude-sonnet-4-6`, with real provider requests.
- Relay received native harness hooks. No Fleet controller or Fleet process
  observer participated in the reproduction.
- The local fix is in the Relay Rust CLI/daemon session tracking. It preserves
  active child scopes across a parent turn end; it does not patch Claude Code.
- A patched live rerun used an eight-second parent command and a **20-second**
  native background child. The parent sent `Stop` at **13:26:47.613 UTC**;
  the child continued until **13:26:58.605 UTC**.
- Relay kept that child tool scope open. It ended about **178 ms after** the
  measured CPU work and **95 ms after** native `PostToolUse`. The subagent scope
  ended about **97 ms after** native `SubagentStop`.
- All three tool boundaries aligned; all 14 hook deliveries succeeded.
- This rerun exercised the same concurrency trigger with a longer child
  workload. An earlier synchronous rerun was rejected as insufficient coverage.

Read [patched live results](evidence/final-subagent.json), case
`final-background-subagent`. The tested binary SHA-256 is
`db3295a74c7cf73c5fd7e9160d26d5bfc73989d54da7f9f9a79dc66722dcb9e9`.

Local regression coverage includes
`parent_stop_preserves_child_tool_llm_and_later_child_activity` in
[session_tests.rs](../../crates/cli/tests/coverage/shared/session_tests.rs).
The recorded Rust validation passed **5,378 tests**, plus CLI Clippy and
formatting checks: [validation record](evidence/source-validation.json).

These results validate the measured child-lifecycle case. Detached processes,
missing hooks, and safe sandbox suspension remain separate questions.

</details>

## other bugs and gaps we fixed locally

Alongside the parent Stop bug above, we addressed six additional findings.
Some are related paths through the same lifecycle problem; these are not seven
independent failures reproduced in live sessions.

**Status:** the fixes are in the local Relay CLI/daemon working tree. They are
not committed, published upstream, or deployed to the existing Fleet workloads.
They were validated with focused tests and isolated cluster experiments.

| Finding | Before | Local fix | Evidence |
| --- | --- | --- | --- |
| Codex child prompt closed parent work | A child prompt was treated as a new parent turn. The parent command scope ended about 8.07 seconds before its CPU work ended. | Use the native child identity to route child prompt and stop events without ending parent work. | Live failing Kubernetes run; focused adapter and session regressions for the fix. |
| Quiet-time cleanup closed children too early | A child with no recent events could be cleaned up without its completion event. | Keep a known child outstanding until its own completion or explicit teardown. Silence alone does not finish it. | Source inspection and focused idle-timeout regressions. |
| Late child activity invented parent work | A child completion or follow-up model request could open an empty parent turn. Finished parent work then looked outstanding. | Preserve child ownership across the parent boundary and settle child activity without inventing a parent turn. | Focused late-event and request-ownership regressions. |
| Changed tool IDs caused another false parent turn | A late child result arrived with ID B for a tool started with ID A. Matching by name and arguments found the tool, but an empty parent turn had already been opened. | Match the existing tool before deciding whether a parent turn is needed. | The regression failed before the fix and passed afterward for Claude and Codex. |
| Background-task snapshots could disappear | The final model answer replaced turn output containing the native task snapshot. | Emit the snapshot as a separate observation before the turn closes, preserving the fields that were actually present. | Subscriber-event regression with a final model output. |
| Installed hooks omitted API-error evidence | Relay did not install the Claude `StopFailure` hook. | Install and record it as unverified error evidence. It must neither close active parent work nor start a new turn by itself. | Hook-registration and active/idle parent regressions. |

<details>
<summary>Where to inspect each fix and its tests</summary>

The shared behavior is in [sessions/mod.rs](../../crates/cli/src/sessions/mod.rs).
Its regression tests are in
[session_tests.rs](../../crates/cli/tests/coverage/shared/session_tests.rs):

- Codex ownership: `codex_shared_session_child_prompt_preserves_parent_work`
  and `codex_shared_session_child_stop_preserves_parent_and_sibling_work`.
  The [Codex adapter tests](../../crates/cli/tests/coverage/agents/codex_adapter_tests.rs)
  also check native child identity and ambiguous boundaries.
- Quiet children: `idle_timeout_keeps_claude_subagent_without_completion_hook`
  and `idle_timeout_keeps_codex_child_without_completion_hook`.
- Late child activity: `claude_child_stop_retains_missing_tool_end_until_late_post_hook`
  and `claude_child_request_affinity_survives_parent_stop_without_phantom_turn`.
- Changed tool ID: `late_child_tool_result_with_changed_id_does_not_reopen_parent_turn`.

The snapshot and error-hook changes are in the
[Claude adapter](../../crates/cli/src/agents/claude/adapter.rs) and hook registration.
The [Claude completion tests](../../crates/cli/tests/coverage/agents/claude_completion_tests.rs)
include:

- `background_snapshot_is_emitted_before_turn_end_even_with_final_llm_output`.
- `background_snapshot_retains_partial_fields_and_parent_identity`.
- `stop_failure_is_installed_and_marked_as_unverified_observation`.
- `helper_stop_failure_does_not_open_a_phantom_turn`.
- `helper_stop_failure_does_not_close_an_active_parent_tool`.

The [baseline evidence](evidence/baseline-0.9.4.json) contains the Codex failure
under `harnesses[harness=codex].cases[case=baseline-subagent]`.
The [validation record](evidence/source-validation.json) records the passing
5,378-test Rust run after the final patch. A passing regression test is evidence
for its specific sequence, not proof that every live workload is covered.

</details>

**What these fixes still do not solve:** recording a background-shell snapshot
does not yet track the lifetime of that shell. Detached processes can outlive
their launching tools. Missing or ambiguous completion hooks still need
reconciliation. Concurrent model/local work and stop-hook continuation still
need a complete idle decision policy and validation. The proposed activity
ledger and authoritative whole-sandbox idle decision are not implemented here.

The [full investigation](REPORT.md#remaining-gaps-to-solve-inside-relay-first)
keeps those remaining gaps separate from the fixes above.
