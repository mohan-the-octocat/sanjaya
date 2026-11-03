"""Trajectory parsing for Sanjaya.

Reads Antigravity / Jetski conversation transcripts
(`<appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl`)
and turns them into a compact model the web UI can render as a DAG, a
timeline, tool statistics, token/context charts, and a subagent tree.

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import threading
from typing import Any, Iterable

TRANSCRIPT_REL = pathlib.Path(".system_generated") / "logs" / "transcript.jsonl"
TRANSCRIPT_FULL_NAME = "transcript_full.jsonl"

CONV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_CREATED_RE = re.compile(r"Created At:\s*(\S+)")
_COMPLETED_RE = re.compile(r"Completed At:\s*(\S+)")
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
# Inter-agent messages (subagent prompts, subagent reports) arrive as
# SYSTEM_MESSAGE steps: "[Message] timestamp=... sender=<id> ... content=<body>".
_MESSAGE_RE = re.compile(
    r"\[Message\][^\n]*?sender=(\S+)[^\n]*?content=(.*?)(?:</SYSTEM_MESSAGE>|$)", re.S
)

PREVIEW_CHARS = 280
DETAIL_CHARS = 200_000

# Tool name -> phase category. Used for the heuristic "Phases" view, which
# groups consecutive model steps doing the same kind of work.
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("delegate", ("invoke_subagent", "swarm", "run_workflow", "define_subagent",
                  "manage_subagents")),
    ("edit", ("write_file", "write_to_file", "edit_file", "replace",
              "create_file", "apply_patch", "multi_edit")),
    ("execute", ("run_command", "manage_task", "schedule", "shell")),
    ("research", ("search_web", "moma_search", "web_fetch", "read_url",
                  "browser", "fetch")),
    ("explore", ("read_file", "view_file", "list_dir", "code_search", "grep",
                 "find", "glob", "skill_search", "read_resource",
                 "list_resources")),
    ("communicate", ("send_message", "ask_question", "notify")),
)

KIND_USER = "user"
KIND_MODEL = "model"
KIND_TOOL = "tool"
KIND_SYSTEM = "system"
KIND_CHECKPOINT = "checkpoint"
KIND_ERROR = "error"
KIND_MESSAGE = "message"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def default_app_dirs(environ: dict[str, str] | None = None) -> list[pathlib.Path]:
  """Returns candidate app data dirs, most specific first, de-duplicated."""
  env = os.environ if environ is None else environ
  candidates: list[pathlib.Path] = []
  override = env.get("SANJAYA_APP_DIRS", "")
  for raw in override.split(os.pathsep):
    if raw.strip():
      candidates.append(pathlib.Path(raw.strip()).expanduser())
  data_dir = env.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR", "")
  if data_dir:
    parts = pathlib.Path(data_dir).parts
    if "sidecar_data" in parts:
      idx = len(parts) - 1 - list(reversed(parts)).index("sidecar_data")
      if idx > 0:
        candidates.append(pathlib.Path(*parts[:idx]))
  home = pathlib.Path(env.get("HOME") or pathlib.Path.home()) / ".gemini"
  for name in ("antigravity", "jetski", "antigravity-sdk"):
    candidates.append(home / name)

  seen: set[str] = set()
  result: list[pathlib.Path] = []
  for c in candidates:
    try:
      key = str(c.resolve())
    except OSError:
      key = str(c)
    if key in seen:
      continue
    seen.add(key)
    result.append(c)
  return result


def is_valid_conversation_id(conv_id: str | None) -> bool:
  return bool(conv_id) and bool(CONV_ID_RE.match(conv_id or ""))


def find_conversation_dir(
    conv_id: str, app_dirs: Iterable[pathlib.Path]
) -> pathlib.Path | None:
  """Returns `<appDataDir>/brain/<conv_id>` holding a transcript, or None."""
  if not is_valid_conversation_id(conv_id):
    return None
  for app_dir in app_dirs:
    conv_dir = app_dir / "brain" / conv_id
    if (conv_dir / TRANSCRIPT_REL).is_file():
      return conv_dir
  return None


def _extract_request(text: str) -> str:
  m = _USER_REQUEST_RE.search(text or "")
  body = m.group(1) if m else (text or "")
  return body.strip()


def parse_message(text: str) -> tuple[str, str] | None:
  """Returns (sender, body) for an inter-agent SYSTEM_MESSAGE, else None."""
  if not text or "[Message]" not in text:
    return None
  m = _MESSAGE_RE.search(text)
  if not m:
    return None
  return m.group(1), m.group(2).strip()


def _first_line_title(text: str, limit: int = 110) -> str:
  body = _extract_request(text)
  for line in body.splitlines():
    line = line.strip()
    if line:
      return line[:limit] + ("…" if len(line) > limit else "")
  return ""


_title_cache: dict[str, str] = {}
_title_lock = threading.Lock()


def _read_title(transcript: pathlib.Path) -> str:
  key = str(transcript)
  with _title_lock:
    if key in _title_cache:
      return _title_cache[key]
  title = ""
  fallback = ""
  try:
    with open(transcript, "r", encoding="utf-8", errors="replace") as f:
      for _ in range(25):
        line = f.readline()
        if not line:
          break
        try:
          obj = json.loads(line)
        except ValueError:
          continue
        if obj.get("type") == "USER_INPUT":
          title = _first_line_title(obj.get("content") or "")
          break
        if not fallback and obj.get("type") == "SYSTEM_MESSAGE":
          msg = parse_message(obj.get("content") or "")
          if msg:
            fallback = _first_line_title(_strip_message_preamble(msg[1]))
  except OSError:
    return ""
  title = title or fallback
  if title:
    with _title_lock:
      _title_cache[key] = title
  return title


def _strip_message_preamble(body: str) -> str:
  """Drops leading KEY=value plumbing lines (e.g. PLUGIN_ROOT=...)."""
  lines = body.splitlines()
  while lines and (not lines[0].strip() or re.match(r"^[A-Z_]+=\S*$", lines[0].strip())):
    lines.pop(0)
  return "\n".join(lines)


def list_conversations(
    app_dirs: Iterable[pathlib.Path], limit: int = 60
) -> list[dict[str, Any]]:
  """Lists recent conversations across all app data dirs, newest first."""
  entries: list[tuple[float, str, pathlib.Path, pathlib.Path]] = []
  seen: set[str] = set()
  for app_dir in app_dirs:
    brain = app_dir / "brain"
    try:
      children = list(os.scandir(brain))
    except OSError:
      continue
    for child in children:
      if child.name in seen or not is_valid_conversation_id(child.name):
        continue
      transcript = pathlib.Path(child.path) / TRANSCRIPT_REL
      try:
        st = transcript.stat()
      except OSError:
        continue
      seen.add(child.name)
      entries.append((st.st_mtime, child.name, transcript, app_dir))
  entries.sort(key=lambda e: e[0], reverse=True)
  out = []
  for mtime, conv_id, transcript, app_dir in entries[: max(1, limit)]:
    out.append({
        "id": conv_id,
        "title": _read_title(transcript) or conv_id,
        "app": app_dir.name,
        "updated": _iso_from_epoch(mtime),
        "mtime": mtime,
        "size": _safe_size(transcript),
    })
  return out


def _safe_size(p: pathlib.Path) -> int:
  try:
    return p.stat().st_size
  except OSError:
    return 0


def _iso_from_epoch(ts: float) -> str:
  return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime(
      "%Y-%m-%dT%H:%M:%SZ"
  )


def transcript_meta(conv_dir: pathlib.Path) -> dict[str, Any]:
  path = conv_dir / TRANSCRIPT_REL
  try:
    st = path.stat()
  except OSError:
    return {"exists": False}
  return {
      "exists": True,
      "mtime": st.st_mtime,
      "size": st.st_size,
      "version": f"{st.st_mtime_ns}-{st.st_size}",
  }


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


def parse_time(value: str | None) -> float | None:
  """Parses an ISO-8601 timestamp into epoch seconds (None if invalid)."""
  if not value or not isinstance(value, str):
    return None
  v = value.strip()
  if v.endswith("Z"):
    v = v[:-1] + "+00:00"
  try:
    dt = _dt.datetime.fromisoformat(v)
  except ValueError:
    return None
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=_dt.timezone.utc)
  return dt.timestamp()


def decode_args(args: Any) -> Any:
  """Tool-call args arrive with JSON-encoded string values; decode them."""
  if not isinstance(args, dict):
    return args
  out = {}
  for k, v in args.items():
    if isinstance(v, str):
      try:
        out[k] = json.loads(v)
        continue
      except ValueError:
        pass
    out[k] = v
  return out


def tool_category(name: str) -> str:
  n = (name or "").lower()
  for category, needles in _CATEGORY_RULES:
    for needle in needles:
      if n == needle or n.endswith("_" + needle) or needle in n:
        return category
  if n.startswith("mcp_") or n == "call_mcp_tool":
    return "integrate"
  return "other"


def tool_summary(name: str, args: dict[str, Any]) -> str:
  """Returns a short human label for a tool call."""
  if not isinstance(args, dict):
    return name
  for key in ("toolSummary", "Role", "Description", "CommandLine", "Query",
              "query", "Path", "AbsolutePath", "Prompt", "Message", "action"):
    val = args.get(key)
    if isinstance(val, str) and val.strip():
      text = val.strip().splitlines()[0]
      if key in ("Path", "AbsolutePath"):
        text = pathlib.PurePath(text).name or text
      return text[:90]
  return name


def _clip(text: Any, limit: int = PREVIEW_CHARS) -> str:
  if text is None:
    return ""
  if not isinstance(text, str):
    try:
      text = json.dumps(text, ensure_ascii=False)
    except (TypeError, ValueError):
      text = str(text)
  text = text.strip()
  return text if len(text) <= limit else text[:limit] + "…"


def _tool_duration(content: str) -> float | None:
  if not content:
    return None
  head = content[:400]
  a = _CREATED_RE.search(head)
  b = _COMPLETED_RE.search(head)
  if not a or not b:
    return None
  ta, tb = parse_time(a.group(1)), parse_time(b.group(1))
  if ta is None or tb is None:
    return None
  return max(0.0, tb - ta)


def read_steps(path: pathlib.Path) -> list[dict[str, Any]]:
  steps = []
  try:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          obj = json.loads(line)
        except ValueError:
          continue
        if isinstance(obj, dict):
          steps.append(obj)
  except OSError:
    return []
  return steps


# --------------------------------------------------------------------------
# Trajectory model
# --------------------------------------------------------------------------


def _kind_for(step: dict[str, Any], is_tool_result: bool) -> str:
  stype = step.get("type") or ""
  if stype == "USER_INPUT":
    return KIND_USER
  if stype == "PLANNER_RESPONSE":
    return KIND_MODEL
  if stype == "CHECKPOINT":
    return KIND_CHECKPOINT
  if stype == "ERROR_MESSAGE":
    return KIND_ERROR
  if stype == "SYSTEM_MESSAGE" and parse_message(step.get("content") or ""):
    return KIND_MESSAGE
  if is_tool_result:
    return KIND_TOOL
  return KIND_SYSTEM


def build_trajectory(
    conv_id: str,
    steps: list[dict[str, Any]],
    app_dirs: Iterable[pathlib.Path] | None = None,
) -> dict[str, Any]:
  """Builds the UI model from raw transcript steps."""
  app_dirs = list(app_dirs or [])
  out_steps: list[dict[str, Any]] = []
  pending: list[dict[str, Any]] = []
  call_by_id: dict[str, dict[str, Any]] = {}
  turns: list[dict[str, Any]] = []
  tool_stats: dict[str, dict[str, Any]] = {}
  subagents: list[dict[str, Any]] = []
  subagent_ids: set[str] = set()
  totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
  errors = 0
  compactions = 0
  prev_t: float | None = None
  first_t: float | None = None
  last_t: float | None = None

  # Transcript lines are mostly, but not strictly, ordered by step_index
  # (a tool result can be flushed before its planner step). Sort stably.
  steps = sorted(
      steps,
      key=lambda r: r.get("step_index") if isinstance(r.get("step_index"), int) else 1 << 62,
  )

  for raw in steps:
    idx = raw.get("step_index")
    if not isinstance(idx, int):
      idx = len(out_steps)
    stype = raw.get("type") or ""
    status = raw.get("status") or ""
    t = parse_time(raw.get("created_at"))
    if t is not None:
      first_t = t if first_t is None else min(first_t, t)
      last_t = t if last_t is None else max(last_t, t)
    gap = (t - prev_t) if (t is not None and prev_t is not None) else None
    if t is not None:
      prev_t = t
    content = raw.get("content") or ""
    error_text = raw.get("error") or ""

    result_of = None
    if stype in ("USER_INPUT", "PLANNER_RESPONSE", "CHECKPOINT"):
      pending = []
    elif stype == "GENERIC" and pending:
      result_of = pending.pop(0)

    kind = _kind_for(raw, result_of is not None)
    is_error = status == "ERROR" or stype == "ERROR_MESSAGE"
    if is_error:
      errors += 1
    if stype == "CHECKPOINT":
      compactions += 1

    message = parse_message(content) if kind == KIND_MESSAGE else None
    # Subagent conversations have no USER_INPUT: their first inbound message
    # (the delegated prompt) opens the first turn.
    opens_turn = stype == "USER_INPUT" or (message is not None and not turns)
    if opens_turn:
      request_text = (
          _extract_request(content) if stype == "USER_INPUT"
          else _strip_message_preamble(message[1])
      )
      turns.append({
          "index": len(turns),
          "step_index": idx,
          "request": _clip(request_text, 400),
          "started_at": raw.get("created_at"),
          "start_t": t,
          "end_t": t,
          "steps": [],
          "tool_calls": 0,
          "errors": 0,
          "tools": {},
      })
    turn = turns[-1] if turns else None

    step: dict[str, Any] = {
        "i": idx,
        "type": stype,
        "source": raw.get("source") or "",
        "status": status,
        "kind": kind,
        "created_at": raw.get("created_at"),
        "t": t,
        "gap_s": round(gap, 3) if gap is not None else None,
        "turn": turn["index"] if turn else None,
        "truncated": bool(raw.get("truncated_fields")),
        "preview": _clip(
            _extract_request(content) if stype == "USER_INPUT" else content
        ),
        "error": _clip(error_text, 400) if error_text else "",
    }

    if stype == "PLANNER_RESPONSE":
      tokens = {
          "in": int(raw.get("input_tokens") or 0),
          "out": int(raw.get("output_tokens") or 0),
          "cached": int(raw.get("cache_read_tokens") or 0),
      }
      totals["input_tokens"] += tokens["in"]
      totals["output_tokens"] += tokens["out"]
      totals["cache_read_tokens"] += tokens["cached"]
      step["tokens"] = tokens
      thinking = raw.get("thinking") or ""
      step["has_thinking"] = bool(thinking)
      step["thinking_preview"] = _clip(thinking, 400)
      calls = []
      for ci, call in enumerate(raw.get("tool_calls") or []):
        name = (call or {}).get("name") or "unknown"
        args = decode_args((call or {}).get("args") or {})
        call_id = f"{idx}.{ci}"
        c = {
            "id": call_id,
            "name": name,
            "category": tool_category(name),
            "summary": tool_summary(name, args),
            "args_preview": _clip(args, 600),
            "result_step": None,
            "status": "pending",
            "duration_s": None,
        }
        calls.append(c)
        call_by_id[call_id] = c
        pending.append(call_id)
        stat = tool_stats.setdefault(
            name, {"name": name, "category": c["category"], "count": 0,
                   "errors": 0, "total_s": 0.0, "timed": 0}
        )
        stat["count"] += 1
        if turn is not None:
          turn["tool_calls"] += 1
          turn["tools"][name] = turn["tools"].get(name, 0) + 1
      step["tool_calls"] = calls
      cats = [c["category"] for c in calls]
      # Dominant category; ties go to the first category called (stable,
      # unlike max() over a set, whose order varies with hash seeds).
      step["category"] = (
          max(dict.fromkeys(cats), key=cats.count) if cats else "respond"
      )
      if calls:
        step["label"] = ", ".join(sorted({c["name"] for c in calls}))[:80]
      else:
        step["label"] = "Response"
    elif result_of is not None:
      call = call_by_id[result_of]
      step["result_of"] = result_of
      step["tool_name"] = call["name"]
      step["category"] = call["category"]
      step["label"] = call["summary"] or call["name"]
      duration = _tool_duration(content)
      call["result_step"] = idx
      call["status"] = "error" if is_error else "done"
      call["duration_s"] = duration
      stat = tool_stats[call["name"]]
      if is_error:
        stat["errors"] += 1
      if duration is not None:
        stat["total_s"] += duration
        stat["timed"] += 1
      step["duration_s"] = duration
      if call["name"] in ("invoke_subagent", "swarm") and not is_error:
        for child in dict.fromkeys(UUID_RE.findall(content)):
          if child == conv_id or child in subagent_ids:
            continue
          subagent_ids.add(child)
          child_dir = find_conversation_dir(child, app_dirs) if app_dirs else None
          subagents.append({
              "id": child,
              "spawned_by_step": call_by_id[result_of]["id"].split(".")[0],
              "result_step": idx,
              "role": call["summary"],
              "exists": child_dir is not None,
              "title": (
                  _read_title(child_dir / TRANSCRIPT_REL) if child_dir else ""
              ),
          })
      step.setdefault("children", [])
      step["children"] = [
          s["id"] for s in subagents if s["result_step"] == idx
      ]
    elif stype == "USER_INPUT":
      step["label"] = _first_line_title(content, 80) or "User input"
    elif message is not None:
      sender, body = message
      body = _strip_message_preamble(body)
      step["sender"] = sender
      step["preview"] = _clip(body)
      step["label"] = (
          f"Message from {sender[:8]}: " + (_first_line_title(body, 70) or "…")
      )
    elif stype == "CHECKPOINT":
      step["label"] = "Context compaction"
    elif stype == "ERROR_MESSAGE":
      step["label"] = "Error"
    else:
      step["label"] = stype.replace("_", " ").title() or "Step"

    if turn is not None:
      turn["steps"].append(idx)
      if t is not None:
        turn["end_t"] = t
      if is_error:
        turn["errors"] += 1
    out_steps.append(step)

  for turn in turns:
    if turn["start_t"] is not None and turn["end_t"] is not None:
      turn["duration_s"] = round(turn["end_t"] - turn["start_t"], 3)
    else:
      turn["duration_s"] = None

  for stat in tool_stats.values():
    stat["avg_s"] = round(stat["total_s"] / stat["timed"], 3) if stat["timed"] else None
    stat["total_s"] = round(stat["total_s"], 3)

  phases = build_phases(out_steps)
  title = turns[0]["request"].splitlines()[0][:110] if turns else conv_id

  return {
      "id": conv_id,
      "title": title,
      "steps": out_steps,
      "turns": turns,
      "phases": phases,
      "subagents": subagents,
      "tools": sorted(tool_stats.values(), key=lambda s: -s["count"]),
      "stats": {
          "steps": len(out_steps),
          "turns": len(turns),
          "model_steps": sum(1 for s in out_steps if s["kind"] == KIND_MODEL),
          "tool_calls": sum(s["count"] for s in tool_stats.values()),
          "errors": errors,
          "compactions": compactions,
          "subagents": len(subagents),
          "started_at": _iso_from_epoch(first_t) if first_t is not None else None,
          "ended_at": _iso_from_epoch(last_t) if last_t is not None else None,
          "duration_s": (
              round(last_t - first_t, 3)
              if first_t is not None and last_t is not None else None
          ),
          **totals,
      },
  }


def build_phases(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Groups consecutive model steps of the same category into phases.

  This is a deterministic, LLM-free stand-in for milestone summarisation.
  User inputs and compactions always start a new phase.
  """
  phases: list[dict[str, Any]] = []
  current: dict[str, Any] | None = None
  for s in steps:
    kind = s["kind"]
    if kind in (KIND_USER, KIND_CHECKPOINT, KIND_MESSAGE):
      current = {
          "category": {KIND_USER: "request", KIND_CHECKPOINT: "compaction",
                       KIND_MESSAGE: "message"}[kind],
          "label": s.get("label", ""),
          "start": s["i"], "end": s["i"], "steps": [s["i"]],
          "tool_calls": 0, "errors": 0, "turn": s.get("turn"),
          "start_t": s.get("t"), "end_t": s.get("t"),
      }
      phases.append(current)
      current = None
      continue
    if kind != KIND_MODEL:
      if current is not None:
        current["end"] = s["i"]
        current["steps"].append(s["i"])
        if s.get("t") is not None:
          current["end_t"] = s["t"]
        if s.get("status") == "ERROR" or kind == KIND_ERROR:
          current["errors"] += 1
      continue
    cat = s.get("category") or "respond"
    if current is None or current["category"] != cat or current["turn"] != s.get("turn"):
      current = {
          "category": cat, "label": "", "start": s["i"], "end": s["i"],
          "steps": [], "tool_calls": 0, "errors": 0, "turn": s.get("turn"),
          "tools": {}, "start_t": s.get("t"), "end_t": s.get("t"),
      }
      phases.append(current)
    current["end"] = s["i"]
    current["steps"].append(s["i"])
    if s.get("t") is not None:
      current["end_t"] = s["t"]
    for c in s.get("tool_calls") or []:
      current["tool_calls"] += 1
      current.setdefault("tools", {})
      current["tools"][c["name"]] = current["tools"].get(c["name"], 0) + 1
  for p in phases:
    if not p["label"]:
      tools = p.get("tools") or {}
      top = sorted(tools.items(), key=lambda kv: -kv[1])[:3]
      p["label"] = ", ".join(f"{n}×{c}" if c > 1 else n for n, c in top) or (
          "model response" if p["category"] == "respond" else p["category"])
    if p.get("start_t") is not None and p.get("end_t") is not None:
      p["duration_s"] = round(p["end_t"] - p["start_t"], 3)
    else:
      p["duration_s"] = None
  return phases


