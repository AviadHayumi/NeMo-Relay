<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Final local Click task report — independently checked

The native Claude agent wrote the report below after fixing the identified
prose-wrapping regression. Independent verification after the invocation found:

- Fixed formatting suite: **21 passed**.
- Original tag 8.3.1 with the new tests: **2 failed, 19 passed**.
- Fixed complete suite with the existing pytest warning filtered:
  **1318 passed, 21 skipped, 1 xfailed**.
- **72** prose outputs across `wrap_text`, `write_text`, and `write_dl`, at
  widths 15, 20, 32, 50, 65, and 80, exactly matched the original tag.

This is a locally verified task artifact, not an upstream-approved Click change.
The documented character-wrapping fallback at extremely narrow widths remains.
Relay timing conclusions are separate from the quality of this patch; see
[the measured invocation results](patched-real-click.json).

---

# Click Issue #3362 Fix Report (Continuation)

**This is a continuation of the same original task.**  Prior invocations cloned
the repo at tag 8.3.1, reproduced the bug, applied a fix, and ran tests.  This
invocation corrects a scope problem: the previous fix applied `break_on_hyphens=False`
to all `wrap_text()` callers (including prose), which was too broad.  The fix is now
scoped to `write_usage` only.

Repository: `/workspace/patched-click`  
Tag: `8.3.1` (detached HEAD `1d038f2`)  
Issue: https://github.com/pallets/click/issues/3362

---

## 1. Scope Problem (Previous Invocation)

The prior fix set `break_on_hyphens=False` unconditionally in `wrap_text()`'s
`TextWrapper` constructor.  This changed wrapping behavior for `write_text()` and
`write_dl()` (ordinary help prose) too — a regression in the generic behavior that
unmodified Click 8.3.1 never had.

---

## 2. Reproduction

At width=65 with 36-char padding, the unmodified code splits `--max-retry-count`:

```
Usage: prog AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA --max-retry-
            count VALUE
```

Root cause: `wrap_text()` used stdlib's `TextWrapper` with the default
`break_on_hyphens=True`, allowing textwrap to treat any `\w-\w` sequence as a
valid line-break point.  When `--max-retry-` (12 chars) fits in the remaining
space but `--max-retry-count` (17 chars) does not, textwrap splits there.

---

## 3. Changed Files

### `src/click/formatting.py` — scoped fix (+8 lines)

```diff
+    break_on_hyphens: bool = True,            # new param on wrap_text(), default True
 ) -> str:
     ...
+        break_on_hyphens=break_on_hyphens,    # TextWrapper constructor

 # in write_usage() — both call sites:
+                    break_on_hyphens=False,   # usage lines: no hyphen breaks
```

- `wrap_text()` gains a backward-compatible `break_on_hyphens: bool = True` parameter.
- The `TextWrapper` constructor passes it through (was always `True` implicitly before).
- Both `write_usage()` call sites pass `break_on_hyphens=False`.
- `write_text()` and `write_dl()` are **untouched** — they keep the default `True`.

### `tests/test_formatting.py` — four new tests (+94 lines)

| Test | What it checks | Fails on 8.3.1? |
|---|---|---|
| `test_write_usage_no_split_on_hyphen` | width=65 padded case; option token intact | **YES** |
| `test_write_usage_option_intact_at_fitting_widths` | widths 32-34 where option fits whole | no (stability guard) |
| `test_write_usage_no_split_on_hyphen_various_widths` | widths 24-27; hyphen-split fingerprint absent | **YES** |
| `test_wrap_text_prose_preserves_hyphen_breaks` | `wrap_text()` prose still splits at hyphens | no (regression guard) |

---

## 4. Overflow Policy

When the option token is **longer than the available column** after the continuation
indent (e.g., widths below 28 for `--max-retry-count` with an 11-char indent), the
character-level fallback in `_handle_long_word` still applies.  The fix does **not**
claim to prevent all breaks — only the hyphen-logic split where the option *could*
fit as a whole token but textwrap picks a hyphen boundary instead.

`HelpFormatter(width=N)` does **not** clamp `N` to any minimum when a caller passes
an explicit `width`; the 50-char minimum in `__init__` only applies to the
auto-detected terminal-size path.

