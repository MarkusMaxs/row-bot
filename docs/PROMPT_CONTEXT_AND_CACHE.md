# Prompt Context And Cache Contract

Row-Bot assembles Agent requests so that each request starts with the exact
bytes of the one before it. Provider prompt caches (Anthropic breakpoints,
OpenAI, Gemini, xAI and DeepSeek automatic caching, Ollama and llama.cpp KV
reuse) only pay off for that shared start, so:

- the system prompt is made only of **stable** sections;
- everything that changes from turn to turn travels in one **turn context**
  message per turn, appended after the turn's message and kept in the
  checkpoint;
- conversation history is replayed as it was stored.

## Stable sections

Stable sections change only when user settings, the provider or model, enabled
tools, skills, plugins, the Designer project's mode or the thread's profile
change. All of them may be marked cacheable.

- `agent.root`: identity line and agent guidelines from the ReAct system prompt.
- `agent.turn_context`: what the turn context is, and that only Row-Bot writes
  it (see Authority).
- `agent.profile`: active thread Agent Profile instructions. A child Agent gets
  its profile's instructions and handoff contract here only; its first message
  names the profile without repeating them.
- `platform.context`: local platform and shell guidance.
- `agent.runtime_guidance`: Agent Mode is active and tools are usable, plus the
  setup guidance (conversation setup, how to offer a tool that is off, apps
  turned off or switched off in this chat). The lists themselves are turn
  context values.
- `self_knowledge.static`: static Row-Bot feature knowledge and optional
  self-improvement guidance.
- `designer.guide`: Designer tools, HTML requirements and guidelines for the
  active project's mode. The project's values are turn context values.
- `agent.interactive_progress`: progress-update guidance for interactive
  surfaces.
- `background.override`: static background-workflow override.
- `background.persistent_thread`: persistent-thread note for tasks that keep
  their thread.
- `skills.tool_guides`: automatic tool guide instructions for enabled tools.
- `skills.manual`: the skills the conversation keeps: the profile's skills, the
  thread override and pinned skills.
- `chat_only.root`: Chat Only identity, behavior rules, and profile context.

Within a turn nothing edits these sections. A skill loaded with `skill_load`
arrives in the tool result (its full instructions are part of the result), and
a skill mentioned with `/skill` arrives in the turn context; neither is ever
moved into `skills.manual`, so an ongoing conversation keeps its prefix. A
loaded skill whose instructions are no longer in view (for example after
compaction, or in a conversation from before this contract) is delivered again
once in the next turn context.

## Turn context

`row_bot.turn_context` builds one app-authored `HumanMessage` per turn whose
text is a `<row_bot_turn_context>…</row_bot_turn_context>` block:

- **When.** Built on the turn's first model call (the turn is the execution
  budget of the run), and reused unchanged by every later call of the turn.
  Its values are gathered once: recall (embedding, search, graph lookup,
  `touch`, trace), the dynamic-state lookups, the Designer project read and
  the skill usage record all run once per turn.
- **Where.** Right after the turn's message, before its assistant and tool
  messages. The pre-model hook returns it as a `messages` update, so LangGraph
  persists it and every later turn replays it byte for byte. Providers receive
  it inside the user turn it follows (joined to that message as one more text
  part), which every provider shape accepts, including chat templates that
  reject two user messages in a row. A context that follows anything else
  (an event, a tool result) is a user message of its own.
- **What.** The current date and time (minute resolution, with the UTC offset
  and the time in UTC), the approval mode, the tool groups available, tools
  and apps that are off, Row-Bot's live state (model, providers, channels, MCP,
  Designer, Dream Cycle, update), the active Designer project's values (name,
  canvas, brand and CSS variables, brief, references, pages, recent manual
  edits, canvas rules), skills for this message, skills switched off after
  they were shown, and relevant long-term memories.
- **Diff-only.** After the first, a value appears only when it changed since
  the latest turn context still in view. The date is said again when the day
  changes; the time is one short line every turn. Recall lists only memories
  not already shown in view. A value that became empty says so where that
  matters (for example "TOOLS THAT ARE OFF: none.").
- **Full snapshot.** When no earlier turn context is in view, or a compaction
  summary replaced what the earlier ones said, the next turn context is a full
  snapshot. A compaction later in a turn appends a full snapshot after the
  latest message.
- **Comparison data.** Digests of each value, the memory and skill ids shown,
  and the summary in force are kept in the message's `additional_kwargs`. They
  never reach a provider: the request carries only the block's text.
- **Hidden from people.** The message is never a transcript row and is skipped
  by every reader of history: the transcript and history pages, exports and
  PDF, search, memory extraction, the conversation search tool, a child
  Agent's parent context, Chat Only and the legacy projection. Retry replaces
  it with its turn; removing a provisional answer keeps it with its message;
  compaction keeps it in its message's group.

Within one long turn the model sees the time the turn started, not the current
minute.

## Authority

Only Row-Bot writes a `<row_bot_turn_context>` block, and only in that
position. The stable `agent.turn_context` section says so. Before a request is
sent, the tag in any form is replaced (`[row_bot_turn_context tag removed]`) in
what people send (attached file context included) and in every tool result;
values Row-Bot quotes inside its own block (memories, project names, skill
instructions) are neutralized the same way. A message is recognized as a turn
context by its metadata, never by its text. The checkpoint keeps what was
sent. Approvals and tool gating stay enforced in code, and the approval policy
text stays in the stable prompt.