# --------------------------------------------------------------------------
# Cached loading
# --------------------------------------------------------------------------


class TrajectoryStore:
  """Caches parsed trajectories keyed by (path, mtime_ns, size)."""

  def __init__(self, app_dirs: list[pathlib.Path], max_entries: int = 24):
    self.app_dirs = app_dirs
    self.max_entries = max_entries
    self._cache: dict[str, tuple[str, dict[str, Any]]] = {}
    self._lock = threading.Lock()

  def conv_dir(self, conv_id: str) -> pathlib.Path | None:
    return find_conversation_dir(conv_id, self.app_dirs)

  def load(self, conv_id: str) -> dict[str, Any] | None:
    conv_dir = self.conv_dir(conv_id)
    if conv_dir is None:
      return None
    meta = transcript_meta(conv_dir)
    if not meta.get("exists"):
      return None
    version = meta["version"]
    with self._lock:
      hit = self._cache.get(conv_id)
      if hit and hit[0] == version:
        return hit[1]
    model = build_trajectory(
        conv_id, read_steps(conv_dir / TRANSCRIPT_REL), self.app_dirs
    )
    model["version"] = version
    model["mtime"] = meta["mtime"]
    model["app"] = conv_dir.parent.parent.name
    with self._lock:
      self._cache[conv_id] = (version, model)
      if len(self._cache) > self.max_entries:
        oldest = next(iter(self._cache))
        self._cache.pop(oldest, None)
    return model

  def step_detail(self, conv_id: str, step_index: int) -> dict[str, Any] | None:
    """Returns the full (untruncated) raw step, preferring transcript_full."""
    conv_dir = self.conv_dir(conv_id)
    if conv_dir is None:
      return None
    logs = conv_dir / TRANSCRIPT_REL.parent
    for name in (TRANSCRIPT_FULL_NAME, TRANSCRIPT_REL.name):
      path = logs / name
      if not path.is_file():
        continue
      raw = _find_step(path, step_index)
      if raw is not None:
        return _detail_from_raw(raw, source_file=name)
    return None

  def subagent_tree(
      self, conv_id: str, depth: int = 3, budget: int = 60
  ) -> dict[str, Any] | None:
    seen: set[str] = set()
    remaining = [budget]

    def walk(cid: str, d: int, role: str) -> dict[str, Any] | None:
      if cid in seen or remaining[0] <= 0:
        return None
      seen.add(cid)
      remaining[0] -= 1
      model = self.load(cid)
      if model is None:
        return {"id": cid, "title": "", "role": role, "missing": True,
                "children": []}
      node = {
          "id": cid,
          "title": model["title"],
          "role": role,
          "stats": {k: model["stats"].get(k) for k in (
              "steps", "tool_calls", "errors", "duration_s", "subagents",
              "input_tokens", "output_tokens", "cache_read_tokens",
              "ended_at")},
          "children": [],
      }
      if d < depth:
        for sub in model["subagents"]:
          child = walk(sub["id"], d + 1, sub.get("role") or "")
          if child is not None:
            node["children"].append(child)
      return node

    return walk(conv_id, 0, "root")


