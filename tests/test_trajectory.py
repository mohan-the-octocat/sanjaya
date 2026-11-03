"""Unit tests for trajectory parsing."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import fixtures  # noqa: F401  (sets sys.path)
from fixtures import CHILD_ID, PARENT_ID, make_app_dir, parent_steps
import trajectory as T


class HelpersTest(unittest.TestCase):

  def test_parse_time(self):
    self.assertEqual(T.parse_time("1970-01-01T00:00:10Z"), 10.0)
    self.assertIsNone(T.parse_time("not a time"))
    self.assertIsNone(T.parse_time(None))

  def test_decode_args_unwraps_json_strings(self):
    out = T.decode_args({"a": '"x"', "b": "5", "c": "plain text", "d": 3})
    self.assertEqual(out, {"a": "x", "b": 5, "c": "plain text", "d": 3})

  def test_tool_category(self):
    self.assertEqual(T.tool_category("read_file"), "explore")
    self.assertEqual(T.tool_category("edit_file"), "edit")
    self.assertEqual(T.tool_category("run_command"), "execute")
    self.assertEqual(T.tool_category("invoke_subagent"), "delegate")
    self.assertEqual(T.tool_category("search_web"), "research")
    self.assertEqual(T.tool_category("mcp_foo_bar"), "integrate")
    self.assertEqual(T.tool_category("something_new"), "other")

  def test_tool_category_write_to_file(self):
    self.assertEqual(T.tool_category("write_to_file"), "edit")

  def test_parse_message(self):
    msg_text = (
        "[Message] timestamp=2026-01-01T00:00:00Z sender=abc priority=X "
        "content=PLUGIN_ROOT=/x\n\nRole: worker. Do the thing"
    )
    parsed_msg = T.parse_message(msg_text)
    self.assertIsNotNone(parsed_msg)
    self.assertEqual(parsed_msg[0], "abc")
    self.assertEqual(
        parsed_msg[1], "PLUGIN_ROOT=/x\n\nRole: worker. Do the thing"
    )
    self.assertIsNone(T.parse_message("not a message"))
    self.assertIsNone(T.parse_message(""))
    self.assertIsNone(T.parse_message(None))

  def test_tool_summary_prefers_tool_summary_then_basename(self):
    self.assertEqual(T.tool_summary("run_command", {"toolSummary": "Run tests", "CommandLine": "x"}), "Run tests")
    self.assertEqual(T.tool_summary("read_file", {"Path": "/a/b/c.py"}), "c.py")
    self.assertEqual(T.tool_summary("noop", {}), "noop")

  def test_conversation_id_validation_blocks_traversal(self):
    self.assertTrue(T.is_valid_conversation_id(PARENT_ID))
    for bad in ("", "../etc", "a/b", ".hidden", "x" * 200, None):
      self.assertFalse(T.is_valid_conversation_id(bad), bad)

  def test_default_app_dirs_derives_from_sidecar_data(self):
    env = {"HOME": "/home/u",
           "ANTIGRAVITY_EXECUTABLE_DATA_DIR": "/x/appdata/sidecar_data/sanjaya/sanjaya/data",
           "SANJAYA_APP_DIRS": "/custom"}
    dirs = [str(d) for d in T.default_app_dirs(env)]
    self.assertEqual(dirs[0], "/custom")
    self.assertEqual(dirs[1], "/x/appdata")
    self.assertIn("/home/u/.gemini/antigravity", dirs)
    self.assertIn("/home/u/.gemini/jetski", dirs)


class BuildTrajectoryTest(unittest.TestCase):

  def setUp(self):
    self.model = T.build_trajectory(PARENT_ID, parent_steps())
    self.by_i = {s["i"]: s for s in self.model["steps"]}

  def test_steps_are_sorted_by_index(self):
    self.assertEqual([s["i"] for s in self.model["steps"]], list(range(12)))

  def test_tool_results_pair_with_calls_in_order(self):
    calls = self.by_i[2]["tool_calls"]
    self.assertEqual(calls[0]["name"], "read_file")
    self.assertEqual(calls[0]["result_step"], 3)
    self.assertEqual(calls[0]["status"], "done")
    self.assertEqual(calls[1]["result_step"], 4)
    self.assertEqual(calls[1]["status"], "error")
    self.assertEqual(self.by_i[4]["tool_name"], "run_command")
    self.assertEqual(self.by_i[4]["kind"], "tool")

  def test_ephemeral_messages_do_not_consume_results(self):
    self.assertEqual(self.by_i[1]["kind"], "system")
    self.assertNotIn("result_of", self.by_i[1])

  def test_tool_duration_from_created_completed_header(self):
    self.assertEqual(self.by_i[2]["tool_calls"][1]["duration_s"], 12.0)
    self.assertEqual(self.by_i[3]["duration_s"], 1.0)

  def test_last_call_without_result_is_pending(self):
    self.assertEqual(self.by_i[11]["tool_calls"][0]["status"], "pending")

  def test_stats(self):
    st = self.model["stats"]
    self.assertEqual(st["steps"], 12)
    self.assertEqual(st["turns"], 2)
    self.assertEqual(st["model_steps"], 4)
    self.assertEqual(st["tool_calls"], 5)
    self.assertEqual(st["errors"], 1)
    self.assertEqual(st["compactions"], 1)
    self.assertEqual(st["subagents"], 1)
    self.assertEqual(st["input_tokens"], 1800)
    self.assertEqual(st["cache_read_tokens"], 16000)
    self.assertEqual(st["output_tokens"], 450)
    self.assertEqual(st["duration_s"], 3604.0)

  def test_turns(self):
    turns = self.model["turns"]
    self.assertEqual(turns[0]["request"], "Fix the flaky build\nplease")
    self.assertEqual(turns[0]["tool_calls"], 4)
    self.assertEqual(turns[0]["errors"], 1)
    self.assertEqual(turns[1]["steps"], [10, 11])
    self.assertEqual(self.model["title"], "Fix the flaky build")

  def test_subagent_detection(self):
    sub = self.model["subagents"][0]
    self.assertEqual(sub["id"], CHILD_ID)
    self.assertEqual(sub["role"], "Build Debugger")
    self.assertEqual(sub["result_step"], 6)
    self.assertEqual(self.by_i[6]["children"], [CHILD_ID])
    self.assertFalse(sub["exists"])  # no app dirs supplied

  def test_tool_stats(self):
    tools = {t["name"]: t for t in self.model["tools"]}
    self.assertEqual(tools["run_command"]["count"], 2)
    self.assertEqual(tools["run_command"]["errors"], 1)
    self.assertEqual(tools["run_command"]["avg_s"], 12.0)
    self.assertEqual(self.model["tools"][0]["name"], "run_command")

  def test_phases_group_by_category_and_break_on_turns(self):
    cats = [p["category"] for p in self.model["phases"]]
    self.assertEqual(cats, ["request", "explore", "delegate", "compaction",
                            "edit", "request", "execute"])
    explore = self.model["phases"][1]
    self.assertEqual(explore["start"], 2)
    self.assertEqual(explore["end"], 4)
    self.assertEqual(explore["errors"], 1)

  def test_subagent_transcript_first_message(self):
    msg_content = (
        "[Message] timestamp=2026-01-01T00:00:00Z sender=abc priority=X "
        "content=PLUGIN_ROOT=/x\n\nRole: worker. Do the thing"
    )
    raw_steps = [
        {"step_index": 0, "source": "SYSTEM_SDK", "type": "SYSTEM_MESSAGE",
         "status": "DONE", "created_at": "2026-01-01T00:00:00Z",
         "content": msg_content},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "status": "DONE", "created_at": "2026-01-01T00:00:05Z",
         "tool_calls": []},
    ]
    subagent_model = T.build_trajectory("subagent-conv-1", raw_steps)
    self.assertEqual(len(subagent_model["turns"]), 1)
    self.assertTrue(
        subagent_model["turns"][0]["request"].startswith("Role: worker")
    )
    self.assertEqual(subagent_model["steps"][0]["kind"], "message")
    self.assertEqual(subagent_model["steps"][0]["sender"], "abc")

    with tempfile.TemporaryDirectory() as temp_dir:
      test_app_dir = pathlib.Path(temp_dir) / "app"
      fixtures.write_conversation(test_app_dir, "subagent-conv-1", raw_steps)
      conversation_list = T.list_conversations([test_app_dir])
      self.assertEqual(len(conversation_list), 1)
      self.assertEqual(
          conversation_list[0]["title"], "Role: worker. Do the thing"
      )

  def test_mid_conversation_message_does_not_open_turn_and_starts_message_phase(self):
    msg_content = (
        "[Message] timestamp=2026-01-01T00:00:02Z sender=abc priority=X "
        "content=Steer update"
    )
    raw_steps = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "status": "DONE", "created_at": "2026-01-01T00:00:00Z",
         "content": "<USER_REQUEST>Initial prompt</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "status": "DONE", "created_at": "2026-01-01T00:00:01Z",
         "tool_calls": []},
        {"step_index": 2, "source": "SYSTEM_SDK", "type": "SYSTEM_MESSAGE",
         "status": "DONE", "created_at": "2026-01-01T00:00:02Z",
         "content": msg_content},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "status": "DONE", "created_at": "2026-01-01T00:00:03Z",
         "tool_calls": []},
    ]
    steer_model = T.build_trajectory("conv-with-mid-message", raw_steps)
    self.assertEqual(len(steer_model["turns"]), 1)
    self.assertIn(2, steer_model["turns"][0]["steps"])
    phase_categories = [phase["category"] for phase in steer_model["phases"]]
    self.assertIn("message", phase_categories)
    msg_phase = next(
        phase for phase in steer_model["phases"] if phase["category"] == "message"
    )
    self.assertEqual(msg_phase["start"], 2)
    self.assertEqual(msg_phase["end"], 2)

  def test_empty_transcript(self):
    m = T.build_trajectory("x", [])
    self.assertEqual(m["stats"]["steps"], 0)
    self.assertEqual(m["phases"], [])
    self.assertEqual(m["title"], "x")


class StoreTest(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.app = make_app_dir(pathlib.Path(self.tmp.name))
    self.store = T.TrajectoryStore([self.app])

  def tearDown(self):
    self.tmp.cleanup()

  def test_list_conversations_newest_first_with_titles(self):
    convs = T.list_conversations([self.app])
    self.assertEqual([c["id"] for c in convs], [PARENT_ID, CHILD_ID])
    self.assertEqual(convs[0]["title"], "Fix the flaky build")
    self.assertEqual(convs[1]["title"], "Find the flake")

  def test_load_is_cached_until_file_changes(self):
    a = self.store.load(PARENT_ID)
    b = self.store.load(PARENT_ID)
    self.assertIs(a, b)
    path = self.app / "brain" / PARENT_ID / T.TRANSCRIPT_REL
    with open(path, "a", encoding="utf-8") as f:
      f.write('{"step_index": 12, "type": "PLANNER_RESPONSE", "status": "DONE"}\n')
    c = self.store.load(PARENT_ID)
    self.assertIsNot(a, c)
    self.assertEqual(c["stats"]["steps"], 13)

  def test_subagent_exists_when_transcript_present(self):
    model = self.store.load(PARENT_ID)
    self.assertTrue(model["subagents"][0]["exists"])
    self.assertEqual(model["subagents"][0]["title"], "Find the flake")

  def test_step_detail_reads_full_transcript(self):
    d = self.store.step_detail(PARENT_ID, 3)
    self.assertEqual(d["source_file"], "transcript_full.jsonl")
    self.assertIn("cc_library", d["content"])
    d2 = self.store.step_detail(PARENT_ID, 2)
    self.assertEqual(d2["tool_calls"][1]["args"]["CommandLine"], "make test")
    self.assertIsNone(self.store.step_detail(PARENT_ID, 999))

  def test_subagent_tree(self):
    tree = self.store.subagent_tree(PARENT_ID)
    self.assertEqual(tree["id"], PARENT_ID)
    self.assertEqual(len(tree["children"]), 1)
    self.assertEqual(tree["children"][0]["id"], CHILD_ID)
    self.assertEqual(tree["children"][0]["stats"]["tool_calls"], 1)

  def test_unknown_conversation(self):
    self.assertIsNone(self.store.load("33333333-3333-4333-8333-333333333333"))
    self.assertIsNone(self.store.load("../../etc"))


class ExportTest(unittest.TestCase):

  def test_markdown_report(self):
    model = T.build_trajectory(PARENT_ID, parent_steps())
    md = T.export_markdown(model, {"_": [{"note": "overall"}], "4": [{"note": "flaky here"}]})
    self.assertIn("# Trajectory: Fix the flaky build", md)
    self.assertIn("| `run_command` | 2 | 1 |", md)
    self.assertIn("overall", md)
    self.assertIn("📝 flaky here", md)
    self.assertIn(CHILD_ID, md)

  def test_fork_prompt_only_includes_history_up_to_step(self):
    model = T.build_trajectory(PARENT_ID, parent_steps())
    prompt = T.fork_prompt(model, 6, "Try bazel instead")
    self.assertIn("fork of conversation " + PARENT_ID + " at step 6", prompt)
    self.assertIn("1. Fix the flaky build", prompt)
    self.assertNotIn("now push it", prompt)
    self.assertIn("run_command — Run tests (failed)", prompt)
    self.assertIn("invoke_subagent", prompt)
    self.assertNotIn("edit_file", prompt)
    self.assertIn("Try bazel instead", prompt)


if __name__ == "__main__":
  unittest.main()