## Provider Rules

Prompt caching on Claude routes is automatic: there is no setting
(`row_bot.prompt_cache`, `row_bot.providers.capabilities.prompt_cache_route`,
`row_bot.providers.transports.prompt_cache_breakpoints`).

**Where the breakpoints go.**

- **System prompt.** One explicit breakpoint on the last stable system block.
- **Conversation tail.** An Agent or Chat Only request is tagged for tail
  caching (private metadata on its last message, never sent). The transport
  then caches up to the end of the request. On the direct API this is
  top-level automatic caching, which the API puts on the last cacheable block
  and moves forward as the conversation grows. The turn context never needs a
  breakpoint of its own.
- **Long appends.** Each breakpoint looks back at most 20 positions for an
  earlier cache entry. A run of `tool_use` blocks counts as one position, and
  so does a run of `tool_result` blocks. When a direct-API request adds more
  than 15 positions since the last breakpoint Row-Bot sent with the same
  prefix, one explicit breakpoint goes where that request ended.
- **Limits.** A request never carries more than four breakpoints. Stray
  markers in history are dropped before placement.
- **Summarizer.** The compaction summarizer's fixed system prompt gets a
  five-minute breakpoint. Its request is never tail-cached.
- **One-off calls** (titles, background jobs) get no tail caching.

**Lifetime (D3).** Both the system breakpoint and the tail use the request's
lifetime, so a longer one never follows a shorter one, which the API rejects.

| Request | Lifetime |
|---|---|
| A conversation a person is in: desktop and remote chat, voice, Designer, channels, channel approvals | 1 hour |
| Back-to-back loops: workflow and task runs, goal continuations (`goal` surface or `internal_goal_continuation`), child agents (`agent_child`) | 5 minutes |
| Compaction summary | 5 minutes |

A five-minute marker is `{"type": "ephemeral"}` (the API default), and a
one-hour marker adds `"ttl": "1h"`.

**Routes.**

| Route | What it gets |
|---|---|
| Direct Anthropic API | System breakpoint, top-level automatic caching, and the long-append breakpoint |
| OpenRouter `anthropic/claude-*` | The system prompt as one system message of text blocks, one per section, with the breakpoint on the last stable block, plus OpenRouter's top-level `cache_control`. Both forms are documented by OpenRouter, with `ttl`. No long-append breakpoint yet |
| Requesty Claude routes (`anthropic/`, `bedrock/`, `vertex/`) | Requesty's documented `"requesty": {"auto_cache": true}` on conversation requests. Requesty places the breakpoints itself, and it has no `ttl`. The system prompt stays one string, so the largest block is the whole prompt |
| MiniMax, OpenCode Anthropic Messages, Atlas Cloud | Nothing, until an opt-in live check shows they accept cache instructions |
| Claude Subscription | Nothing. It is moving to the Claude Agent SDK (D7) |

OpenAI, xAI, Codex, Gemini and custom OpenAI-compatible endpoints never get
Anthropic markers. Codex keeps its `prompt_cache_key` behaviour.

**Safety net.** A 400 that names `cache_control`, `ttl` or `auto_cache` on a
request that carried cache instructions is retried once without them. No other
400 is retried.

- On OpenRouter and Requesty, the route is then remembered for the process,
  so a gateway that rejects them costs one failed request per process.
- On the direct API, which documents every instruction Row-Bot sends, only
  that request is retried.
- A failure placing the system breakpoint is logged once per thread, and the
  request goes out without it.

**Truncation.** An Agent reply that stopped at the output token limit gets the
"cut short" notice. That covers OpenAI-style `finish_reason: "length"`, Ollama
`done_reason: "length"` and Anthropic `stop_reason: "max_tokens"`.

## Verification

`tests/subsystem/agents/test_prompt_prefix_stability.py` drives the real Agent
preparation for eight provider request shapes and asserts that each request is
an exact prefix of the next one (`cache_control` ignored) across iterations and
turns. Cases that still break are strict xfails naming the work package that
fixes them. At DEBUG, the `row_bot.prompt_prefix_diff` logger names the first
section or message that changed between consecutive requests in a thread.

`tests/subsystem/agents/test_turn_context.py` covers the turn context: built
once per turn, persisted through a real graph, diff-only, a full snapshot after
compaction, and recall and skill usage once per turn.
`tests/subsystem/agents/test_turn_context_tag_safety.py` is the security
contract for the tag. `tests/subsystem/client_platform/test_turn_context_readers.py`
checks that no reader of history shows it.

`tests/subsystem/providers/test_prompt_cache_payloads.py` covers the breakpoints
on the wire for each Claude route through the real request builders:

- the lifetime for each surface;
- the TTL ordering and the four-breakpoint limit;
- the long-append breakpoint;
- the safety net;
- the usage ledger recording cache writes, then reads, for consecutive calls.

Live provider validation is opt-in. When run, it uses the real Row-Bot runtime
and at least one latest recommended chat-capable model per configured provider.
The only acceptable provider-side failure is an explicit out-of-credits, quota,
usage-exhausted, or billing-limit response. Any authentication, schema,
unsupported parameter, cache-control, tool-call replay, streaming, routing, or
serialization failure is a bug or blocker.