---

## 5. After Fix (correct output)

```
Usage: prog AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            --max-retry-count VALUE
```

---

## 6. Red/Green Test Proof

### RED — unmodified 8.3.1 source, new tests present

```
$ git checkout HEAD -- src/click/formatting.py   # revert fix, keep tests
$ PYTHONPATH=src pytest tests/test_formatting.py::test_write_usage_no_split_on_hyphen \
    tests/test_formatting.py::test_write_usage_option_intact_at_fitting_widths \
    tests/test_formatting.py::test_write_usage_no_split_on_hyphen_various_widths \
    tests/test_formatting.py::test_wrap_text_prose_preserves_hyphen_breaks \
    -v -W ignore::pytest.PytestRemovedIn10Warning

FAILED test_write_usage_no_split_on_hyphen
  AssertionError: 'Usage: prog AAA...AAA --max-retry-\n            count VALUE\n'
FAILED test_write_usage_no_split_on_hyphen_various_widths
  AssertionError: '--max-retry-\n' found at widths 24-27
PASSED test_write_usage_option_intact_at_fitting_widths   (stability guard — passes in both states)
PASSED test_wrap_text_prose_preserves_hyphen_breaks       (regression guard — passes in both states)

EXIT: 1
```

### GREEN — scoped fix applied

```
$ PYTHONPATH=src pytest tests/test_formatting.py -v -W ignore::pytest.PytestRemovedIn10Warning

21 passed in 0.09s   EXIT: 0
```

### Full suite

```
$ PYTHONPATH=src pytest tests/ -W ignore::pytest.PytestRemovedIn10Warning -q

1318 passed, 21 skipped, 1 xfailed   EXIT: 0
```

(`test_basic.py` collection error on unmodified 8.3.1 is a pre-existing pytest 9.x
incompatibility with `itertools.chain` in parametrize; suppressed with
`-W ignore::pytest.PytestRemovedIn10Warning` which makes collection succeed.)

---

## 7. Independent Reviewer Findings

A native Agent reviewer independently applied the intended fix and ran checks:

- **Fix correctness**: all three changes (signature, TextWrapper constructor, both
  `write_usage` call sites) present and correct.
- **Prose behavior**: `wrap_text()` default `True` correctly preserves stdlib
  hyphen-break behavior; `write_text()` prose still splits `long-running` at the
  hyphen when forced by width.
- **API compatibility**: `wrap_text` is exported publicly in `__init__.py`;
  new parameter has a default so all existing call sites are unaffected.
- **No remaining concerns**.

---

## 8. Prose Scope Finding — RESOLVED

The previous invocation's broad `break_on_hyphens=False` on all `wrap_text()` calls
changed ordinary prose wrapping.  This invocation resolves it by:
- Defaulting `break_on_hyphens=True` in `wrap_text()` (unchanged stdlib behavior).
- Passing `break_on_hyphens=False` only from `write_usage()`.
- Adding `test_wrap_text_prose_preserves_hyphen_breaks` as a direct expected-output
  regression confirming prose wrapping matches unmodified Click 8.3.1.

---

## 9. Exact Commands

```bash
cd /workspace/patched-click

# RED
git checkout HEAD -- src/click/formatting.py
PYTHONPATH=src /work/evidence/patched-real-click/home/.local/bin/pytest \
  tests/test_formatting.py::test_write_usage_no_split_on_hyphen \
  tests/test_formatting.py::test_write_usage_no_split_on_hyphen_various_widths \
  -v -W ignore::pytest.PytestRemovedIn10Warning
# EXIT: 1

# Restore fix (re-apply break_on_hyphens parameter and write_usage call sites)

# GREEN — formatting tests
PYTHONPATH=src /work/evidence/patched-real-click/home/.local/bin/pytest \
  tests/test_formatting.py -v -W ignore::pytest.PytestRemovedIn10Warning
# EXIT: 0  (21 passed)

# GREEN — full suite
PYTHONPATH=src /work/evidence/patched-real-click/home/.local/bin/pytest \
  tests/ -W ignore::pytest.PytestRemovedIn10Warning -q
# EXIT: 0  (1318 passed, 21 skipped, 1 xfailed)
```
