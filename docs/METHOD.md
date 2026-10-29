# Method

How each `leanaudit` number is built, from the tables Hermes Agent already keeps in `state.db`.

## Sources

| Table | Used for |
|---|---|
| `sessions` | Source (telegram, cron, oneshot…), model, end reason, model calls, tool calls, tokens: `input_tokens` (computed), `cache_read_tokens`, `output_tokens`, `reasoning_tokens`, `system_prompt_hash` |
| `system_prompts` | The exact system prompt text for each hash |
| `messages` | Role, content, tool calls (name + arguments), tool name, timestamp, `compacted` |
| `session_model_usage` | Side work Hermes does around a session, by `task`: `background_review`, `vision`, `title_generation`, `compression`, `approval` |

**Prompt tokens** = `input_tokens + cache_read_tokens`. Hermes stores computed and cache-hit tokens separately; an
engine's own prompt counter usually includes both. Compare like with like.

## Rebuilding the stream

For each session, in message order:

- every **assistant** message is one model call. It re-read the fixed prefix plus everything carried so far.
- **user**, **assistant** (content + tool-call JSON) and **tool** messages are tokenized and added to what's carried.
- **compacted** messages left the context at an unknown point, so they are not carried at all (a lower bound).

Sum over all calls: fixed prefix × calls + Σ carried history at each call. Then compare with Hermes' measured prompt
tokens. On this house the rebuild covers 0.86–0.97 of the measured total by month. The remainder (chat-template
tokens, reasoning replay, memory and skill context injected per turn) is reported as "not rebuilt" and never
assigned to a waste.

**Re-read multiplier** = tool-output tokens carried across all later calls ÷ tool-output tokens written.

## Each waste

**Overprocessing.** `Σ system_prompt_tokens[hash] × api_call_count` over sessions with a stored hash, plus
`tool_schema_tokens × api_call_count` when `--schemas` is given. Sessions without a stored prompt add nothing.

**Inventory (WIP).** Tool output carried, from the rebuild above. By tool: each result's tokens × the number of
model calls after it in its session.

**Waiting.** `Σ input_tokens ÷ Σ prompt tokens`, over sessions with `cache_read_tokens > 0`. Sessions with zero
cache reads are excluded, because on a working prefix cache the second call always hits the system prompt; zero
means the engine doesn't report hits. The share excluded is printed beside the number.

**Inspection.** Prompt tokens of cron sessions whose last assistant message starts or ends with `[SILENT]` (Hermes'
convention for "nothing to report"), plus `background_review` prompt tokens. Necessary non-value-added work: the
Lean answer is cheaper inspection, not none.

**Defects.** A tool result failed when the tool says so: top-level `success: false`, a non-empty `error`, or text
starting with `Error`. Non-zero shell exit codes are counted apart and not called defects, because `grep` exits 1
when nothing matches. Dead runs: sessions ended `cron_incomplete_no_output` or `startup_orphan_reap`.

**Motion.** A tool call whose name and JSON-normalized arguments match an earlier call in the same session. Some
repeats are legitimate (running a test again after a fix), so this is a ceiling.

**Inventory (idle stock).** With `--schemas`: per tool, schema tokens × model calls in the window ÷ times the tool
was called. Tools called zero times are idle stock.

**Lead time.** Model step = time from the previous message to an assistant message (gaps over 30 min dropped as
human or scheduler time). Tool step = the longest gap from an assistant message to one of its tool results.

## The prune replay

`--whatif-prune N` replays each session under Hermes' `compression.proactive_prune_tokens = N`, mirroring
`agent/context_compressor_prune.py` as of hermes-agent `bf0203a794`:

1. checked before every model call (Hermes checks after each tool round);
2. fires when fixed prefix + carried ≥ N and the rearm runway has regrown;
3. candidates: tool results ≥ 8,000 characters, outside the first message and the last 6;
4. each becomes a 13-token stub (Hermes' own summaries: median 44 characters over 400 real outputs);
5. commits only if it reclaims ≥ 4,096 tokens, then rearms at `after + max(reclaimed, N, 4096)`;
6. a commit rewrites history from the first pruned message, so those tokens are counted once as recompute.

Not modeled: Hermes' lossless dedup of byte-identical results and trimming of old tool-call arguments (both would
save more), and the agent re-reading something it lost (which would save less). The live trial measures the last one.
