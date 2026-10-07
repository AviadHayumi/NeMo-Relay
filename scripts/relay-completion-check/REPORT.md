<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Can we use Relay alone to tell when Claude still has work?

This records the October 7, 2026 investigation and measured results. The current
focus is **Claude Code doing a real coding task**. We reproduced and fixed
Relay lifecycle bugs inside Relay, including an early child completion and a
late-result path that invented new work. **Whole-sandbox idleness is still not
proven**; detached work and missing native events remain explicit gaps.

Earlier two-harness probes
also included Codex; those results are kept, but new live runs focus on Claude.

The product signal under test is Relay. We do not feed a Fleet process observer,
the test journal, or `strace` results into Relay. Those independent records let
us check whether Relay tells the truth.

## The real task

We give the unmodified Claude Code CLI [Click issue 3362](https://github.com/pallets/click/issues/3362).
It must clone Click at tag `8.3.1`, reproduce a hyphenated option being split in
usage text, add a failing test, fix the code, and run an independent native
reviewer subagent. It then reruns tests and writes a report. All changes stay
inside the test sandbox. Nothing is pushed or posted to Click.

There are no artificial waits in this task. A bounded timeout stops a runaway
test; hitting it counts as incomplete evidence, never a pass.

<details>
<summary>What exactly do we record?</summary>

- The complete native hook payload, with its receipt timestamp.
- Whether Relay's own hook forwarder delivered that hook successfully.
- Relay's complete ATOF event stream, including scope IDs and synthetic-close reasons.
- Claude's structured output, errors, final result, and the exact CLI/Relay versions.
- The resulting Click patch, the written report, and actual test output.
- `strace` process creation, execution, and exit records. This is a standard Linux
  diagnostic tool, used only in the test. The selected process-only trace does
  not capture environment values or network request bodies.

The stock harness image stays unchanged. Test utilities and Relay are mounted
into isolated test Pods. No Kubernetes service-account token is mounted.

</details>

## What “finished” means in these tests

We compare matching operations, not just counters. For each native tool ID we
compare its native completion, its Relay scope end, and the available independent
process record. A missing match is reported as missing or inconclusive.

An early Relay scope end can hide work. A late or missing end can make completed
work look active. We initially allow 50 ms for early-boundary comparison and
one second for delayed delivery. These are experiment tolerances, not a
suspension policy. `strace` adds overhead; these instrumented runs are correctness
checks, not performance benchmarks.

<details>
<summary>Why a closed tool scope does not always mean the sandbox is idle</summary>

A command can start a detached process and return immediately. Its shell tool
really has returned, so closing that tool scope is correct. The detached process
may still be working. That is a coverage gap for a whole-sandbox claim, not
necessarily an incorrect tool timestamp.

Likewise, a parent response can finish while its reviewer continues. In that
case the parent response may close, but the reviewer's own scope and tools must
stay open until their own completion signals arrive.

Relay currently provides lifecycle telemetry and an internal plugin-liveness
check. This investigation does not pretend that an existing public Relay API
already certifies sandbox idleness or safe suspension. Model request presence
also does not prove that every thread is waiting.

</details>

## Findings so far

The initial Kubernetes probes used published Relay `0.9.4`, Claude Code
`2.1.280`, and Codex `0.149.1`. All timings below come from saved events and a
bounded CPU-work journal, not from the agent's written answer.

| Finding | What happened | Relay-only response |
| --- | --- | --- |
| Parent Stop closed a running Claude child | The child's CPU command continued about 3.84 seconds after Relay ended its tool scope. The parent Stop payload explicitly listed the child as running. | Preserve child scopes and their tools/model work across parent response boundaries. |
| Codex child prompt closed parent work | Relay ended the parent's eight-second command about 8.07 seconds before the command ended. | Use native child identity when routing prompt and Stop boundaries. |
| Quiet-time cleanup could close a child without its end event | The original sweeper did not require the active-child collection to be empty. | Keep known children active until their own completion or explicit teardown. |
| Late child activity could create an empty parent turn | A child completion or follow-up request could lazily reopen parent state. | Preserve child ownership and avoid inventing a new parent turn for known child activity. |
| Claude background snapshots could be lost in the final answer | Turn-end output can be replaced by the final LLM output. | Export the native task snapshot as a separate Relay event before the boundary closes. |
| API-error evidence was absent from installed hooks | Claude supports `StopFailure`; the Relay hook list omitted it. | Capture the event as unverified error evidence. Do not assume an internal helper error ends the parent. |
| A late result with a changed tool ID reopened a parent turn | Name-and-arguments fallback found the retained child tool, but the code had already created a new parent turn. A focused regression reproduced this. | Reconcile the existing tool before deciding whether a new parent turn is needed. |

The first two entries were reproduced in the cluster. The other entries were
established by source inspection and focused regression tests; they are not
seven independent live-cluster failures. The starting source revision is
`db4c6c520d66f15acf28b21be179a6d576254a1b`.

## Real Click task: first completed run

With unmodified Relay from that source revision, Claude completed its invocation
in **162.1 seconds**, made **36 tool calls**, and started and completed **one
native reviewer subagent**. There were **79 recorded hooks and zero delivery
failures**. All 36 tool endings and the child ending matched Relay within the
experiment tolerances. All 662 process/thread trace files contained terminal
events; this number counts threads as well as processes, not 662 commands.

This real task did **not** reproduce the early-child-closure bug seen in the
concurrent CPU fixture. Both results matter: a passing sequential coding task
cannot replace the concurrency regression.

<details>
<summary>Did the coding task itself really run tests?</summary>

Yes. The run cloned Click at tag `8.3.1`, edited code and tests, invoked pytest,
and used the native Agent tool for review. Independent verification applied its
new tests to the original source: **1 failed, 19 passed**. Against the changed
source: **20 passed**. This is a real red/green regression check.

That does not certify every design choice in its Click patch. The report still
shows an overlong option breaking at a very narrow width, and the broad wrapping
change needs scrutiny for normal hyphenated prose. We keep patch correctness
separate from whether Relay recorded execution correctly. The work is local;
no Click PR, push, or comment was made.

</details>

<details>
<summary>What did the first patched attempt teach us?</summary>

It was incomplete. The model spent much longer revisiting reproduction and
waiting for responses, then reached its turn cap without starting the reviewer.
The test initially also used an outer execution timeout shorter than its remote
run allowance. Stopping strace did not guarantee stopping its traced CLI.

We corrected the runner's time budget and process-group cleanup. The incomplete
run is retained as evidence and cannot pass the analyzer's conformance check.
A real-task pass requires a completed reviewer, saved report, successful hook
delivery, complete process traces, and matching operation boundaries. It still
does not imply a correct Click patch or whole-sandbox suspension safety.

</details>

The patched continuation completed in **320.4 seconds**, with **46 tools**,
**one completed native reviewer**, and a saved report. All tool endings matched;
Relay ended the reviewer scope about **94 ms after** its native completion.
All **841** process/thread traces contain terminal events. Completing a resumed
attempt does not erase the earlier incomplete attempt. The further native-Claude
follow-up below addresses the prose-wrapping finding.

## Final Click correction: useful work, not just an activity demo

After receiving the concrete prose-regression finding, Claude made a focused
correction in a native continuation, requested another native reviewer, and
finished in **232.9 seconds**. This invocation made **35 tool calls**; all their
Relay endings matched. The reviewer completed, all hook deliveries succeeded,
and the process/thread trace was complete.

Independent checks confirmed:

- Original Click `8.3.1` plus the new tests: **2 failed, 19 passed**.
- Corrected source, focused tests: **21 passed**.
- Corrected source, full suite: **1,318 passed, 21 skipped, 1 xfailed** with the
  recorded filter for a pre-existing pytest deprecation warning.
- **72 ordinary-prose outputs** across `wrap_text`, `write_text`, and `write_dl`
  at six widths exactly match the original version.

The code keeps ordinary wrapping's `break_on_hyphens=True` default and turns it
off only at the two usage-formatting call sites. Options wider than the available
column still use Click's existing character-wrapping fallback; this exercise fixes
the reported hyphen boundary, not that separate overflow policy. The earlier broad patches and
their failed requirements remain in the evidence. We did not accept the agent's
first confident report as sufficient proof.

## Final live regression: parent answers before its child finishes

The final binary passed the scenario that exposed the bug. Claude used its
native background Agent option. The parent ran an eight-second CPU command;
the child ran a twenty-second CPU command. Their work overlapped, and a real
parent `Stop` arrived while the child was still working.

- Before: published Relay ended the child's tool **3.835 seconds before** its
  independently recorded CPU work ended.
- After: the final patched Relay ended that tool **0.178 seconds after** the
  independent work-end record. The parent had answered about **11 seconds**
  before the child finished. It ended the child scope **0.097 seconds after**
  native `SubagentStop`.
- All three tool boundaries matched, every hook delivery succeeded, and all
  recorded processes/threads ended.

The first final-build attempt delegated synchronously. Its events matched, but
there was no overlapping work or parent Stop during child execution. It is
saved as **insufficient concurrency coverage**, not silently counted as a
passing reproduction. See [final-subagent.json](evidence/final-subagent.json).

## Source validation

After the final late-result fix:

- `just test-rust`: **5,378 passed**, no skips or retries (5,335 workspace tests
  plus 43 tests in the Rust example suites).
- `cargo clippy -p nemo-relay-cli --all-targets -- -D warnings`: passed.
- `cargo fmt --all -- --check`: passed.
- The new late-ID regression failed before the fix; eight related tests passed
  after it, including existing post-only tool behavior.
- The Python experiment scripts pass Ruff, formatting, compilation, and a scoped
  `ty` check. Repository copyright and script-executable checks passed.

<details>
<summary>Why did the first large test run show failures?</summary>

Adding `StopFailure` changed the installed Claude hook count from 14 to 15; the
corresponding descriptor expectation needed updating. Five other failures came
from the disposable builder using `sleep` as PID 1: exited descendants remained
zombies, so process-existence assertions saw them as present. The same five tests
passed under verified Tini with no production process changes. The final full
run used that process reaper and passed without retries.

The final Linux amd64 Relay binary has SHA256
`db3295a74c7cf73c5fd7e9160d26d5bfc73989d54da7f9f9a79dc66722dcb9e9`.
The earlier real Click continuation used the preceding patch build,
`25b16fcaa026e836778524528d92be6d4a23d4c177dc9f766507763ea0b7bbed`;
the final binary adds the tested late-ID fix. Each live result records its binary
hash, so these builds are not silently treated as identical.

</details>

## Evidence locations

- Persistent local raw records and test logs: `artifacts/relay-completion-check/20261007/`
  at the repository root. This directory is ignored by Git and restricted to the
  local user. It includes the temporary collection paths recorded below.
- Published-release probe logs: `/private/tmp/relay-completion-20261007-raw/`.
- Initial failing Codex regressions: `/private/tmp/relay-completion-baseline-tests.log`.
- Reusable runner, oracle, collector, analyzer, and HTML command-timeline renderer:
  this directory. See [README.md](README.md) for commands.
- Reviewed timing comparisons: [evidence/README.md](evidence/README.md).
- Final Click task: [report](evidence/click-final-report.md),
  [patch](evidence/click-final.patch), and
  [independent checks](evidence/click-final-red-green.json).
- Final source validation and hashes: [source-validation.json](evidence/source-validation.json).
- Claude-specific source findings: [claude-notes.md](claude-notes.md).
- Independent model review and full-source adjudication: [peer-review.md](peer-review.md).

Raw logs can include task text and model output. They remain outside Git until
reviewed. Sanitized summaries must distinguish an attempted scenario from one
whose required behavior actually occurred.

## Remaining gaps to solve inside Relay first

1. How should Relay track a managed background shell after its launch tool returns?
2. What should it report when a completion hook is missing or its owner is ambiguous?
3. What evidence can Relay obtain for detached work that the harness itself does not track?
4. What distinguishes a model wait from concurrent local work during streaming?

The next Relay-only step is an explicit activity ledger: track native task IDs,
record unresolved or missing events, and expose an **unknown** result when the
available evidence cannot establish completion. Keep “outstanding operation”
separate from “executing local code”: an open model request may be waiting while
another thread or tool still works.

For detached work absent from the harness's events, current hooks have no end
signal to consume. Relay would need to own or instrument that execution, or the
supported workload contract would need to rule it out. Moving the same existing
events into another dashboard does not create that missing evidence. These are
remaining implementation choices, not capabilities delivered by this patch.

All temporary experiment Pods, their ConfigMaps, and the Rust builder were
removed after copying and hashing the raw evidence. The existing Fleet workloads
were not changed. The Relay source changes remain local and uncommitted; nothing
was pushed to NVIDIA or Click, and no issue or pull request was posted.

No Fleet UI rollout, Substrate integration, checkpoint, freeze, or resume is part
of these experiments.