def _find_step(path: pathlib.Path, step_index: int) -> dict[str, Any] | None:
  needle = f'"step_index":{step_index},'
  needle_sp = f'"step_index": {step_index},'
  try:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
      for line in f:
        if needle not in line and needle_sp not in line:
          continue
        try:
          obj = json.loads(line)
        except ValueError:
          continue
        if obj.get("step_index") == step_index:
          return obj
  except OSError:
    return None
  return None


def _detail_from_raw(raw: dict[str, Any], source_file: str) -> dict[str, Any]:
  def big(text: Any) -> str:
    if text is None:
      return ""
    if not isinstance(text, str):
      text = json.dumps(text, ensure_ascii=False, indent=2)
    return text if len(text) <= DETAIL_CHARS else (
        text[:DETAIL_CHARS] + f"\n… [{len(text) - DETAIL_CHARS} more chars]"
    )

  calls = []
  for call in raw.get("tool_calls") or []:
    calls.append({
        "name": (call or {}).get("name"),
        "args": decode_args((call or {}).get("args") or {}),
    })
  return {
      "i": raw.get("step_index"),
      "type": raw.get("type"),
      "source": raw.get("source"),
      "status": raw.get("status"),
      "created_at": raw.get("created_at"),
      "content": big(raw.get("content")),
      "thinking": big(raw.get("thinking")),
      "error": big(raw.get("error")),
      "tool_calls": calls,
      "tokens": {
          "in": raw.get("input_tokens"),
          "out": raw.get("output_tokens"),
          "cached": raw.get("cache_read_tokens"),
      },
      "media": raw.get("media") or [],
      "source_file": source_file,
  }


