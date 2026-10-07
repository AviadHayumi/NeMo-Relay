// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

use axum::http::HeaderMap;
use serde_json::{Value, json};

use super::adapt;
use crate::configuration::GatewayConfig;
use crate::sessions::SessionManager;

fn manager() -> SessionManager {
    SessionManager::new(GatewayConfig {
        bind: "127.0.0.1:0".parse().unwrap(),
        openai_base_url: "http://127.0.0.1".into(),
        openai_auth_header: None,
        anthropic_base_url: "http://127.0.0.1".into(),
        anthropic_auth_header: None,
        metadata: None,
        plugin_config: None,
        max_hook_payload_bytes: crate::configuration::DEFAULT_MAX_HOOK_PAYLOAD_BYTES,
        launched_agent: None,
        max_passthrough_body_bytes: crate::configuration::DEFAULT_MAX_PASSTHROUGH_BODY_BYTES,
    })
}

async fn hook(manager: &SessionManager, name: &str, mut payload: Value) {
    payload["session_id"] = json!("claude-completion-audit");
    payload["hook_event_name"] = json!(name);
    let headers = HeaderMap::new();
    manager
        .apply_events(&headers, adapt(payload, &headers).events)
        .await
        .unwrap();
}

async fn start(manager: &SessionManager) {
    hook(manager, "SessionStart", json!({"source": "startup"})).await;
    hook(
        manager,
        "UserPromptSubmit",
        json!({"prompt": "Run the bounded test."}),
    )
    .await;
}

#[tokio::test]
async fn foreground_tool_and_stop_leave_no_active_work() {
    let manager = manager();
    start(&manager).await;
    let tool = json!({
        "tool_name": "Bash", "tool_use_id": "foreground",
        "tool_input": {"command": "sleep 1"}
    });
    hook(&manager, "PreToolUse", tool.clone()).await;
    assert!(manager.has_open_sessions().await);
    let mut done = tool;
    done["tool_response"] = json!({"stdout": "", "stderr": "", "interrupted": false});
    hook(&manager, "PostToolUse", done).await;
    hook(
        &manager,
        "Stop",
        json!({"background_tasks": [], "session_crons": []}),
    )
    .await;
    assert!(!manager.has_open_sessions().await);
    manager.close_all("test_shutdown").await.unwrap();
}

// This reproduces the measured Claude background-reviewer sequence. It checks Relay's
// existing plugin-liveness decision, not an invented public sandbox-idleness API.
#[tokio::test]
async fn parent_stop_keeps_background_subagent_active_until_its_stop() {
    let manager = manager();
    start(&manager).await;
    hook(
        &manager,
        "SubagentStart",
        json!({"agent_id": "reviewer", "agent_type": "general-purpose"}),
    )
    .await;
    hook(
        &manager,
        "PostToolUse",
        json!({
            "tool_name": "Agent", "tool_use_id": "launch-reviewer",
            "tool_input": {"run_in_background": true},
            "tool_response": {"status": "async_launched", "agentId": "reviewer"}
        }),
    )
    .await;
    assert!(manager.has_open_sessions().await);
    hook(
        &manager,
        "Stop",
        json!({
            "background_tasks": [{"id": "reviewer", "type": "subagent", "status": "running"}],
            "session_crons": []
        }),
    )
    .await;
    let child_pending = manager.has_open_sessions().await;
    hook(
        &manager,
        "SubagentStop",
        json!({"agent_id": "reviewer", "agent_type": "general-purpose"}),
    )
    .await;
    let child_finished = !manager.has_open_sessions().await;
    manager.close_all("test_shutdown").await.unwrap();
    assert!(
        child_pending,
        "parent Stop cannot remove a still-live child from plugin liveness"
    );
    assert!(
        child_finished,
        "the actual child completion must release its obligation"
    );
}

#[tokio::test]
async fn stop_background_shell_snapshot_does_not_yet_drive_plugin_liveness() {
    let manager = manager();
    start(&manager).await;
    hook(&manager, "Stop", json!({
        "background_tasks": [{"id": "shell-1", "type": "shell", "status": "running", "command": "sleep 8"}],
        "session_crons": []
    })).await;
    let task_pending = manager.has_open_sessions().await;
    manager.close_all("test_shutdown").await.unwrap();
    assert!(
        !task_pending,
        "current plugin liveness does not track native shell snapshots; this does not prove the shell has finished"
    );
}

