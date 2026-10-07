// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

use super::*;

#[test]
fn shared_session_child_boundaries_preserve_native_owner() {
    for hook in ["UserPromptSubmit", "Stop", "stop"] {
        let outcome = adapt(
            json!({
                "hook_event_name": hook,
                "session_id": "root-thread",
                "agent_id": "child-thread"
            }),
            &HeaderMap::new(),
        );
        assert_eq!(outcome.response, json!({}));
        let boundary = outcome
            .events
            .iter()
            .find_map(|event| match event {
                NormalizedEvent::PromptSubmitted(event) | NormalizedEvent::TurnEnded(event) => {
                    Some(event)
                }
                _ => None,
            })
            .unwrap();
        assert_eq!(boundary.session_id, "root-thread");
        assert_eq!(boundary.metadata["subagent_id"], "child-thread");
        assert_eq!(boundary.payload["agent_id"], "child-thread");
        if hook == "UserPromptSubmit" {
            assert!(outcome.events.iter().any(|event| matches!(
                event, NormalizedEvent::LlmHint(hint) if hint.subagent_id.as_deref() == Some("child-thread")
            )));
        } else {
            assert!(
                outcome
                    .events
                    .iter()
                    .all(|event| !matches!(event, NormalizedEvent::LlmHint(_)))
            );
        }
    }
}

#[test]
fn root_and_ambiguous_boundaries_do_not_gain_child_ownership() {
    let mut headers = HeaderMap::new();
    headers.insert(
        "x-nemo-relay-session-id",
        "caller-override".parse().unwrap(),
    );
    for ids in [
        json!({"session_id": "root-thread", "agent_id": "root-thread"}),
        json!({"session_id": "root-thread"}),
        json!({"agent_id": "possibly-root"}),
        json!({"session_id": "", "agent_id": "possibly-root"}),
        json!({"session_id": "root-thread", "agent_id": "  "}),
    ] {
        for hook in ["UserPromptSubmit", "Stop"] {
            let mut payload = ids.clone();
            payload["hook_event_name"] = json!(hook);
            let outcome = adapt(payload, &headers);
            for event in &outcome.events {
                if let NormalizedEvent::PromptSubmitted(event) | NormalizedEvent::TurnEnded(event) =
                    event
                {
                    assert_eq!(event.session_id, "caller-override");
                    assert!(event.metadata.get("subagent_id").is_none());
                }
            }
            assert!(
                outcome
                    .events
                    .iter()
                    .any(|event| matches!(event, NormalizedEvent::LlmHint(_)))
            );
        }
    }
}
