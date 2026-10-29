#!/usr/bin/env python3
"""leanaudit: walk an AI agent's token stream the way a Lean engineer walks a factory floor.

Reads Hermes Agent session databases (state.db) read-only and sorts every prompt and output
token into the Lean wastes: overprocessing (the fixed prefix re-sent every call), waiting
(uncached prefill), inventory (tool schemas shipped but rarely used), defects (failed tool
calls, dead runs), motion (identical repeat calls) and inspection (runs that found nothing).

Only aggregates leave this script: no message text, prompts, chat ids or file paths.

    python3 leanaudit.py ~/.hermes/profiles/*/state.db --since 2026-10-20 \\
        --tokenizer tokenizer.json --schemas schemas.json --json out.json --html out.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

__version__ = "0.1.0"

# Session end reasons that mean the run produced nothing usable.
DEAD_END_REASONS = {"cron_incomplete_no_output", "startup_orphan_reap"}
SILENT_MARK = "[SILENT]"
# A gap longer than this between messages is a human or a scheduler, not the agent loop.
MAX_STEP_SECONDS = 1800.0


# ---------------------------------------------------------------- tokens

class Counter_:
    """Token counter: a real tokenizer.json when given, else a chars/3.5 estimate."""

    def __init__(self, tokenizer_path: str | None):
        self.exact = False
        self._tok = None
        if tokenizer_path:
            try:
                from tokenizers import Tokenizer  # optional dependency
            except ImportError:
                sys.exit("--tokenizer needs `pip install tokenizers`")
            self._tok = Tokenizer.from_file(tokenizer_path)
            self.exact = True

    def count_many(self, texts: list[str]) -> list[int]:
        if self._tok is None:
            return [round(len(t) / 3.5) for t in texts]
        return [len(e.ids) for e in self._tok.encode_batch(texts)]


def load_schemas(path: str, counter: Counter_) -> dict[str, int]:
    """Tool name -> schema tokens. Accepts OpenAI-style tool defs or [[name, tokens], ...]."""
    data = json.load(open(path))
    if data and isinstance(data[0], list):
        return {name: int(n) for name, n in data}
    names = [d.get("function", d).get("name", "?") for d in data]
    counts = counter.count_many([json.dumps(d, ensure_ascii=False) for d in data])
    return dict(zip(names, counts))


# ---------------------------------------------------------------- reading

def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def has_column(con, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"pragma table_info({table})"))


def has_table(con, table: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() is not None


def tool_result_failed(content: str | None) -> bool:
    """A tool call failed when the tool says so: success=false or a non-empty error field.

    Non-zero shell exits are NOT counted here (grep exits 1 on no match); they are reported apart.
    """
    if not content:
        return False
    head = content.lstrip()[:1]
    if head != "{":
        return content.lstrip().startswith(("Error", "error:"))
    try:
        d = json.loads(content)
    except ValueError:
        return False
    if not isinstance(d, dict):
        return False
    if d.get("success") is False:
        return True
    err = d.get("error")
    return bool(err) and err is not True


def nonzero_exit(content: str | None) -> bool:
    if not content or '"exit_code"' not in content:
        return False
    try:
        d = json.loads(content)
    except ValueError:
        return False
    return isinstance(d, dict) and isinstance(d.get("exit_code"), int) and d["exit_code"] != 0


def audit_db(path: str, since: float, until: float, counter: Counter_, acc: dict,
             tool_tax: int = 0, whatif: tuple = ()) -> None:
    con = connect(path)
    label = os.path.basename(os.path.dirname(os.path.abspath(path))).lstrip(".") or "default"
    has_hash = has_column(con, "sessions", "system_prompt_hash") and has_table(con, "system_prompts")

    sessions = con.execute(
        "select id, source, model, end_reason, api_call_count, tool_call_count, input_tokens, "
        "cache_read_tokens, output_tokens, reasoning_tokens"
        + (", system_prompt_hash" if has_hash else ", null as system_prompt_hash")
        + " from sessions where started_at >= ? and started_at < ?",
        (since, until),
    ).fetchall()
    if not sessions:
        return
    ids = [s["id"] for s in sessions]
    acc["profiles"][label] += len(sessions)

    # System prompt tokens per unique hash (exact text, counted once).
    hashes = {s["system_prompt_hash"] for s in sessions if s["system_prompt_hash"]}
    sys_tok: dict[str, int] = {}
    if hashes:
        rows = []
        for chunk in chunks(sorted(hashes), 500):
            rows += con.execute(
                f"select hash, prompt from system_prompts where hash in ({','.join('?' * len(chunk))})", chunk
            ).fetchall()
        for (h, _), n in zip(rows, counter.count_many([r["prompt"] for r in rows])):
            sys_tok[h] = n

    by_id = {}
    sess_hash = {s["id"]: s["system_prompt_hash"] for s in sessions}
    for s in sessions:
        prompt = (s["input_tokens"] or 0) + (s["cache_read_tokens"] or 0)
        calls = s["api_call_count"] or 0
        t = acc["totals"]
        t["sessions"] += 1
        t["api_calls"] += calls
        t["tool_calls"] += s["tool_call_count"] or 0
        t["prompt"] += prompt
        t["uncached"] += s["input_tokens"] or 0
        t["cached"] += s["cache_read_tokens"] or 0
        t["output"] += s["output_tokens"] or 0
        t["reasoning"] += s["reasoning_tokens"] or 0
        src = acc["by_source"][s["source"] or "unknown"]
        src["sessions"] += 1
        src["prompt"] += prompt
        src["output"] += s["output_tokens"] or 0
        src["api_calls"] += calls

        m = acc["by_model"][s["model"] or "(unset)"]
        m["sessions"] += 1
        m["prompt"] += prompt
        m["uncached"] += s["input_tokens"] or 0
        m["cached"] += s["cache_read_tokens"] or 0
        # A working prefix cache always hits the system prompt from the second call on, so a
        # multi-call session with zero cache reads ran on an engine that doesn't report hits.
        c = acc["cache_sessions"]
        if (s["cache_read_tokens"] or 0) > 0:
            c["reporting_prompt"] += prompt
            c["reporting_uncached"] += s["input_tokens"] or 0
        else:
            c["unreported_prompt"] += prompt

        h = s["system_prompt_hash"]
        if h in sys_tok and calls:
            acc["fixed"]["system"] += sys_tok[h] * calls
            acc["fixed"]["system_calls"] += calls
            acc["fixed"]["system_sizes"].append(sys_tok[h])
        else:
            acc["fixed"]["unmeasured_calls"] += calls
        if s["end_reason"] in DEAD_END_REASONS:
            acc["outcome"]["dead"] += prompt
            acc["outcome"]["dead_sessions"] += 1
        acc["end_reasons"][s["end_reason"] or "(open)"] += 1
        by_id[s["id"]] = (s["source"], prompt)

    # Aux model work Hermes does around sessions (memory review, titles, vision, compaction).
    if has_table(con, "session_model_usage") and has_column(con, "session_model_usage", "task"):
        for chunk in chunks(ids, 500):
            for r in con.execute(
                "select task, sum(api_call_count) c, sum(input_tokens+cache_read_tokens) p, sum(output_tokens) o "
                f"from session_model_usage where task <> '' and session_id in ({','.join('?' * len(chunk))}) group by task",
                chunk,
            ):
                a = acc["aux"][r["task"]]
                a["calls"] += r["c"] or 0
                a["prompt"] += r["p"] or 0
                a["output"] += r["o"] or 0

    # Messages: tool outcomes, repeats, silent runs, step timing, and the history each call re-reads.
    has_compacted = has_column(con, "messages", "compacted")
    last_assistant: dict[str, str] = {}
    for chunk in chunks(ids, 40):
        q = (
            "select session_id, role, content, tool_calls, tool_name, timestamp"
            + (", compacted" if has_compacted else ", 0 as compacted")
            + f" from messages where session_id in ({','.join('?' * len(chunk))}) order by session_id, id"
        )
        rows = con.execute(q, chunk).fetchall()
        sizes = counter.count_many([(r["content"] or "") + (r["tool_calls"] or "") for r in rows])
        cur_sid, seen, prev_ts, last_asst_ts, batch_max = None, None, None, None, 0.0
        carried = Counter()  # history in context right now, by kind
        pending: list = []  # (tool, tokens, calls before it): carry known once the session ends
        n_calls = 0

        events: list = []  # (role, tokens, chars) for the what-if replay

        def settle():
            for name, ntok, before in pending:
                acc["carry_by_tool"][name] += ntok * (n_calls - before)
            if events and whatif:
                fixed = sys_tok.get(sess_hash.get(cur_sid), 0) + tool_tax
                for trig in whatif:
                    acc["whatif"][trig].update(simulate_prune(events, fixed, trig))

        for r, ntok in zip(rows, sizes):
            sid = r["session_id"]
            if sid != cur_sid:
                settle()
                if batch_max:
                    acc["timing"]["tool"].append(batch_max)
                cur_sid, seen, prev_ts, last_asst_ts, batch_max = sid, Counter(), None, None, 0.0
                carried, pending, n_calls, events = Counter(), [], 0, []
            ts = r["timestamp"] or 0.0
            role = r["role"]
            kind = {"tool": "tool_output", "user": "user", "assistant": "assistant"}.get(role)
            if whatif and kind and not r["compacted"]:
                events.append((role, ntok, len(r["content"] or "")))
            if role == "assistant":
                # One assistant message = one model call that re-read everything carried so far.
                for k, v in carried.items():
                    acc["carry"][k] += v
                acc["carry"]["calls"] += 1
                n_calls += 1
                if batch_max:
                    acc["timing"]["tool"].append(batch_max)
                    batch_max = 0.0
                if prev_ts is not None and 0 <= ts - prev_ts <= MAX_STEP_SECONDS:
                    acc["timing"]["model"].append(ts - prev_ts)
                last_asst_ts = ts
                if r["content"]:
                    last_assistant[sid] = r["content"]
                if r["tool_calls"] and len(r["tool_calls"]) > 4:
                    try:
                        calls = json.loads(r["tool_calls"])
                    except ValueError:
                        calls = []
                    for c in calls if isinstance(calls, list) else []:
                        fn = c.get("function") or {}
                        key = (fn.get("name"), norm_args(fn.get("arguments")))
                        acc["calls_by_tool"][key[0] or "?"] += 1
                        if seen[key]:
                            acc["motion"]["repeats"] += 1
                            acc["motion"]["by_tool"][key[0] or "?"] += 1
                        seen[key] += 1
            elif role == "tool":
                name = r["tool_name"] or "?"
                if not r["compacted"]:
                    pending.append((name, ntok, n_calls))
                acc["results"]["total"] += 1
                if tool_result_failed(r["content"]):
                    acc["results"]["failed"] += 1
                    acc["results"]["failed_by_tool"][name] += 1
                if nonzero_exit(r["content"]):
                    acc["results"]["nonzero_exit"] += 1
                acc["results"]["by_tool"][name] += 1
                if last_asst_ts is not None and 0 <= ts - last_asst_ts <= MAX_STEP_SECONDS:
                    batch_max = max(batch_max, ts - last_asst_ts)
            elif role == "user":
                if batch_max:
                    acc["timing"]["tool"].append(batch_max)
                    batch_max = 0.0
                last_asst_ts = None
            prev_ts = ts
            # Compacted messages left the context at an unknown point: count them nowhere (lower bound).
            if kind and not r["compacted"]:
                carried[kind] += ntok
                if role == "tool":
                    acc["tool_output_tokens"] += ntok
        settle()
        if batch_max:
            acc["timing"]["tool"].append(batch_max)

    for sid, (source, prompt) in by_id.items():
        if source != "cron":
            continue
        acc["outcome"]["cron_runs"] += 1
        text = (last_assistant.get(sid) or "").strip()
        if text.startswith(SILENT_MARK) or text.endswith(SILENT_MARK):
            acc["outcome"]["silent"] += prompt
            acc["outcome"]["silent_runs"] += 1
    con.close()


def simulate_prune(events, fixed: int, trigger: int, protect_n: int = 6, min_chars: int = 8000,
                   min_reclaim: int = 4096, stub_tokens: int = 13) -> Counter:
    """Replay one session under Hermes' proactive tool-result prune (compression.proactive_prune_tokens).

    Mirrors the shipped rules: checked after every tool round; fires when context >= trigger and the
    rearm runway has regrown; summarizes tool results >= min_chars outside the last protect_n
    messages to a one-line stub (Hermes' own summaries median 44 chars ~ 13 tokens); commits only
    when it reclaims >= min_reclaim. Each commit rewrites history, so everything from the first
    pruned message on is recomputed once (the cache break).
    """
    out = Counter()
    entries: list[list] = []  # [tokens_now, chars, is_tool, pruned]
    base_hist = sim_hist = 0
    rearm = 0
    for role, ntok, chars in events:
        if role == "assistant":
            ctx = fixed + sim_hist
            if ctx >= trigger and ctx >= rearm:
                cut = len(entries) - protect_n
                cands = [i for i in range(1, max(cut, 1))
                         if entries[i][2] and not entries[i][3] and entries[i][1] >= min_chars]
                reclaim = sum(entries[i][0] - stub_tokens for i in cands)
                if cands and reclaim >= min_reclaim:
                    for i in cands:
                        entries[i][0], entries[i][3] = stub_tokens, True
                    sim_hist -= reclaim
                    out["recompute"] += sum(e[0] for e in entries[cands[0]:])
                    out["prunes"] += 1
                    rearm = (ctx - reclaim) + max(reclaim, trigger, min_reclaim)
            out["base"] += fixed + base_hist
            out["sim"] += fixed + sim_hist
        if role in ("assistant", "tool", "user"):
            entries.append([ntok, chars, role == "tool", False])
            base_hist += ntok
            sim_hist += ntok
    return out


def norm_args(a) -> str:
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return a
    return json.dumps(a, sort_keys=True, ensure_ascii=False)


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def new_acc() -> dict:
    return {
        "profiles": Counter(),
        "totals": Counter(),
        "by_source": defaultdict(Counter),
        "by_model": defaultdict(Counter),
        "fixed": {"system": 0, "system_calls": 0, "unmeasured_calls": 0, "system_sizes": []},
        "aux": defaultdict(Counter),
        "outcome": Counter(),
        "end_reasons": Counter(),
        "results": {"total": 0, "failed": 0, "nonzero_exit": 0, "by_tool": Counter(), "failed_by_tool": Counter()},
        "motion": {"repeats": 0, "by_tool": Counter()},
        "calls_by_tool": Counter(),
        "timing": {"model": [], "tool": []},
        "carry": Counter(),
        "carry_by_tool": Counter(),
        "tool_output_tokens": 0,
        "whatif": defaultdict(Counter),
        "cache_sessions": Counter(),
    }


# ---------------------------------------------------------------- the waste walk

def pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def summarize(acc: dict, schemas: dict[str, int] | None, args) -> dict:
    t = acc["totals"]
    prompt, out = t["prompt"], t["output"]
    calls = t["api_calls"]
    aux_prompt = sum(a["prompt"] for a in acc["aux"].values())
    all_prompt = prompt + aux_prompt

    # Engines that never report a cache hit can't be judged on misses.
    reporting = {m: v for m, v in acc["by_model"].items() if v["cached"] > 0}
    cs = acc["cache_sessions"]
    unreported_prompt = cs["unreported_prompt"]
    rep_unc, rep_all = cs["reporting_uncached"], cs["reporting_prompt"]

    sys_fixed = acc["fixed"]["system"]
    sizes = acc["fixed"]["system_sizes"]
    tool_tax = sum(schemas.values()) if schemas else None
    tools_fixed = tool_tax * calls if tool_tax else 0

    inventory = []
    if schemas:
        used = acc["calls_by_tool"]
        for name, tok in sorted(schemas.items(), key=lambda kv: -kv[1]):
            uses = used.get(name, 0)
            inventory.append({
                "tool": name,
                "schema_tokens": tok,
                "uses": uses,
                "shipped_tokens": tok * calls,
                "tokens_per_use": round(tok * calls / uses) if uses else None,
            })

    res = acc["results"]
    timing = acc["timing"]
    oc = acc["outcome"]
    delivered = prompt - oc["silent"] - oc["dead"]

    # Rebuild the prompt stream from parts: every call = fixed prefix + all history carried so far.
    carry = acc["carry"]
    history = carry["tool_output"] + carry["assistant"] + carry["user"]
    modeled = sys_fixed + tools_fixed + history
    wip = {
        "history_reread": history,
        "tool_output": carry["tool_output"],
        "assistant": carry["assistant"],
        "user": carry["user"],
        "tool_output_written": acc["tool_output_tokens"],
        "tool_output_reread_x": round(carry["tool_output"] / acc["tool_output_tokens"], 1) if acc["tool_output_tokens"] else None,
        "top_carried_tools": [
            {"tool": k, "tokens": v, "share": pct(v, carry["tool_output"])}
            for k, v in acc["carry_by_tool"].most_common(8)
        ],
        "self_check": {
            "modeled_prompt": modeled,
            "measured_prompt": prompt,
            "modeled_over_measured": round(modeled / prompt, 3) if prompt else None,
        },
    }

    wastes = [
        {
            "key": "overprocessing",
            "lean": "Overprocessing",
            "what": "Fixed prefix re-sent on every model call (system prompt + tool schemas)",
            "tokens": sys_fixed + tools_fixed,
            "share_of_prompt": pct(sys_fixed + tools_fixed, prompt),
            "note": "System prompt measured per session from its exact text"
            + ("; tool schemas measured today and applied to every call in the window." if tool_tax else "; tool schemas not given."),
        },
        {
            "key": "wip",
            "lean": "Inventory (WIP)",
            "what": "Tool output carried in context and re-read by every later call in the session",
            "tokens": carry["tool_output"],
            "share_of_prompt": pct(carry["tool_output"], prompt),
            "note": f"Each tool-output token was re-read {wip['tool_output_reread_x']}× on average. Reasoning text is not counted.",
        },
        {
            "key": "waiting",
            "lean": "Waiting",
            "what": "Prompt tokens the engine had to compute (cache miss) on engines that report cache",
            "tokens": rep_unc,
            "share_of_prompt": pct(rep_unc, rep_all),
            "note": f"{pct(unreported_prompt, prompt)}% of prompt tokens ran in sessions with zero reported cache reads "
                    "(engine doesn't report hits); left out, not called waste.",
        },
        {
            "key": "inspection",
            "lean": "Inspection",
            "what": "Scheduled runs that checked and found nothing to say ([SILENT]) + after-session reviews",
            "tokens": oc["silent"] + acc["aux"].get("background_review", Counter())["prompt"],
            "share_of_prompt": pct(oc["silent"] + acc["aux"].get("background_review", Counter())["prompt"], all_prompt),
            "note": "Necessary non-value-added: the Lean move is cheaper inspection, not no inspection.",
        },
        {
            "key": "defects",
            "lean": "Defects",
            "what": "Tool calls the tool itself reported as failed, and runs that died with no output",
            "tokens": oc["dead"],
            "count": res["failed"],
            "rate": pct(res["failed"], res["total"]),
            "share_of_prompt": pct(oc["dead"], prompt),
            "note": f"{res['nonzero_exit']:,} non-zero shell exits are excluded (grep exits 1 on no match).",
        },
        {
            "key": "motion",
            "lean": "Motion",
            "what": "Tool calls identical (same tool, same arguments) to an earlier call in the same session",
            "count": acc["motion"]["repeats"],
            "rate": pct(acc["motion"]["repeats"], sum(acc["calls_by_tool"].values())),
            "note": "Some repeats are legitimate (re-running a test after a fix); this is the ceiling, not the floor.",
        },
    ]
    if schemas:
        idle = [i for i in inventory if i["uses"] == 0]
        wastes.append({
            "key": "inventory",
            "lean": "Inventory (idle stock)",
            "what": "Tool schemas shipped on every call but never called in the window",
            "tokens": sum(i["shipped_tokens"] for i in idle),
            "count": len(idle),
            "share_of_prompt": pct(sum(i["shipped_tokens"] for i in idle), prompt),
            "note": "See the inventory table: tokens shipped per actual use, per tool.",
        })

    return {
        "tool": f"leanaudit {__version__}",
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": args.label,
        "window": {"since": args.since, "until": args.until},
        "tokenizer": "exact" if args.tokenizer else "estimate (chars/3.5)",
        "profiles": dict(acc["profiles"]),
        "totals": {**t, "aux_prompt": aux_prompt},
        "headline": {
            "read_per_write": round(all_prompt / out) if out else None,
            "value_ratio_pct": round(100.0 * out / (all_prompt + out), 2) if out else None,
            "prompt_per_call": round(prompt / calls) if calls else None,
            "system_prompt_tokens_median": round(statistics.median(sizes)) if sizes else None,
            "tool_schema_tokens": tool_tax,
            "tool_count": len(schemas) if schemas else None,
        },
        "wastes": wastes,
        "outcome_split": {
            "delivered_or_chat": delivered,
            "silent_inspection": oc["silent"],
            "dead_runs": oc["dead"],
            "aux": aux_prompt,
            "cron_runs": oc["cron_runs"],
            "silent_runs": oc["silent_runs"],
            "dead_sessions": oc["dead_sessions"],
        },
        "prefix_split": {
            "system": sys_fixed,
            "tools": tools_fixed,
            "conversation": max(0, prompt - sys_fixed - tools_fixed),
        },
        "wip": wip,
        "cache": {
            "reporting_models": sorted({os.path.basename(m) for m in reporting}),
            "miss_pct_reporting": pct(rep_unc, rep_all),
            "unreported_prompt": unreported_prompt,
        },
        "aux": {k: dict(v) for k, v in sorted(acc["aux"].items(), key=lambda kv: -kv[1]["prompt"])},
        "by_source": {k: dict(v) for k, v in sorted(acc["by_source"].items(), key=lambda kv: -kv[1]["prompt"])},
        "tool_results": {
            "total": res["total"],
            "failed": res["failed"],
            "failed_pct": pct(res["failed"], res["total"]),
            "nonzero_exit": res["nonzero_exit"],
            "top_failed": [
                {"tool": k, "failed": v, "of": res["by_tool"][k], "pct": pct(v, res["by_tool"][k])}
                for k, v in res["failed_by_tool"].most_common(8)
            ],
        },
        "motion": {"repeats": acc["motion"]["repeats"], "top": acc["motion"]["by_tool"].most_common(6)},
        "inventory": inventory,
        "lead_time": {
            "model_step_median_s": med(timing["model"]),
            "tool_step_median_s": med(timing["tool"]),
            "model_total_h": round(sum(timing["model"]) / 3600, 1),
            "tool_total_h": round(sum(timing["tool"]) / 3600, 1),
            "model_steps": len(timing["model"]),
            "tool_steps": len(timing["tool"]),
        },
        "end_reasons": dict(acc["end_reasons"].most_common()),
        "whatif_prune": [
            {
                "trigger_tokens": trig,
                "prompt_now": w["base"],
                "prompt_with_prune": w["sim"],
                "saved": w["base"] - w["sim"],
                "saved_pct": pct(w["base"] - w["sim"], w["base"]),
                "prunes": w["prunes"],
                "recompute": w["recompute"],
                "net_saved": w["base"] - w["sim"] - w["recompute"],
            }
            for trig, w in sorted(acc["whatif"].items())
        ],
    }


def med(xs):
    return round(statistics.median(xs), 1) if xs else None


# ---------------------------------------------------------------- output

def fmt_tok(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def print_text(r: dict) -> None:
    h, t = r["headline"], r["totals"]
    print(f"leanaudit · {r['label']} · {r['window']['since']} → {r['window']['until'] or 'now'} · tokens {r['tokenizer']}")
    print(f"  {t['sessions']:,} sessions · {t['api_calls']:,} model calls · {t['tool_calls']:,} tool calls")
    print(f"  read {fmt_tok(t['prompt'] + t['aux_prompt'])} · wrote {fmt_tok(t['output'])} · "
          f"{h['read_per_write']}:1 read per write · {h['prompt_per_call']:,} prompt tokens per call")
    for w in r["wastes"]:
        bits = []
        if "tokens" in w:
            bits.append(f"{fmt_tok(w['tokens'])} tok")
        if "share_of_prompt" in w:
            bits.append(f"{w['share_of_prompt']}% of prompt")
        if "count" in w:
            bits.append(f"{w['count']:,} events")
        if "rate" in w:
            bits.append(f"{w['rate']}%")
        print(f"  {w['lean']:<15} {' · '.join(bits):<45} {w['what']}")
    sc = r["wip"]["self_check"]
    print(f"  self-check: fixed prefix + carried history = {fmt_tok(sc['modeled_prompt'])} vs Hermes-measured "
          f"{fmt_tok(sc['measured_prompt'])} ({sc['modeled_over_measured']}×)")
    for w in r["whatif_prune"]:
        print(f"  what-if prune @ {w['trigger_tokens']:,}: read {fmt_tok(w['prompt_with_prune'])} instead of "
              f"{fmt_tok(w['prompt_now'])} (−{w['saved_pct']}%), {w['prunes']:,} cache breaks costing "
              f"{fmt_tok(w['recompute'])} recompute")
    lt = r["lead_time"]
    print(f"  lead time: model step median {lt['model_step_median_s']} s, tool step median {lt['tool_step_median_s']} s "
          f"({lt['model_total_h']} h model vs {lt['tool_total_h']} h tools)")


def write_html(r: dict, path: str) -> None:
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")).read()
    data = json.dumps(r).replace("</", "<\\/")
    out = tpl.replace("__REPORT_JSON__", data).replace("__TITLE__", html.escape(r["label"]))
    with open(path, "w") as f:
        f.write(out)


def parse_day(s: str | None, default: float) -> float:
    if not s:
        return default
    return dt.datetime.fromisoformat(s).astimezone().timestamp()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dbs", nargs="+", help="Hermes state.db files (opened read-only)")
    p.add_argument("--since", help="YYYY-MM-DD (local time), inclusive")
    p.add_argument("--until", help="YYYY-MM-DD (local time), exclusive")
    p.add_argument("--tokenizer", help="tokenizer.json for exact counts (else chars/3.5)")
    p.add_argument("--schemas", help="tool schemas JSON: OpenAI tool defs or [[name, tokens], ...]")
    p.add_argument("--whatif-prune", type=int, nargs="*", default=[], metavar="TOKENS",
                   help="replay sessions with Hermes' proactive tool-result prune at these trigger sizes")
    p.add_argument("--label", default="Hermes Agent")
    p.add_argument("--json", help="write the report as JSON")
    p.add_argument("--html", help="write a self-contained HTML report")
    args = p.parse_args(argv)

    counter = Counter_(args.tokenizer)
    schemas = load_schemas(args.schemas, counter) if args.schemas else None
    since, until = parse_day(args.since, 0.0), parse_day(args.until, 1e12)
    acc = new_acc()
    tool_tax = sum(schemas.values()) if schemas else 0
    for db in args.dbs:
        audit_db(db, since, until, counter, acc, tool_tax, tuple(args.whatif_prune))
    if not acc["totals"]["sessions"]:
        print("no sessions in window", file=sys.stderr)
        return 1
    report = summarize(acc, schemas, args)
    print_text(report)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
        print(f"  json → {args.json}")
    if args.html:
        write_html(report, args.html)
        print(f"  html → {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
