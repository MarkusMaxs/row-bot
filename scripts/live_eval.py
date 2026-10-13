"""Row-Bot's opt-in live eval (cost plan WP B3).

    uv run python scripts/live_eval.py plan                 # tasks x providers, estimated budget; no network
    uv run python scripts/live_eval.py run --checks         # needs ROW_BOT_LIVE_EVAL=1; spends money
    uv run python scripts/live_eval.py ab --a baseline --b cache-key-none --providers openai
    uv run python scripts/live_eval.py diff <a>/report.json <b>/report.json
    uv run python scripts/live_eval.py ledger --data-dir <validation data folder>
    uv run python scripts/live_eval.py manual

Credentials come from provider environment variables only (ANTHROPIC_API_KEY,
OPENAI_API_KEY, ...). Caps: ROW_BOT_LIVE_EVAL_MAX_USD (default 5) and
ROW_BOT_LIVE_EVAL_MAX_TOKENS (default 2,000,000). Reports land in
.tmp/live-eval/<timestamp>/. See tests/e2e/live_eval/ for the harness.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.e2e.live_eval.orchestrate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
