"""Tests on a synthetic Hermes state.db whose answers are worked out by hand.

    python3 -m unittest discover -s tests
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import leanaudit as la  # noqa: E402

T0 = 1_790_000_000.0  # a fixed epoch inside the window


def build_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
    create table sessions (id text primary key, source text, model text, end_reason text, started_at real,
        api_call_count int, tool_call_count int, input_tokens int, cache_read_tokens int, output_tokens int,
        reasoning_tokens int, system_prompt_hash text);
    create table system_prompts (hash text primary key, prompt text);
    create table messages (id integer primary key autoincrement, session_id text, role text, content text,
        tool_calls text, tool_name text, timestamp real, compacted int default 0);
    create table session_model_usage (session_id text, model text, task text, api_call_count int,
        input_tokens int, output_tokens int, cache_read_tokens int);
    """)
    # chars/3.5 counter: 35 chars = 10 tokens, 70 chars = 20 tokens
    con.execute("insert into system_prompts values ('h1', ?)", ("s" * 35,))
    # s1: a chat with 3 model calls. user(10) -> asst call 1 -> tool(20) -> asst call 2 -> tool(20, failed) -> asst call 3
    con.execute("insert into sessions values ('s1','telegram','m-cache',null,?,3,2,100,900,30,0,'h1')", (T0,))
    call = lambda args: json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(args)}}])
    rows = [
        ("s1", "user", "u" * 35, None, None, T0),
        ("s1", "assistant", "", call({"command": "ls"}), None, T0 + 5),
        ("s1", "tool", json.dumps({"output": "x" * 50, "exit_code": 0, "error": None})[:70].ljust(70), None, "terminal", T0 + 6),
        ("s1", "assistant", "", call({"command": "ls"}), None, T0 + 11),
        ("s1", "tool", json.dumps({"success": False, "error": "boom"}).ljust(70), None, "terminal", T0 + 12),
        ("s1", "assistant", "done", None, None, T0 + 20),
    ]
    con.executemany("insert into messages (session_id, role, content, tool_calls, tool_name, timestamp) values (?,?,?,?,?,?)", rows)
    # s2: a cron run that said nothing, on an engine that never reports cache (2 calls, 0 cache reads)
    con.execute("insert into sessions values ('s2','cron','m-nocache','cron_complete',?,2,0,500,0,5,0,'h1')", (T0 + 100,))
    con.executemany("insert into messages (session_id, role, content, tool_calls, tool_name, timestamp) values (?,?,?,?,?,?)", [
        ("s2", "user", "check", None, None, T0 + 100),
        ("s2", "assistant", "[SILENT]", None, None, T0 + 103),
    ])
    # s3: a run that died, outside nothing special
    con.execute("insert into sessions values ('s3','cron','m-cache','cron_incomplete_no_output',?,1,0,40,60,0,0,null)", (T0 + 200,))
    con.execute("insert into session_model_usage values ('s1','m-cache','background_review',1,70,3,0)")
    con.commit()
    con.close()


def args(**kw):
    base = dict(label="test", since=None, until=None, tokenizer=None, whatif_prune=[])
    base.update(kw)
    return argparse.Namespace(**base)


class AuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "state.db")
        build_db(cls.db)
        before = open(cls.db, "rb").read()
        acc = la.new_acc()
        la.audit_db(cls.db, 0, 1e12, la.Counter_(None), acc, tool_tax=0, whatif=(1,))
        cls.r = la.summarize(acc, None, args())
        cls.unchanged = open(cls.db, "rb").read() == before
        cls.w = {w["key"]: w for w in cls.r["wastes"]}

    def test_read_only(self):
        self.assertTrue(self.unchanged)

    def test_totals(self):
        t = self.r["totals"]
        self.assertEqual(t["sessions"], 3)
        self.assertEqual(t["prompt"], 1000 + 500 + 100)
        self.assertEqual(t["aux_prompt"], 70)
        self.assertEqual(self.r["headline"]["read_per_write"], round(1670 / 35))

    def test_fixed_prefix(self):
        # 10-token system prompt x (3 + 2) calls; s3 has no hash and is unmeasured
        self.assertEqual(self.r["prefix_split"]["system"], 50)

    def test_carried_history(self):
        # s1 call 1 re-reads user(10); call 2 user+tool(10+20); call 3 user+tool+tool(10+20+20)
        # assistant msgs: call 1 msg = tool_calls json, re-read by calls 2 and 3
        x = self.r["wip"]
        self.assertEqual(x["tool_output"], 20 + 40)
        self.assertEqual(x["tool_output_written"], 40)
        self.assertEqual(x["tool_output_reread_x"], 1.5)
        self.assertEqual(x["user"], 10 * 3 + 1)  # s1 three calls + s2 "check" (5 chars -> 1 token) once

    def test_failures_and_motion(self):
        res = self.r["tool_results"]
        self.assertEqual((res["total"], res["failed"]), (2, 1))
        self.assertEqual(self.r["motion"]["repeats"], 1)  # second identical `ls`

    def test_inspection_and_dead(self):
        oc = self.r["outcome_split"]
        self.assertEqual((oc["silent_runs"], oc["silent_inspection"]), (1, 500))
        self.assertEqual((oc["dead_sessions"], oc["dead_runs"]), (1, 100))
        self.assertEqual(self.w["inspection"]["tokens"], 500 + 70)

    def test_cache_rule(self):
        # s2 never reported a cache read: excluded, not counted as a miss
        c = self.r["cache"]
        self.assertEqual(c["unreported_prompt"], 500)
        self.assertEqual(self.w["waiting"]["share_of_prompt"], la.pct(140, 1100))

    def test_lead_time(self):
        lt = self.r["lead_time"]
        self.assertEqual(lt["model_steps"], 4)  # 3 in s1 + 1 in s2, including the first step after a user message
        self.assertEqual(lt["tool_step_median_s"], 1.0)


class PruneTest(unittest.TestCase):
    def test_prune_fires_and_recomputes(self):
        # user, asst, tool(1000 tok, 9000 chars), asst, user, asst, user, asst ... protect last 2
        ev = [("user", 10, 40), ("assistant", 5, 20), ("tool", 1000, 9000), ("assistant", 5, 20),
              ("user", 10, 40), ("assistant", 5, 20)]
        out = la.simulate_prune(ev, fixed=100, trigger=500, protect_n=2, min_reclaim=100, stub_tokens=13)
        self.assertEqual(out["prunes"], 1)
        # calls: #1 sees 10; #2 sees 1015; #3 would see 1030, pruned to 43
        self.assertEqual(out["base"], 3 * 100 + 10 + 1015 + 1030)
        self.assertEqual(out["sim"], 3 * 100 + 10 + 1015 + 43)
        self.assertEqual(out["recompute"], 13 + 5 + 10)

    def test_small_outputs_never_pruned(self):
        ev = [("user", 10, 40), ("assistant", 5, 20), ("tool", 1000, 500), ("assistant", 5, 20),
              ("user", 10, 40), ("assistant", 5, 20)]
        out = la.simulate_prune(ev, fixed=100, trigger=500, protect_n=2, min_reclaim=100)
        self.assertEqual(out["prunes"], 0)
        self.assertEqual(out["base"], out["sim"])


class ResultParseTest(unittest.TestCase):
    def test_failure_detection(self):
        f = la.tool_result_failed
        self.assertTrue(f('{"success": false, "error": "x"}'))
        self.assertTrue(f('{"output": "", "error": "Timeout"}'))
        self.assertFalse(f('{"output": "ok", "exit_code": 1, "error": null}'))  # grep no-match is not a defect
        self.assertFalse(f('{"success": true, "diff": "..."}'))
        self.assertTrue(f("Error: file not found"))
        self.assertTrue(la.nonzero_exit('{"output": "", "exit_code": 1, "error": null}'))


if __name__ == "__main__":
    unittest.main()