# --------------------------------------------------------------------------
# Export & fork handoff
# --------------------------------------------------------------------------


def _fmt_dur(seconds: float | None) -> str:
  if seconds is None:
    return "—"
  seconds = float(seconds)
  if seconds < 60:
    return f"{seconds:.1f}s"
  if seconds < 3600:
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"
  return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60)}m"


def export_markdown(model: dict[str, Any],
                    annotations: dict[str, Any] | None = None) -> str:
  """Renders a trajectory as a Markdown report."""
  st = model["stats"]
  notes = (annotations or {})
  lines = [
      f"# Trajectory: {model['title']}",
      "",
      f"- Conversation: `{model['id']}`",
      f"- Window: {st.get('started_at') or '—'} → {st.get('ended_at') or '—'}"
      f" ({_fmt_dur(st.get('duration_s'))})",
      f"- Steps: {st['steps']} · Turns: {st['turns']} · Tool calls: "
      f"{st['tool_calls']} · Errors: {st['errors']} · Compactions: "
      f"{st['compactions']} · Subagents: {st['subagents']}",
      f"- Tokens: in {st['input_tokens']:,} · out {st['output_tokens']:,} · "
      f"cached {st['cache_read_tokens']:,}",
      "",
  ]
  if notes.get("_"):
    lines += ["## Trajectory notes", ""]
    lines += [f"- {n['note']}" for n in notes["_"]]
    lines.append("")
  lines += ["## Phases", "", "| # | Phase | Steps | Tools | Errors | Duration |",
            "|---|---|---|---|---|---|"]
  for n, p in enumerate(model["phases"], 1):
    label = str(p["label"]).replace("|", "\\|")[:80]
    lines.append(
        f"| {n} | {p['category']}: {label} | {p['start']}–{p['end']} | "
        f"{p['tool_calls']} | {p['errors']} | {_fmt_dur(p.get('duration_s'))} |"
    )
  lines += ["", "## Tool usage", "", "| Tool | Calls | Errors | Avg |",
            "|---|---|---|---|"]
  for t in model["tools"]:
    lines.append(
        f"| `{t['name']}` | {t['count']} | {t['errors']} | {_fmt_dur(t['avg_s'])} |"
    )
  for turn in model["turns"]:
    lines += ["", f"## Turn {turn['index'] + 1}", "",
              "> " + turn["request"].replace("\n", "\n> "), ""]
    for i in turn["steps"]:
      s = next((x for x in model["steps"] if x["i"] == i), None)
      if s is None or s["kind"] in (KIND_SYSTEM,):
        continue
      flag = " ❌" if s["status"] == "ERROR" or s["kind"] == KIND_ERROR else ""
      if s["kind"] == KIND_MODEL:
        calls = ", ".join(f"`{c['name']}` ({c['summary']})" for c in s.get("tool_calls", []))
        lines.append(f"- **#{i} model**{flag}: {calls or s['preview'][:160]}")
      elif s["kind"] == KIND_TOOL:
        lines.append(f"  - #{i} ← `{s.get('tool_name')}` {_fmt_dur(s.get('duration_s'))}{flag}")
      elif s["kind"] == KIND_CHECKPOINT:
        lines.append(f"- **#{i} context compaction**")
      elif s["kind"] == KIND_ERROR:
        lines.append(f"- **#{i} error**: {s['error'][:200]}")
      for n in notes.get(str(i), []):
        lines.append(f"  - 📝 {n['note']}")
  if model["subagents"]:
    lines += ["", "## Subagents", ""]
    for sub in model["subagents"]:
      lines.append(f"- `{sub['id']}` — {sub.get('role') or ''} {sub.get('title') or ''}".rstrip())
  return "\n".join(lines) + "\n"