#[test]
fn stop_failure_is_installed_and_marked_as_unverified_observation() {
    use crate::agents::CodingAgent;
    use crate::events::NormalizedEvent;
    use crate::hooks::generated_hooks;

    assert!(
        generated_hooks(CodingAgent::ClaudeCode, "relay-hook")["hooks"]
            .get("StopFailure")
            .is_some()
    );
    let payload = json!({
        "session_id": "claude-completion-audit", "hook_event_name": "StopFailure",
        "error": "rate_limit", "error_details": "429 Too Many Requests"
    });
    let outcome = adapt(payload.clone(), &HeaderMap::new());
    let [NormalizedEvent::HookMark(event)] = outcome.events.as_slice() else {
        panic!("StopFailure must not be treated as parent completion");
    };
    assert_eq!(event.payload, payload);
    assert_eq!(event.metadata["nemo_relay_observation_only"], true);
    assert_eq!(event.metadata["completion_signal"], "unverified");
}

#[tokio::test]
async fn helper_stop_failure_does_not_open_a_phantom_turn() {
    let manager = manager();
    hook(&manager, "SessionStart", json!({"source": "startup"})).await;
    assert!(!manager.has_open_sessions().await);
    hook(&manager, "StopFailure", json!({"error": "rate_limit"})).await;
    let phantom_turn = manager.has_open_sessions().await;
    manager.close_all("test_shutdown").await.unwrap();
    assert!(!phantom_turn, "a helper error is not a new user turn");
}

#[tokio::test]
async fn helper_stop_failure_does_not_close_an_active_parent_tool() {
    use nemo_relay::api::event::ScopeCategory;
    use nemo_relay::api::subscriber::{
        deregister_subscriber, flush_subscribers, register_subscriber,
    };
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    let _guard = crate::test_support::PLUGIN_CONFIG_TEST_LOCK.lock().await;
    let subscriber = "claude-stop-failure-parent-tool";
    let tool_ends = Arc::new(AtomicUsize::new(0));
    let recorded = Arc::clone(&tool_ends);
    register_subscriber(
        subscriber,
        Arc::new(move |event| {
            if event.tool_call_id() == Some("stop-failure-parent-tool")
                && event.scope_category() == Some(ScopeCategory::End)
            {
                recorded.fetch_add(1, Ordering::Relaxed);
            }
        }),
    )
    .unwrap();
    let manager = manager();
    start(&manager).await;
    hook(
        &manager,
        "PreToolUse",
        json!({
            "tool_name": "Bash", "tool_use_id": "stop-failure-parent-tool",
            "tool_input": {"command": "sleep 1"}
        }),
    )
    .await;
    hook(&manager, "StopFailure", json!({"error": "rate_limit"})).await;
    let parent_active = manager.has_open_sessions().await;
    flush_subscribers().unwrap();
    let ended_before_parent_tool_result = tool_ends.load(Ordering::Relaxed);
    manager.close_all("test_shutdown").await.unwrap();
    flush_subscribers().unwrap();
    deregister_subscriber(subscriber).unwrap();
    assert!(
        parent_active,
        "a helper failure cannot settle parent execution"
    );
    assert_eq!(
        ended_before_parent_tool_result, 0,
        "the parent tool must remain open, not merely the enclosing turn"
    );
}

#[test]
fn background_snapshot_retains_partial_fields_and_parent_identity() {
    use crate::events::NormalizedEvent;

    for hook_name in ["Stop", "SubagentStop"] {
        let outcome = adapt(
            json!({
                "session_id": "snapshot-parent", "hook_event_name": hook_name,
                "agent_id": "snapshot-child", "background_tasks": []
            }),
            &HeaderMap::new(),
        );
        let snapshot = outcome
            .events
            .iter()
            .find_map(|event| match event {
                NormalizedEvent::HookMark(event) => Some(event),
                _ => None,
            })
            .expect("present native field produces a snapshot mark");
        assert_eq!(snapshot.payload, json!({"background_tasks": []}));
        assert!(snapshot.payload.get("session_crons").is_none());
        assert_eq!(snapshot.metadata["session_id"], "snapshot-parent");
        assert_eq!(snapshot.metadata["snapshot_scope"], "parent_session");
        assert_eq!(snapshot.metadata["nemo_relay_observation_only"], true);
        if hook_name == "SubagentStop" {
            assert_eq!(snapshot.metadata["subagent_id"], "snapshot-child");
        }
        let snapshot_index = outcome
            .events
            .iter()
            .position(|event| matches!(event, NormalizedEvent::HookMark(_)))
            .unwrap();
        let boundary_index = outcome
            .events
            .iter()
            .position(|event| {
                matches!(
                    event,
                    NormalizedEvent::TurnEnded(_) | NormalizedEvent::SubagentEnded(_)
                )
            })
            .unwrap();
        assert!(
            snapshot_index < boundary_index,
            "a terminal child signal must follow its pre-finish task snapshot"
        );
    }
    let no_snapshot = adapt(
        json!({
            "session_id": "snapshot-parent", "hook_event_name": "Stop"
        }),
        &HeaderMap::new(),
    );
    assert!(
        !no_snapshot
            .events
            .iter()
            .any(|event| matches!(event, NormalizedEvent::HookMark(_)))
    );
    let unavailable = adapt(
        json!({
            "session_id": "snapshot-parent", "hook_event_name": "Stop", "background_tasks": null
        }),
        &HeaderMap::new(),
    );
    let unavailable_snapshot = unavailable
        .events
        .iter()
        .find_map(|event| match event {
            NormalizedEvent::HookMark(event) => Some(event),
            _ => None,
        })
        .unwrap();
    assert_eq!(
        unavailable_snapshot.payload,
        json!({"background_tasks": null}),
        "an unavailable task registry must not become an empty list"
    );
}

