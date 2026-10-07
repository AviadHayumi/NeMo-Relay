<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Independent review of the Relay-only lifecycle patch

Reviewed on 2026-10-07 against the complete working-tree source, including the
shared session manager, Claude and Codex adapters, and focused regression tests.
The external reviewer was `aws/anthropic/bedrock-claude-opus-4-8`. Its input was a
diff, and several findings explicitly depended on code outside that diff.

**Result: this review did not establish an additional code defect.** It did
identify useful questions and restate real limits of the experiment. That does
not mean the implementation is bug-free or that Relay can certify a sandbox
safe to suspend.

A model review is a set of hypotheses. We accept a finding after checking its
input assumptions, reachable call path, and observable result. We do not apply
a proposed fix merely because the reviewer calls it P0 or P1.

## Findings checked against the source

| External finding | Adjudication | Why |
| --- | --- | --- |
| P0: an orphaned child tool or LLM no longer blocks idle | Rejected for tracked work | Both idle predicates check the complete tool and LLM collections, independently of the child collection. |
| P1: preservation depends on reason strings | Maintenance suggestion, no demonstrated defect | Current parent-boundary call sites use the expected reasons. Other reasons intentionally force trace cleanup. |
| P1: the preservation path forgets correlation fields | Rejected | All four fields in the full clear are handled by the preservation path. Only mappings to still-active children survive. |
| P1: `StopFailure` with non-object metadata bypasses passive handling | Rejected for the native adapter path | The shared extractor constructs object metadata. It does not copy a raw `payload.metadata` value into the normalized event. |
| P1: one snapshot payload can contain several terminal events | Rejected | One native hook is classified at a time. `Stop` has one turn end; `SubagentStop` has one child end. A task array does not become a batch of end events. |
| P2: a new parent prompt is swallowed as a surviving child's prompt | Not reproduced; proposed fix would break child continuation | Native Codex parent and child identities are distinguished before session dispatch. A real child prompt must still work after its parent turn ends. |
| P2: an unidentified parent model request falls back to the old sole child after Stop | Rejected for the stated call path | Gateway startup opens a new parent turn first unless it has positive child identity or known request affinity. Weak ownership is then restricted to children of that current turn. |
| P3: a child cannot finish after the parent turn has closed | Rejected | Child closure uses its saved scope stack and exact handle. Focused tests exercise this boundary with no parent turn. |

## Evidence behind those decisions

### 1. Tracked orphan work still blocks plugin shutdown

[`Session::is_idle_for` and `blocks_plugin_idle_shutdown`](../../crates/cli/src/sessions/mod.rs)
check all tracked tools and LLMs. The idle sweeper requires `tools.is_empty()`
and `llms.is_empty()`; these checks do not depend on a corresponding subagent
entry. The plugin-liveness check also treats either nonempty collection as
outstanding work.

Adding an extra check for child-owned members of an already-empty collection
would be redundant. It also would not discover an OS process absent from those
collections.

Regression evidence in
[`session_tests.rs`](../../crates/cli/tests/coverage/shared/session_tests.rs):

- `claude_child_stop_retains_missing_tool_end_until_late_post_hook`: child end
  removes the child scope, but the missing tool result still blocks shutdown.
  Its eventual result settles the original tool without creating a new turn.
- `idle_timeout_keeps_claude_subagent_without_completion_hook` and
  `idle_timeout_waits_for_active_claude_subagent_tool_call`: silence does not
  expire the tracked child or its work.

### 2. Boundary reasons and correlation cleanup are intentional

`close_turn` preserves independent Claude/Codex children for
`closed_by_turn_end` and `superseded_by_next_turn`. Explicit session end and
daemon shutdown still finalize traces. A typed boundary reason could reduce
future maintenance mistakes; there is no current misspelled call site to fix.

`clear_correlation_state` handles exactly four fields: pending LLM hints,
pending tool hints, request affinity, and the previous LLM owner. The preserving
variant handles all four too. It clears session-wide guesses and retains only
tool hints and request affinities that name an active child. With no active
children, the result is the same as a full clear.

`next_parent_prompt_preserves_existing_child_and_shutdown_cleans_it` and
`claude_child_request_affinity_survives_parent_stop_without_phantom_turn`
exercise the reason for preserving those particular mappings.

### 3. Passive failure marks cannot receive raw non-object metadata here

