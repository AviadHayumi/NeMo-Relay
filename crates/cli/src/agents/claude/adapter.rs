// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

use axum::http::HeaderMap;
use serde_json::{Map, Value, json};

use crate::agents::shared::adapters::{
    AdapterOutcome, CLAUDE_CODE_PAYLOAD_EXTRACTOR, ClassificationRules, classify,
    permission_request,
};
use crate::events::{AgentKind, NormalizedEvent, SessionEvent};
use crate::sessions::HookEffects;

/// Normalizes Claude Code hook payloads and returns the hook response Claude expects.
///
/// Claude Code uses permission-bearing tool hooks. Pre-tool events acknowledge guardrail success
/// without granting host permission; the later `PermissionRequest` receives the final decision.
/// Note: Claude's hook output schema rejects `null` for optional string fields like `stopReason`;
/// omit them entirely instead.
pub(crate) fn adapt(payload: Value, headers: &HeaderMap) -> AdapterOutcome {
    let mut events = classify(
        &payload,
        headers,
        &CLAUDE_CODE_PAYLOAD_EXTRACTOR,
        &ClassificationRules {
            kind: AgentKind::ClaudeCode,
            agent_start: &["SessionStart", "sessionStart", "session_start"],
            agent_end: &["SessionEnd", "sessionEnd", "session_end"],
            subagent_start: &["SubagentStart", "subagentStart"],
            subagent_end: &["SubagentStop", "subagentStop", "SubagentEnd"],
            tool_start: &["PreToolUse", "preToolUse"],
            tool_end: &[
                "PostToolUse",
                "postToolUse",
                "PostToolUseFailure",
                "postToolUseFailure",
                "ToolUseFailed",
                "toolUseFailed",
                "PermissionDenied",
                "permissionDenied",
            ],
            // Claude Code reports only the close of a turn (`Stop`); its turns stay
            // lazily opened, and `PreCompact`/`PostCompact` are matched by the
            // shared fallback rather than by an adapter-specific rule.
            turn_start: &[],
            turn_end: &["Stop", "stop"],
            compaction: &[],
        },
    );
    // A StopFailure may describe a background child or internal helper request using the
    // parent's session id. Record the error without claiming the parent's turn ended or
    // opening a new turn for an error received while the parent was waiting for input.
    for event in &mut events {
        if let NormalizedEvent::HookMark(event) = event
            && event.event_name.eq_ignore_ascii_case("StopFailure")
            && let Some(metadata) = event.metadata.as_object_mut()
        {
            metadata.insert("nemo_relay_observation_only".into(), json!(true));
            metadata.insert("completion_signal".into(), json!("unverified"));
        }
    }
    if let Some((index, snapshot)) = background_snapshot(&payload, &events) {
        events.insert(index, NormalizedEvent::HookMark(snapshot));
    }
    let response = json!({ "continue": true });
    AdapterOutcome {
        events,
        response,
        permission: permission_request(
            &payload,
            headers,
            AgentKind::ClaudeCode,
            &CLAUDE_CODE_PAYLOAD_EXTRACTOR,
        ),
    }
}

/// Return a request intercept's complete input through Claude Code's native PreToolUse contract.
/// This edits arguments only: omitting `permissionDecision` preserves Claude's permission checks.
pub(crate) fn response_with_effects(mut response: Value, effects: &HookEffects) -> Value {
    if let Some(transform) = &effects.tool_argument_transform {
        response["hookSpecificOutput"] = json!({
            "hookEventName": "PreToolUse",
            "updatedInput": transform.arguments,
        });
    }
    response
}

// Stop's normal turn output can be replaced by the final assistant response. Preserve native
// task snapshots in a separate mark before trace cleanup so consumers can still inspect them.
// A missing field stays missing, and the snapshot is not a claim of sandbox completion.
fn background_snapshot(
    payload: &Value,
    events: &[NormalizedEvent],
) -> Option<(usize, SessionEvent)> {
    let fields: Map<String, Value> = ["background_tasks", "session_crons"]
        .into_iter()
        .filter_map(|key| {
            payload
                .get(key)
                .map(|value| (key.to_string(), value.clone()))
        })
        .collect();
    if fields.is_empty() {
        return None;
    }
    events.iter().enumerate().find_map(|(index, event)| {
        let (session_id, event_name, metadata, subagent_id) = match event {
            NormalizedEvent::TurnEnded(event) => {
                (&event.session_id, &event.event_name, &event.metadata, None)
            }
            NormalizedEvent::SubagentEnded(event) => (
                &event.session_id,
                &event.event_name,
                &event.metadata,
                Some(&event.subagent_id),
            ),
            _ => return None,
        };
        let mut metadata = metadata.as_object().cloned().unwrap_or_default();
        metadata.insert("session_id".into(), json!(session_id));
        metadata.insert("nemo_relay_observation_only".into(), json!(true));
        metadata.insert(
            "nemo_relay_observation_role".into(),
            json!("background_snapshot"),
        );
        metadata.insert("completion_signal".into(), json!("unverified"));
        metadata.insert("snapshot_scope".into(), json!("parent_session"));
        if let Some(subagent_id) = subagent_id {
            metadata.insert("subagent_id".into(), json!(subagent_id));
        }
        Some((
            index,
            SessionEvent {
                session_id: session_id.clone(),
                agent_kind: AgentKind::ClaudeCode,
                event_name: event_name.clone(),
                payload: Value::Object(fields.clone()),
                metadata: Value::Object(metadata),
            },
        ))
    })
}

#[cfg(test)]
#[path = "../../../tests/coverage/agents/claude_completion_tests.rs"]
mod completion_tests;