#[tokio::test]
async fn background_snapshot_is_emitted_before_turn_end_even_with_final_llm_output() {
    use crate::sessions::LlmGatewayStart;
    use nemo_relay::api::event::ScopeCategory;
    use nemo_relay::api::llm::LlmRequest;
    use nemo_relay::api::subscriber::{
        deregister_subscriber, flush_subscribers, register_subscriber,
    };
    use std::sync::{Arc, Mutex};

    let _guard = crate::test_support::PLUGIN_CONFIG_TEST_LOCK.lock().await;
    let name = "claude-background-snapshot-preserved";
    let events = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&events);
    register_subscriber(
        name,
        Arc::new(move |event| {
            if event
                .metadata()
                .and_then(|m| m.get("session_id"))
                .and_then(Value::as_str)
                != Some("claude-completion-audit")
            {
                return;
            }
            if event
                .metadata()
                .and_then(|m| m.get("nemo_relay_observation_role"))
                .and_then(Value::as_str)
                == Some("background_snapshot")
            {
                recorded
                    .lock()
                    .unwrap()
                    .push(json!({"kind": "snapshot", "data": event.data()}));
            } else if event.name() == "claude-code-turn"
                && event.scope_category() == Some(ScopeCategory::End)
            {
                recorded
                    .lock()
                    .unwrap()
                    .push(json!({"kind": "turn_end", "output": event.output()}));
            }
        }),
    )
    .unwrap();
    let manager = manager();
    start(&manager).await;
    let llm = manager.start_llm(&HeaderMap::new(), LlmGatewayStart {
        session_id: Some("claude-completion-audit".into()),
        provider: "anthropic.messages".into(),
        model_name: Some("claude-test".into()),
        subagent_id: None,
        conversation_id: None,
        generation_id: None,
        request_id: None,
        request: LlmRequest {
            headers: Default::default(),
            content: json!({"model": "claude-test", "messages": [{"role": "user", "content": "test snapshot preservation"}]})
        },
        streaming: false,
        metadata: json!({}),
    }).await.unwrap();
    manager.end_llm(llm, json!({
        "role": "assistant", "content": [{"type": "text", "text": "snapshot-preserved-answer"}],
        "stop_reason": "end_turn"
    }), json!({})).await.unwrap();
    let snapshot = json!({
        "background_tasks": [{"id": "snapshot-preserved-shell", "type": "shell", "status": "running"}],
        "session_crons": []
    });
    hook(&manager, "Stop", snapshot.clone()).await;
    manager.close_all("test_shutdown").await.unwrap();
    flush_subscribers().unwrap();
    deregister_subscriber(name).unwrap();
    let events = events.lock().unwrap();
    let snapshot_index = events
        .iter()
        .position(|event| event["kind"] == "snapshot" && event["data"] == snapshot)
        .expect("ATOF-facing subscriber must receive the native snapshot");
    let turn_end_index = events
        .iter()
        .position(|event| {
            event["kind"] == "turn_end"
                && event["output"]
                    .to_string()
                    .contains("snapshot-preserved-answer")
        })
        .expect("turn must retain the final LLM answer");
    assert!(
        snapshot_index < turn_end_index,
        "native task evidence must precede turn cleanup"
    );
    assert!(
        events[turn_end_index]["output"]
            .get("background_tasks")
            .is_none()
    );
}
