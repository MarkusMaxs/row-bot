"""Row-Bot's opt-in live eval and verification harness (cost plan WP B3).

Nothing here runs in the deterministic lanes: the live tests are
``tests/e2e/test_live_eval.py`` and ``tests/e2e/test_live_verification.py``
(marker ``live_provider``, and ``ROW_BOT_LIVE_EVAL=1``), and the runner is
``scripts/live_eval.py``. Credentials come from environment variables only;
every run uses fresh data folders under ``.tmp/live-eval/``.

Modules:

- ``tasks``: the eval tasks and their deterministic pass checks.
- ``providers``: which providers have credentials in the environment.
- ``budget`` / ``pricing``: the USD and token caps, and costs from the
  provider or a rates file (no built-in prices).
- ``report``: per-task records, the JSON/Markdown report and the A/B diff.
- ``variants``: A/B configurations as test-only patches.
- ``orchestrate``: plan (no network), run, A/B, diff, ledger; the CLI.
- ``worker`` / ``runner``: one provider's tasks through the real Agent runtime.
- ``checks`` / ``manual``: the live verification checks and the manual checklist.
"""
