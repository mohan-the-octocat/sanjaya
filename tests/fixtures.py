"""Shared synthetic fixtures for Sanjaya tests (no real transcript data)."""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "sidecars" / "sanjaya"
if str(SIDECAR) not in sys.path:
  sys.path.insert(0, str(SIDECAR))

PARENT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"


def _args(**kw):
  # Real transcripts JSON-encode each argument value.
  return {k: json.dumps(v) for k, v in kw.items()}


def parent_steps():
  return [
      {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
       "status": "DONE", "created_at": "2026-01-01T00:00:00Z",
       "content": "<USER_REQUEST>\nFix the flaky build\nplease\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"},
      {"step_index": 1, "source": "SYSTEM_SDK", "type": "EPHEMERAL_MESSAGE",
       "status": "DONE", "created_at": "2026-01-01T00:00:01Z", "content": "<memory>"},
      {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
       "status": "DONE", "created_at": "2026-01-01T00:00:05Z",
       "input_tokens": 1000, "cache_read_tokens": 4000, "output_tokens": 50,
       "thinking": "Look at the build file first.",
       "tool_calls": [
           {"name": "read_file", "args": _args(Path="/repo/BUILD")},
           {"name": "run_command", "args": _args(CommandLine="make test", toolSummary="Run tests")},
       ]},
      # Out-of-order line: the result for call 2.1 is flushed before 2.0's.
      {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "ERROR",
       "created_at": "2026-01-01T00:00:20Z", "error": "exit 2",
       "content": "Created At: 2026-01-01T00:00:08Z\nCompleted At: 2026-01-01T00:00:20Z\nfailed"},
      {"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "DONE",
       "created_at": "2026-01-01T00:00:07Z",
       "content": "Created At: 2026-01-01T00:00:06Z\nCompleted At: 2026-01-01T00:00:07Z\ncc_library(...)"},
      {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE",
       "status": "DONE", "created_at": "2026-01-01T00:00:30Z",
       "input_tokens": 500, "cache_read_tokens": 5000, "output_tokens": 80,
       "tool_calls": [
           {"name": "invoke_subagent", "args": _args(Role="Build Debugger", Prompt="Find the flake")},
       ]},
      {"step_index": 6, "source": "MODEL", "type": "GENERIC", "status": "DONE",
       "created_at": "2026-01-01T00:00:31Z",
       "content": ("Created At: 2026-01-01T00:00:31Z\nCompleted At: 2026-01-01T00:00:31Z\n"
                   "Created the following subagent:\n{\n  \"conversationId\":  \"" + CHILD_ID + "\"\n}")},
      {"step_index": 7, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE",
       "created_at": "2026-01-01T00:05:00Z", "content": "# Resuming from a compaction"},
      {"step_index": 8, "source": "MODEL", "type": "PLANNER_RESPONSE",
       "status": "DONE", "created_at": "2026-01-01T00:05:10Z",
       "input_tokens": 200, "cache_read_tokens": 1000, "output_tokens": 300,
       "tool_calls": [
           {"name": "edit_file", "args": _args(Path="/repo/BUILD", Target="a", Replacement="b")},
       ]},
      {"step_index": 9, "source": "MODEL", "type": "GENERIC", "status": "DONE",
       "created_at": "2026-01-01T00:05:11Z",
       "content": "Created At: 2026-01-01T00:05:10Z\nCompleted At: 2026-01-01T00:05:11Z\nok"},
      {"step_index": 10, "source": "USER_EXPLICIT", "type": "USER_INPUT",
       "status": "DONE", "created_at": "2026-01-01T01:00:00Z",
       "content": "<USER_REQUEST>Thanks, now push it</USER_REQUEST>"},
      {"step_index": 11, "source": "MODEL", "type": "PLANNER_RESPONSE",
       "status": "DONE", "created_at": "2026-01-01T01:00:04Z",
       "input_tokens": 100, "cache_read_tokens": 6000, "output_tokens": 20,
       "tool_calls": [
           {"name": "run_command", "args": _args(CommandLine="git push")},
       ]},
  ]


def child_steps():
  return [
      {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
       "status": "DONE", "created_at": "2026-01-01T00:00:32Z",
       "content": "<USER_REQUEST>Find the flake</USER_REQUEST>"},
      {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
       "status": "DONE", "created_at": "2026-01-01T00:00:40Z",
       "input_tokens": 10, "cache_read_tokens": 0, "output_tokens": 5,
       "tool_calls": [{"name": "code_search", "args": _args(Query="flaky")}]},
      {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE",
       "created_at": "2026-01-01T00:00:42Z", "content": "results"},
  ]


def write_conversation(app_dir: pathlib.Path, conv_id: str, steps, full_steps=None):
  logs = app_dir / "brain" / conv_id / ".system_generated" / "logs"
  logs.mkdir(parents=True, exist_ok=True)
  with open(logs / "transcript.jsonl", "w", encoding="utf-8") as f:
    for s in steps:
      f.write(json.dumps(s) + "\n")
  if full_steps is not None:
    with open(logs / "transcript_full.jsonl", "w", encoding="utf-8") as f:
      for s in full_steps:
        f.write(json.dumps(s) + "\n")
  return logs


def make_app_dir(tmp: pathlib.Path) -> pathlib.Path:
  app = tmp / "app"
  full = parent_steps()
  truncated = [dict(s) for s in full]
  truncated[4] = dict(truncated[4], content="cc_lib…", truncated_fields=["content"])
  write_conversation(app, PARENT_ID, truncated, full_steps=full)
  write_conversation(app, CHILD_ID, child_steps())
  # Make the parent the most recently updated conversation.
  parent_t = app / "brain" / PARENT_ID / ".system_generated" / "logs" / "transcript.jsonl"
  child_t = app / "brain" / CHILD_ID / ".system_generated" / "logs" / "transcript.jsonl"
  os.utime(child_t, (1_700_000_000, 1_700_000_000))
  os.utime(parent_t, (1_700_000_100, 1_700_000_100))
  return app
