// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

use super::*;

fn claude_transform_hook(event: &str, id: &str, input: Value) -> Request<Body> {
    Request::post("/hooks/claude-code")
        .header("content-type", "application/json")
        .body(Body::from(
            json!({
                "session_id": "claude-transform-session",
                "hook_event_name": event,
                "tool_use_id": id,
                "tool_name": "Bash",
                "tool_input": input,
            })
            .to_string(),
        ))
        .unwrap()
}

async fn transform_response(response: Response) -> Value {
    assert_eq!(response.status(), StatusCode::OK);
    serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes()).unwrap()
}

#[tokio::test]
async fn claude_pre_tool_rewrite_preserves_input_identity_and_permission_flow() {
    let _guard = PLUGIN_CONFIG_TEST_LOCK.lock().await;
    const INTERCEPT: &str = "cli-claude-transform";
    const GUARDRAIL: &str = "cli-claude-transform-policy";
    const SUBSCRIBER: &str = "cli-claude-transform-events";
    let seen = Arc::new(Mutex::new(Vec::new()));
    register_tool_conditional_execution_guardrail(
        GUARDRAIL,
        1,
        Arc::new({
            let seen = seen.clone();
            move |_, args| {
                let seen = seen.clone();
                Box::pin(async move {
                    seen.lock().unwrap().push(args);
                    Ok(None)
                })
            }
        }),
    )
    .unwrap();
    let _policy = ToolGuardrailCleanup(GUARDRAIL);
    register_tool_request_intercept(
        INTERCEPT,
        1,
        false,
        Arc::new(|_, mut args| {
            Box::pin(async move {
                if args["command"] == "printf original" {
                    args["command"] = json!("printf rewritten");
                }
                Ok(args)
            })
        }),
    )
    .unwrap();
    let _intercept = ToolInterceptCleanup(INTERCEPT);
    let recorded = Arc::new(Mutex::new(Vec::new()));
    register_subscriber(
        SUBSCRIBER,
        Arc::new({
            let recorded = recorded.clone();
            move |event| {
                if event.name() == "Bash" {
                    recorded.lock().unwrap().push(event.to_json_value());
                }
            }
        }),
    )
    .unwrap();
    let _subscriber = SubscriberCleanup(SUBSCRIBER);

    let original = json!({"command": "printf original", "timeout": 12000, "description": "Keep this", "run_in_background": false});
    let mut rewritten = original.clone();
    rewritten["command"] = json!("printf rewritten");
    let app = router(test_config());
    let pre = transform_response(
        app.clone()
            .oneshot(claude_transform_hook(
                "PreToolUse",
                "tool-original-id",
                original.clone(),
            ))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(
        pre,
        json!({"continue": true, "hookSpecificOutput": {
            "hookEventName": "PreToolUse", "updatedInput": rewritten,
        }}),
        "a rewrite must not grant permission or drop unchanged input fields"
    );
    assert_eq!(
        *seen.lock().unwrap(),
        vec![original.clone()],
        "pre-tool guardrails run once on the proposed input"
    );

    // PermissionRequest remains a separate decision and must match the input actually returned.
    let permission = transform_response(
        app.clone()
            .oneshot(claude_transform_hook(
                "PermissionRequest",
                "tool-original-id",
                rewritten.clone(),
            ))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(
        permission["hookSpecificOutput"]["decision"]["behavior"],
        "allow"
    );
    assert!(
        permission["hookSpecificOutput"]
            .get("updatedInput")
            .is_none()
    );
    let stale = transform_response(
        app.clone()
            .oneshot(claude_transform_hook(
                "PermissionRequest",
                "tool-original-id",
                original,
            ))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(stale["hookSpecificOutput"]["decision"]["behavior"], "deny");

    let post = transform_response(
        app.clone()
            .oneshot(claude_transform_hook(
                "PostToolUse",
                "tool-original-id",
                rewritten.clone(),
            ))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(post, json!({"continue": true}));
    let next = transform_response(
        app.oneshot(claude_transform_hook(
            "PreToolUse",
            "tool-next-id",
            json!({"command": "pwd"}),
        ))
        .await
        .unwrap(),
    )
    .await;
    assert_eq!(
        next,
        json!({"continue": true}),
        "a rewrite belongs only to the response for its tool"
    );
    flush_subscribers().unwrap();
    let recorded = recorded.lock().unwrap();
    let events: Vec<_> = recorded
        .iter()
        .filter(|event| event["category_profile"]["tool_call_id"] == "tool-original-id")
        .collect();
    assert_eq!(
        events.len(),
        2,
        "the native tool ID must retain one start/end pair"
    );
    assert_eq!(events[0]["data"], rewritten);
    assert_eq!(events[0]["uuid"], events[1]["uuid"]);
}

#[tokio::test]
async fn claude_pre_tool_rewrite_does_not_bypass_a_guardrail_rejection() {
    let _guard = PLUGIN_CONFIG_TEST_LOCK.lock().await;
    const GUARDRAIL: &str = "cli-claude-transform-block";
    const INTERCEPT: &str = "cli-claude-transform-block-witness";
    register_tool_conditional_execution_guardrail(
        GUARDRAIL,
        1,
        Arc::new(|_, _| Box::pin(async { Ok(Some("blocked before rewrite".into())) })),
    )
    .unwrap();
    let _policy = ToolGuardrailCleanup(GUARDRAIL);
    let calls = Arc::new(AtomicUsize::new(0));
    register_tool_request_intercept(
        INTERCEPT,
        1,
        false,
        Arc::new({
            let calls = calls.clone();
            move |_, args| {
                let calls = calls.clone();
                Box::pin(async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    Ok(args)
                })
            }
        }),
    )
    .unwrap();
    let _intercept = ToolInterceptCleanup(INTERCEPT);
    let response = router(test_config())
        .oneshot(claude_transform_hook(
            "PreToolUse",
            "blocked-id",
            json!({"command": "pwd"}),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    let body: Value =
        serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(body["error"]["type"], "nemo_relay_guardrail_rejected");
    assert!(body.get("hookSpecificOutput").is_none());
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn claude_pre_tool_intercept_error_does_not_publish_a_rewrite() {
    let _guard = PLUGIN_CONFIG_TEST_LOCK.lock().await;
    const INTERCEPT: &str = "cli-claude-transform-error";
    register_tool_request_intercept(
        INTERCEPT,
        1,
        false,
        Arc::new(|_, args| {
            Box::pin(async move {
                if args["command"] == "fail-intercept" {
                    Err(nemo_relay::error::FlowError::Internal(
                        "rewrite failed".into(),
                    ))
                } else {
                    Ok(args)
                }
            })
        }),
    )
    .unwrap();
    let _intercept = ToolInterceptCleanup(INTERCEPT);
    let app = router(test_config());
    let failed = app
        .clone()
        .oneshot(claude_transform_hook(
            "PreToolUse",
            "failed-id",
            json!({"command": "fail-intercept"}),
        ))
        .await
        .unwrap();
    assert_eq!(failed.status(), StatusCode::INTERNAL_SERVER_ERROR);
    let failed: Value =
        serde_json::from_slice(&failed.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert!(failed.get("hookSpecificOutput").is_none());
    let next = transform_response(
        app.oneshot(claude_transform_hook(
            "PreToolUse",
            "next-id",
            json!({"command": "pwd"}),
        ))
        .await
        .unwrap(),
    )
    .await;
    assert_eq!(next, json!({"continue": true}));
}

#[tokio::test]
async fn claude_pre_tool_rejects_a_non_object_intercept_result_before_tool_start() {
    let _guard = PLUGIN_CONFIG_TEST_LOCK.lock().await;
    const INTERCEPT: &str = "cli-claude-transform-invalid";
    const SUBSCRIBER: &str = "cli-claude-transform-invalid-events";
    register_tool_request_intercept(
        INTERCEPT,
        1,
        false,
        Arc::new(|_, _| Box::pin(async { Ok(json!(["invalid"])) })),
    )
    .unwrap();
    let _intercept = ToolInterceptCleanup(INTERCEPT);
    let starts = Arc::new(AtomicUsize::new(0));
    register_subscriber(
        SUBSCRIBER,
        Arc::new({
            let starts = starts.clone();
            move |event| {
                if event.name() == "Bash" && event.scope_category() == Some(ScopeCategory::Start) {
                    starts.fetch_add(1, Ordering::SeqCst);
                }
            }
        }),
    )
    .unwrap();
    let _subscriber = SubscriberCleanup(SUBSCRIBER);
    let response = router(test_config())
        .oneshot(claude_transform_hook(
            "PreToolUse",
            "invalid-id",
            json!({"command": "pwd"}),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let body: Value =
        serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert!(body.get("hookSpecificOutput").is_none());
    flush_subscribers().unwrap();
    assert_eq!(
        starts.load(Ordering::SeqCst),
        0,
        "invalid updatedInput must never become a recorded execution"
    );
}

#[tokio::test]
async fn claude_retried_pre_tool_returns_the_same_rewrite_without_reapplying_policy() {
    let _guard = PLUGIN_CONFIG_TEST_LOCK.lock().await;
    const INTERCEPT: &str = "cli-claude-transform-replay";
    let calls = Arc::new(AtomicUsize::new(0));
    register_tool_request_intercept(
        INTERCEPT,
        1,
        false,
        Arc::new({
            let calls = calls.clone();
            move |_, mut args| {
                let calls = calls.clone();
                Box::pin(async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    args["command"] = json!(format!(
                        "supervisor -- {}",
                        args["command"].as_str().unwrap()
                    ));
                    Ok(args)
                })
            }
        }),
    )
    .unwrap();
    let _intercept = ToolInterceptCleanup(INTERCEPT);
    let app = router(test_config());
    let original = json!({"command": "pytest", "timeout": 12000});
    let mut replies = Vec::new();
    for _ in 0..2 {
        replies.push(
            transform_response(
                app.clone()
                    .oneshot(claude_transform_hook(
                        "PreToolUse",
                        "retry-id",
                        original.clone(),
                    ))
                    .await
                    .unwrap(),
            )
            .await,
        );
    }
    assert_eq!(
        replies[0]["hookSpecificOutput"]["updatedInput"]["command"],
        "supervisor -- pytest"
    );
    assert_eq!(
        replies[0], replies[1],
        "a lost first response must not let a retried hook execute unwrapped arguments"
    );
    let rewritten = replies[0]["hookSpecificOutput"]["updatedInput"].clone();
    let already_rewritten = transform_response(
        app.clone()
            .oneshot(claude_transform_hook("PreToolUse", "retry-id", rewritten))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(
        replies[0], already_rewritten,
        "retrying the returned input must not wrap it twice"
    );
    let conflict = app
        .oneshot(claude_transform_hook(
            "PreToolUse",
            "retry-id",
            json!({"command": "another command", "timeout": 12000}),
        ))
        .await
        .unwrap();
    assert_eq!(
        conflict.status(),
        StatusCode::BAD_REQUEST,
        "reusing a transformed call ID with unrelated input is not a retry"
    );
    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "retry must replay the decision, not execute middleware twice"
    );
}
