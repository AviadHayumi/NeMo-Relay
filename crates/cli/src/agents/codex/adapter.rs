// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

use axum::http::HeaderMap;
use serde_json::{Value, json};

use crate::agents::shared::adapters::{
    AdapterOutcome, CODEX_PAYLOAD_EXTRACTOR, ClassificationRules, classify, normalize_name,
    permission_request,
};
use crate::events::{AgentKind, NormalizedEvent, ToolEvent};

/// Normalizes Codex hook payloads while leaving Codex hook control flow untouched.
///
/// Codex receives an empty response body from this adapter because the gateway currently records
/// hooks instead of making allow/deny decisions. Event spelling is accepted in both camelCase and
/// snake_case forms so installed hooks and inline `run` hook configuration share one path.
pub(crate) fn adapt(payload: Value, headers: &HeaderMap) -> AdapterOutcome {
    let mut events = classify(
        &payload,
        headers,
        &CODEX_PAYLOAD_EXTRACTOR,
        &ClassificationRules {
            kind: AgentKind::Codex,
            agent_start: &["sessionStart", "session_start", "agentStarted"],
            agent_end: &["sessionEnd", "session_end", "agentEnded"],
            subagent_start: &["subagentStart", "subagent_start"],
            subagent_end: &["subagentStop", "subagentEnd", "subagent_stop"],
            tool_start: &["preToolUse", "toolStarted", "tool_start"],
            tool_end: &["postToolUse", "toolEnded", "tool_end", "toolFailed"],
            // Codex reports only the close of a turn (`Stop`); its turns stay
            // lazily opened, and `PreCompact`/`PostCompact` are matched by the
            // shared fallback rather than by an adapter-specific rule.
            turn_start: &[],
            turn_end: &["Stop", "stop"],
            compaction: &[],
        },
    );
    // Recent Codex hooks share the root session_id across threads. agent_id identifies the
    // thread that emitted a prompt or Stop. Preserve that ownership at turn boundaries so a
    // child's prompt cannot supersede its parent's turn and Stop cannot close sibling work.
    if let Some(child_id) = shared_session_child_id(&payload) {
        events.retain_mut(|event| match event {
            NormalizedEvent::PromptSubmitted(event) | NormalizedEvent::TurnEnded(event) => {
                if let Some(metadata) = event.metadata.as_object_mut() {
                    metadata.insert("subagent_id".into(), json!(child_id));
                }
                true
            }
            // There is no next child request to correlate after Stop. Keeping this hint would
            // lazily open a phantom root turn if a late child Stop arrives after root completion.
            NormalizedEvent::LlmHint(event) => normalize_name(&event.event_name) != "stop",
            _ => true,
        });
    }
    AdapterOutcome {
        events,
        response: json!({}),
        permission: permission_request(
            &payload,
            headers,
            AgentKind::Codex,
            &CODEX_PAYLOAD_EXTRACTOR,
        )
        .map(|request| request.map(without_approval_description)),
    }
}

fn shared_session_child_id(payload: &Value) -> Option<&str> {
    // Compare native identifiers, not a caller's Relay session-header override. A missing or
    // empty identifier is insufficient evidence that this hook belongs to a child thread.
    let session_id = payload.get("session_id")?.as_str()?;
    let agent_id = payload.get("agent_id")?.as_str()?;
    (!session_id.trim().is_empty() && !agent_id.trim().is_empty() && agent_id != session_id)
        .then_some(agent_id)
}

/// Builds Codex's native PermissionRequest denial.
///
/// Codex rejects any other shape as invalid hook output instead of denying the request.
/// `continue` is omitted because Codex does not support it for PermissionRequest.
pub(crate) fn permission_denial(message: String) -> Value {
    json!({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": message,
            }
        }
    })
}

// Codex adds `tool_input.description`, a human-readable approval reason, to PermissionRequest for
// its built-in tools but not to the matching PreToolUse. Drop it so the session gate compares the
// tool's actual arguments. MCP tools forward their real arguments, which may include a
// `description`, so they are left untouched.
fn without_approval_description(mut event: ToolEvent) -> ToolEvent {
    if !event.tool_name.starts_with("mcp__")
        && let Value::Object(arguments) = &mut event.arguments
    {
        arguments.remove("description");
    }
    event
}

#[cfg(test)]
#[path = "../../../tests/coverage/agents/codex_adapter_tests.rs"]
mod tests;