[`agent_metadata`](../../crates/cli/src/agents/shared/adapters.rs) creates a new
JSON object from selected canonical fields. The Claude extractor uses that
implementation. A native `metadata: null` or string would not replace it.
`StopFailure` is classified as a mark, not a turn end. The adapter marks it
observation-only because the current native payload may not identify whether a
parent, child, or internal helper failed.

[`claude_completion_tests.rs`](../../crates/cli/tests/coverage/agents/claude_completion_tests.rs)
checks installed hook coverage, passive tagging, no phantom turn after a helper
failure, and no tool-end event for an active parent tool. Those tests do not
claim to resolve the missing upstream ownership information.

### 4. Snapshot order and identity are deliberate

The shared classifier consumes one hook event name. It never expands a
`background_tasks` list into multiple child-end events. Claude `Stop` produces
one `TurnEnded`; `SubagentStop` produces one `SubagentEnded`.

The added snapshot mark precedes that boundary so final model output cannot
replace and hide the native task arrays. `snapshot_scope: parent_session`
describes the registry's scope. An optional `subagent_id` records which child
hook supplied that parent registry snapshot; it does not make the registry
child-scoped. Consumers must respect the explicit scope field.

The Claude tests verify partial fields, parent/child identity, and actual ATOF
event ordering with a saved final model response. Fresh cluster evidence also
shows why ordering matters: a child's own `SubagentStop` snapshot can still
list that child as running. See [Claude evidence notes](claude-notes.md).

### 5. Child prompt ownership must survive a parent turn boundary

The [Codex adapter](../../crates/cli/src/agents/codex/adapter.rs) distinguishes a
child using nonempty native `session_id` and `agent_id` values that differ. A
root prompt whose values are equal does not receive child ownership metadata.
A Relay session-header override does not change this native comparison.

The proposed condition requiring an open parent turn before accepting a child
prompt would reopen a false parent turn when a genuine background child
continues. That would undo the fix. The review's collision scenario supplies
false child identity; it does not show the normal parent path producing it.

[`codex_adapter_tests.rs`](../../crates/cli/tests/coverage/agents/codex_adapter_tests.rs)
checks both shared-session child ownership and root/ambiguous boundaries.
`codex_shared_session_child_prompt_preserves_parent_work` checks dispatch into
the session manager. Incorrect or missing native identity remains a broader
correlation limitation, not proof of this proposed regression.

### 6. Gateway startup runs before weak child-owner inference

Both `start_llm` and the managed `prepare_gateway_call` path call
`ensure_turn_started_for_gateway` before resolving ownership. Without an open
turn, an explicit active child, or known active-child request affinity, that
function opens a parent turn. Sticky/sole-child inference then checks that the
child belongs to the current turn; a retained old child does not qualify.

Unmanaged startup probes use their own ownership path. For a child request
allowed through without a parent turn, explicit ownership or request affinity
is resolved before the weak fallbacks. A payload-derived affinity is still a
correlation heuristic, not a universal proof of identity.

The request-affinity regression covers legitimate child continuation. A
dedicated post-Stop, no-affinity parent gateway case would be a useful extra
test of the inverse path; the review's stated failure is not reachable through
the inspected startup path.

### 7. Ending a child does not require a live parent turn

`close_subagent_scope` retrieves the exact child handle and its saved task-local
scope stack. It pops that child handle without reading `turn_scope`. Missing
tool/LLM results produce a warning and remain tracked independently.

`parent_stop_preserves_child_tool_llm_and_later_child_activity` exercises this
for both Claude and Codex, including later child work and closure after the
parent turn is gone. The Claude adapter regression checks the same native
`Stop` / `SubagentStop` sequence.

## Real limits still open

- Capturing a native background-shell snapshot does not yet make that shell an
  active session-manager handle. This is explicitly demonstrated by
  `stop_background_shell_snapshot_does_not_yet_drive_plugin_liveness`.
- A detached OS process can continue after its launching tool returns. Empty
  trace collections do not prove that every process in the sandbox exited.
- A missing completion hook can leave an obligation unresolved indefinitely.
  Retaining it avoids declaring success from silence, but it is not proof that
  execution is still running. Reconciliation remains necessary.
- An uncorrelated `StopFailure` is preserved as evidence rather than ending a
  possibly unrelated parent turn. Another stop hook can also request continued
  harness execution after Relay receives a stop boundary.
- This patch changes scope and plugin-liveness behavior. It does not add a
  public, authoritative whole-sandbox idle or safe-suspension API.

The external review was useful for asking these questions. Its severity labels
and proposed patches did not establish the answers. Source inspection, focused
tests, and the separately recorded live experiment are the evidence.
