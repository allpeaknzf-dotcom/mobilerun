# Mobilerun v0.6.16 Integration Handoff

Date: 2026-08-14

## Safety State

- Stable worktree: `/Users/opay-20250140/Documents/mobilerun app自动化测试/mobilerun`
- Integration worktree: `/Users/opay-20250140/Documents/mobilerun app自动化测试/mobilerun-v0.6.16-integration`
- Integration branch: `codex/integrate-v0.6.16`
- Stable commit before integration: `9c8c5cb`
- Backup branch: `backup/pel-before-v0.6.16`
- The stable branch has not been modified by this integration.
- Do not merge into the stable branch automatically.

## Completed

- Integrated official `v0.6.16` as the base.
- Selectively migrated Web/H5 support and PEL hybrid locator recovery.
- Preserved official Android/iOS behavior, local core imports, response validation,
  malformed tool-call protection, and screenshot resize behavior.
- Added Web config migration version 8 and optional Playwright dependency.
- Migrated Token optimization:
  - optional compact FastAgent tool definitions
  - full definitions on first turn and every 15 tool calls
  - repeated device state replaced by `<device_state_unchanged/>`
  - duplicate previous-state injection removed
  - stateless manager previous-state compatibility retained
- Added direct behavior tests for Token optimization.
- Fixed PEL auto-discovery so synthetic WebPage root nodes are not used as
  cache fingerprint keys; this restores the cache fast path for Web pages.
- Added regression test for WebPage key text exclusion.

## Verification Completed

- Token/manager/malformed-call targeted regression:
  - `87 passed, 32 subtests passed`
- Full test suite after Web integration:
  - `688 passed, 35 subtests passed`
- Ruff on all changed Token optimization files:
  - passed
- `git diff --check`:
  - passed
- `uv lock`:
  - completed successfully

Earlier real-Chromium PEL shadow DOM tests now pass as part of the full suite.
Browser smoke also verified Web cache hit, popup/tab switch refresh, and
explicit mark_dirty refresh.

## Browser Verification

- `tests/test_pel.py`: `68 passed` including 3 real Chromium shadow DOM tests.
- Browser smoke: passed unchanged-page cache hit, popup/tab switch state
  refresh, and `mark_dirty()` state refresh after DOM mutation.
- Found and fixed a real PEL auto-discovery cache bug: synthetic `WebPage`
  root nodes were included in `key_element_texts`, so web pages could never
  take the lightweight cache fast path.
- Added regression:
  `tests/test_pel_auto_discoverer.py`.

## Interrupted Work

The following command was interrupted at the user's request while the Chromium
download was around 90%:

```bash
uv run --extra web playwright install chromium
```

The download may need to restart or resume. No tests, commits, pushes, merges,
or other operations were started after the interruption.

## Next Steps

Run from the integration worktree:

```bash
cd "/Users/opay-20250140/Documents/mobilerun app自动化测试/mobilerun-v0.6.16-integration"
uv run --extra web playwright install chromium
uv run --extra anthropic --extra web pytest -q tests/test_pel.py
```

Confirm that the 3 real-browser shadow DOM tests execute instead of skipping.
Then add or run a browser-backed WebDriver check for page/popup switching and
state-cache invalidation, because popup cache invalidation was a known PEL risk.

After browser verification:

```bash
uv run --extra anthropic --extra web pytest -q
uv run ruff check \
  mobilerun/agent/action_context.py \
  mobilerun/agent/droid/droid_agent.py \
  mobilerun/agent/fast_agent/fast_agent.py \
  mobilerun/agent/manager/manager_agent.py \
  mobilerun/agent/manager/stateless_manager_agent.py \
  mobilerun/agent/utils/actions.py \
  mobilerun/agent/utils/context_hasher.py \
  mobilerun/agent/utils/dom_extractor.py \
  mobilerun/agent/utils/page_actions.py \
  mobilerun/agent/utils/signatures.py \
  mobilerun/config_manager/config_manager.py \
  mobilerun/config_manager/migrations \
  mobilerun/element \
  mobilerun/pages \
  mobilerun/tools/driver/web.py \
  mobilerun/tools/ui/cached_provider.py \
  mobilerun/tools/ui/web_provider.py \
  tests/test_context_optimization.py \
  tests/test_manager_prompt_selection.py \
  tests/test_pel.py
git diff --check
git status --short --branch
git diff --stat
```

Review the full diff before committing. Commit in logical layers and push only
`codex/integrate-v0.6.16`. Do not merge it into the stable branch without
explicit user approval.