def fork_prompt(model: dict[str, Any], step_index: int, guidance: str) -> str:
  """Builds a self-contained handoff prompt for forking at `step_index`."""
  requests = [t["request"] for t in model["turns"] if t["step_index"] <= step_index]
  done = []
  for s in model["steps"]:
    if s["i"] > step_index:
      break
    if s["kind"] == KIND_MODEL and s.get("tool_calls"):
      for c in s["tool_calls"]:
        status = c.get("status")
        done.append(f"- step {s['i']}: {c['name']} — {c['summary']}"
                    + (" (failed)" if status == "error" else ""))
    elif s["kind"] == KIND_CHECKPOINT:
      done.append(f"- step {s['i']}: context compaction")
  done = done[-60:]
  parts = [
      f"This is a fork of conversation {model['id']} at step {step_index}.",
      "Continue the work from that point, taking a different approach where the "
      "guidance below says so. Do not repeat completed work unless needed.",
      "",
      "## Original user requests",
      *[f"{n}. {r}" for n, r in enumerate(requests, 1)],
      "",
      "## Actions already taken (up to the fork point)",
      *(done or ["- none"]),
      "",
      "## Guidance for this fork",
      guidance.strip() or "(none provided — re-evaluate the approach from here)",
  ]
  return "\n".join(parts)
