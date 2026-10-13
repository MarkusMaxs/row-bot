"""The focused live verification checks, by id: which work package asked for them and where they run.

``tests/e2e/test_live_verification.py`` implements one test per entry. Some
are assertions (the behaviour must hold); the ones marked ``probe`` record what
a provider accepts, for a decision, and pass whatever the answer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationCheck:
    check_id: str
    source: str
    providers: tuple[str, ...]
    description: str
    probe: bool = False
    est_tokens: int = 20_000


CHECKS: tuple[VerificationCheck, ...] = (
    VerificationCheck(
        "a2b_controls_accepted", "A2b", ("anthropic",),
        "thinking-binding beta + drop_block accepted on Opus 5.5 / Sonnet 5.5 / Haiku 5.5 / Fable 5.1 (and explicit "
        "adaptive Opus 4.8), streaming and not; input_transformations present and empty; no 'rejected controls' retry",
        est_tokens=12_000,
    ),
    VerificationCheck(
        "a2b_drop_block_on_edit", "A2b", ("anthropic",),
        "after a tool round on Opus 5.5, an edited system prompt replays with thinking_dropped/prefix_binding_mismatch "
        "and Row-Bot's INFO line",
        est_tokens=30_000,
    ),
    VerificationCheck(
        "a2b_error_mode_recovers", "A2b", ("anthropic",),
        "prefix_mismatch_behavior 'error' on an edited history: exactly one strip-and-retry, then 200 with no thinking "
        "blocks and no reasoning fallback; also the no-header (remembered route) case; records whether the account "
        "is enforced",
        est_tokens=40_000,
    ),
    VerificationCheck(
        "a2b_model_switch", "A2b", ("anthropic",),
        "Opus 5.5 thinking replayed to Sonnet 5.5 reports model_binding_mismatch", probe=True, est_tokens=20_000,
    ),
    VerificationCheck(
        "a3_ttl_1h_and_5m_reuse", "A3", ("anthropic",),
        "a 1h breakpoint writes ephemeral_1h tokens and is read next call; then a 5m request on the same prefix "
        "(records whether the 1h entry is read)",
        est_tokens=25_000,
    ),
    VerificationCheck(
        "a3_agent_loop_cache", "A3/A4a", ("anthropic",),
        "eval tasks warm_loop_cache and long_turn_positions through the agent: cache reads dominate input after the "
        "first call, every later call reads the earlier prefix (>15 positions), a workflow run writes no 1h entries",
        est_tokens=500_000,
    ),
    VerificationCheck(
        "a3_gateway_markers", "A3", ("openrouter", "requesty"),
        "OpenRouter anthropic/* honours block + top-level cache_control with ttl (no 400, reads on the second call); "
        "Requesty auto_cache reads; Requesty manual cache_control with ttl passthrough (probe)",
        est_tokens=40_000,
    ),
    VerificationCheck(
        "a3_gateway_acceptance_probe", "A3", ("minimax", "opencode_zen", "opencode_go", "atlascloud"),
        "MiniMax, OpenCode-Anthropic and Atlas: do block and top-level cache_control get accepted and reported?",
        probe=True, est_tokens=40_000,
    ),
    VerificationCheck(
        "f17_output_limit_detected", "A3 (F17)", (),
        "a capped streamed reply: which stop field and value each provider reports, and whether Row-Bot's truncation "
        "notice would fire (asserted for Anthropic, recorded elsewhere)",
        est_tokens=4_000,
    ),
    VerificationCheck(
        "b1_usage_shapes", "B1", (),
        "a streamed and a non-streamed call per provider reach the ledger with input/output counts; cache fields and "
        "OpenRouter cost recorded",
        est_tokens=8_000,
    ),
    VerificationCheck(
        "b1_ollama_prompt_eval_reuse", "B1", ("ollama",),
        "whether prompt_eval_count counts only new tokens when Ollama reuses its cache", probe=True, est_tokens=10_000,
    ),
    VerificationCheck(
        "b1_deepseek_hit_miss", "B1", ("deepseek",),
        "DeepSeek's own API (DEEPSEEK_API_KEY): prompt_cache_hit_tokens reaches the ledger as cache reads",
        est_tokens=12_000,
    ),
    VerificationCheck(
        "b2_openai_cache_key_and_store", "B2", ("openai",),
        "prompt_cache_key on Chat Completions and Responses, store:false + encrypted reasoning include accepted; "
        "cached tokens on the second call",
        est_tokens=20_000,
    ),
    VerificationCheck(
        "b2_openai_encrypted_rejection", "B2", ("openai",),
        "a garbled replayed reasoning item: one WARNING retry without it, a 200, and the 400 text names 'encrypted'",
        est_tokens=8_000,
    ),
    VerificationCheck(
        "b2_xai_conversation_header", "B2", ("xai",),
        "x-grok-conv-id is sent and the second call of a conversation reports cached tokens", probe=True,
        est_tokens=20_000,
    ),
    VerificationCheck(
        "b2_openrouter_session", "B2", ("openrouter",),
        "session_id is sent; two calls of a conversation land on the same upstream with cache reads", probe=True,
        est_tokens=20_000,
    ),
    VerificationCheck(
        "b2_ollama_keep_alive", "B2", ("ollama",),
        "eval task ollama_no_reload: one num_ctx, keep_alive 30m, no reload across agent, Chat Only, title, background "
        "(and vision with ROW_BOT_LIVE_EVAL_OLLAMA_VISION=1)",
        est_tokens=30_000,
    ),
    VerificationCheck(
        "c1_structured_output", "C1", ("anthropic", "openai", "google", "xai", "ollama"),
        "extraction (anyOf), insights (nullable), condition, verifier and contradiction schemas accepted at the "
        "lowest effort; records the effort sent and the reasoning tokens used against the 4096 headroom",
        est_tokens=60_000,
    ),
    VerificationCheck(
        "a4b_near_limit_and_overflow", "A4b", (),
        "with ROW_BOT_LIVE_EVAL_A4B=1 and ROW_BOT_LIVE_EVAL_A4B_MODEL: compaction before the limit, and a reactive "
        "overflow (inflated context) recovered by one compaction and retry",
        est_tokens=400_000,
    ),
)


def by_id() -> dict[str, VerificationCheck]:
    return {check.check_id: check for check in CHECKS}
