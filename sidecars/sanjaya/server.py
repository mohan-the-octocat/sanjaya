"""Sanjaya sidecar HTTP server.

Serves the Sanjaya trajectory viewer UI into the Antigravity / Jetski
Auxiliary Pane and exposes a small JSON API over local conversation
transcripts. Python 3 standard library only.

Environment (injected by the host SidecarManager):
  ANTIGRAVITY_SIDECAR_WEB_PORT     Port to bind on 127.0.0.1 (required in host).
  ANTIGRAVITY_SIDECAR_UI_TOKEN     Token checked on API and POST requests.
  ANTIGRAVITY_EXECUTABLE_DATA_DIR  Persistent data dir (annotations live here).
Optional:
  SANJAYA_APP_DIRS                 os.pathsep-separated app data dirs to scan.
"""

from __future__ import annotations

import argparse
import hmac
import http
import http.server
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.parse

import trajectory

_HERE = pathlib.Path(__file__).resolve().parent
STATIC_DIR = _HERE / "static"
MAX_BODY = 1 * 1024 * 1024
VERSION = "1.0.0"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/preload.js": ("bridge.js", "application/javascript; charset=utf-8"),
    "/logo.svg": ("logo.svg", "image/svg+xml"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] sanjaya: %(message)s",
)
logger = logging.getLogger("sanjaya")


# --------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------


class AnnotationStore:
  """Step- and trajectory-level notes persisted as one JSON file."""

  def __init__(self, path: pathlib.Path):
    self.path = path
    self._lock = threading.Lock()

  def _read(self) -> dict[str, Any]:
    try:
      with open(self.path, "r", encoding="utf-8") as f:
        data = json.load(f)
      return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
      return {}

  def _write(self, data: dict[str, Any]) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2, ensure_ascii=False)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, self.path)

  def get(self, conv_id: str) -> dict[str, list[dict[str, Any]]]:
    with self._lock:
      return self._read().get(conv_id, {})

  def add(self, conv_id: str, step: str, note: str) -> dict[str, Any]:
    note = note.strip()[:4000]
    if not note:
      raise ValueError("note is empty")
    with self._lock:
      data = self._read()
      conv = data.setdefault(conv_id, {})
      entry = {"note": note,
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
      conv.setdefault(step, []).append(entry)
      self._write(data)
      return conv

  def delete(self, conv_id: str, step: str, index: int) -> dict[str, Any]:
    with self._lock:
      data = self._read()
      conv = data.get(conv_id, {})
      notes = conv.get(step, [])
      if not 0 <= index < len(notes):
        raise ValueError("annotation index out of range")
      notes.pop(index)
      if not notes:
        conv.pop(step, None)
      if conv:
        data[conv_id] = conv
      else:
        data.pop(conv_id, None)
      self._write(data)
      return conv


# --------------------------------------------------------------------------
# agentapi bridge (steer / fork)
# --------------------------------------------------------------------------


def find_agentapi(app_dirs: list[pathlib.Path]) -> str | None:
  found = shutil.which("agentapi")
  if found:
    return found
  for app_dir in app_dirs:
    candidate = app_dir / "bin" / "agentapi"
    if candidate.is_file() and os.access(candidate, os.X_OK):
      return str(candidate)
  return None


def run_agentapi(binary: str | None, args: list[str],
                 project_id: str | None = None) -> Any:
  if not binary:
    raise RuntimeError("agentapi CLI not found on PATH")
  env = os.environ.copy()
  if project_id:
    env["ANTIGRAVITY_PROJECT_ID"] = project_id
  res = subprocess.run(
      [binary, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      text=True, env=env, timeout=30, check=False,
  )
  if res.returncode != 0:
    raise RuntimeError(f"agentapi failed: {res.stderr.strip()[:500]}")
  out = res.stdout.strip()
  try:
    return json.loads(out) if out else {}
  except ValueError:
    return {"output": out}


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


class SanjayaServer(http.server.ThreadingHTTPServer):
  daemon_threads = True
  allow_reuse_address = True

  def __init__(self, addr, handler, *, store: trajectory.TrajectoryStore,
               annotations: AnnotationStore, token: str | None,
               agentapi: str | None):
    super().__init__(addr, handler)
    self.store = store
    self.annotations = annotations
    self.token = token
    self.agentapi = agentapi


class Handler(http.server.BaseHTTPRequestHandler):
  server: SanjayaServer
  server_version = f"Sanjaya/{VERSION}"
  sys_version = ""
  protocol_version = "HTTP/1.1"

  def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
    logger.debug("%s " + fmt, self.address_string(), *args)

  # ---- helpers -----------------------------------------------------------

  def _send(self, status: int, body: bytes, ctype: str,
            cache: str = "no-store") -> None:
    self.send_response(status)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", cache)
    self.send_header("Referrer-Policy", "no-referrer")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(body)

  def _json(self, status: int, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    self._send(status, body, "application/json; charset=utf-8")

  def _error(self, status: int, message: str) -> None:
    self._json(status, {"error": message})

  def _token_ok(self, query: dict[str, list[str]]) -> bool:
    expected = self.server.token
    if not expected:
      return True
    provided = self.headers.get("X-Sidecar-Token") or (query.get("token") or [""])[0]
    if not provided:
      return False
    try:
      return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except (TypeError, UnicodeError):
      return False

  def _conv_id(self, query: dict[str, list[str]]) -> str | None:
    cid = (query.get("id") or [""])[0].strip()
    return cid if trajectory.is_valid_conversation_id(cid) else None

  def _read_body(self) -> dict[str, Any]:
    length = int(self.headers.get("Content-Length") or 0)
    if length < 0 or length > MAX_BODY:
      raise ValueError("invalid body size")
    raw = self.rfile.read(length) if length else b""
    data = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(data, dict):
      raise ValueError("body must be a JSON object")
    return data

  # ---- verbs -------------------------------------------------------------

  def do_HEAD(self) -> None:  # noqa: N802
    self.do_GET()

  def do_GET(self) -> None:  # noqa: N802
    parsed = urllib.parse.urlparse(self.path)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    try:
      if path in STATIC_FILES:
        name, ctype = STATIC_FILES[path]
        try:
          body = (STATIC_DIR / name).read_bytes()
        except OSError:
          self._error(404, "asset missing")
          return
        self._send(200, body, ctype, cache="no-cache")
        return
      if path == "/healthz":
        self._json(200, {"ok": True, "version": VERSION})
        return
      if not path.startswith("/api/"):
        self._error(404, "not found")
        return
      if not self._token_ok(query):
        self._error(401, "invalid or missing sidecar token")
        return
      self._api_get(path, query)
    except (BrokenPipeError, ConnectionResetError):
      self.close_connection = True
    except Exception as err:  # pylint: disable=broad-exception-caught
      logger.exception("GET %s failed", path)
      self._error(500, f"internal error: {type(err).__name__}")

  def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
    store = self.server.store
    if path == "/api/conversations":
      try:
        limit = max(1, min(300, int((query.get("limit") or ["60"])[0])))
      except ValueError:
        limit = 60
      self._json(200, {"conversations": trajectory.list_conversations(
          store.app_dirs, limit=limit)})
      return
    if path == "/api/config":
      self._json(200, {
          "version": VERSION,
          "app_dirs": [str(p) for p in store.app_dirs],
          "agentapi": bool(self.server.agentapi),
      })
      return

    conv_id = self._conv_id(query)
    if conv_id is None:
      self._error(400, "missing or invalid conversation id")
      return

    if path == "/api/meta":
      conv_dir = store.conv_dir(conv_id)
      if conv_dir is None:
        self._error(404, "conversation not found")
        return
      self._json(200, trajectory.transcript_meta(conv_dir))
      return
    if path == "/api/trajectory":
      model = store.load(conv_id)
      if model is None:
        self._error(404, "conversation not found")
        return
      self._json(200, model)
      return
    if path == "/api/step":
      try:
        idx = int((query.get("i") or [""])[0])
      except ValueError:
        self._error(400, "missing or invalid step index")
        return
      detail = store.step_detail(conv_id, idx)
      if detail is None:
        self._error(404, "step not found")
        return
      self._json(200, detail)
      return
    if path == "/api/subagents":
      tree = store.subagent_tree(conv_id)
      if tree is None:
        self._error(404, "conversation not found")
        return
      self._json(200, tree)
      return
    if path == "/api/annotations":
      self._json(200, {"annotations": self.server.annotations.get(conv_id)})
      return
    if path == "/api/export":
      model = store.load(conv_id)
      if model is None:
        self._error(404, "conversation not found")
        return
      md = trajectory.export_markdown(model, self.server.annotations.get(conv_id))
      self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8")
      return
    self._error(404, "unknown endpoint")

  def do_POST(self) -> None:  # noqa: N802
    parsed = urllib.parse.urlparse(self.path)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    try:
      if not self._token_ok(query):
        self._error(401, "invalid or missing sidecar token")
        return
      try:
        body = self._read_body()
      except (ValueError, UnicodeDecodeError) as err:
        self._error(400, f"bad request: {err}")
        return
      self._api_post(path, body)
    except (BrokenPipeError, ConnectionResetError):
      self.close_connection = True
    except subprocess.TimeoutExpired:
      self._error(504, "agentapi timed out")
    except Exception as err:  # pylint: disable=broad-exception-caught
      logger.exception("POST %s failed", path)
      self._error(500, f"internal error: {type(err).__name__}: {err}")

  def _api_post(self, path: str, body: dict[str, Any]) -> None:
    if path == "/api/annotations":
      conv_id = str(body.get("id") or "")
      if not trajectory.is_valid_conversation_id(conv_id):
        self._error(400, "missing or invalid conversation id")
        return
      step = str(body.get("step") if body.get("step") is not None else "_")
      if step != "_" and not step.lstrip("-").isdigit():
        self._error(400, "step must be an integer or '_'")
        return
      action = body.get("action") or "add"
      try:
        if action == "add":
          conv = self.server.annotations.add(conv_id, step, str(body.get("note") or ""))
        elif action == "delete":
          conv = self.server.annotations.delete(conv_id, step, int(body.get("index", -1)))
        else:
          self._error(400, "unknown action")
          return
      except ValueError as err:
        self._error(400, str(err))
        return
      self._json(200, {"annotations": conv})
      return

    if path == "/api/fork-prompt":
      conv_id = str(body.get("id") or "")
      model = self.server.store.load(conv_id) if trajectory.is_valid_conversation_id(conv_id) else None
      if model is None:
        self._error(404, "conversation not found")
        return
      try:
        step = int(body.get("step"))
      except (TypeError, ValueError):
        self._error(400, "missing or invalid step")
        return
      self._json(200, {"prompt": trajectory.fork_prompt(
          model, step, str(body.get("guidance") or ""))})
      return

    # Agent API proxies used by the window.sidecar bridge.
    if path == "/_sidecar/send-message":
      msg = body.get("message")
      conv_id = body.get("conversationId") or os.environ.get(
          "ANTIGRAVITY_SIDECAR_CONVERSATION_ID")
      if not msg or not conv_id:
        self._error(400, "message and conversationId are required")
        return
      self._json(200, run_agentapi(self.server.agentapi,
                                   ["send-message", str(conv_id), str(msg)],
                                   body.get("projectId")))
      return
    if path == "/_sidecar/new-conversation":
      msg = body.get("message")
      if not msg:
        self._error(400, "message is required")
        return
      args = ["new-conversation"]
      if body.get("title"):
        args += ["--title", str(body["title"])]
      args.append(str(msg))
      self._json(200, run_agentapi(self.server.agentapi, args, body.get("projectId")))
      return
    if path == "/_sidecar/get-conversation-metadata":
      conv_id = body.get("conversationId")
      if not conv_id:
        self._error(400, "conversationId is required")
        return
      self._json(200, run_agentapi(self.server.agentapi,
                                   ["get-conversation-metadata", str(conv_id)]))
      return
    self._error(404, "unknown endpoint")


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def resolve_data_dir() -> pathlib.Path:
  env = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
  if env:
    return pathlib.Path(env)
  return pathlib.Path.home() / ".gemini" / "sanjaya"


def make_server(host: str, port: int, app_dirs: list[pathlib.Path] | None = None,
                data_dir: pathlib.Path | None = None,
                token: str | None = None) -> SanjayaServer:
  app_dirs = app_dirs if app_dirs is not None else trajectory.default_app_dirs()
  data_dir = data_dir or resolve_data_dir()
  return SanjayaServer(
      (host, port), Handler,
      store=trajectory.TrajectoryStore(app_dirs),
      annotations=AnnotationStore(data_dir / "annotations.json"),
      token=token,
      agentapi=find_agentapi(app_dirs),
  )


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Sanjaya trajectory viewer sidecar")
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=None)
  args = parser.parse_args(argv)
  port = args.port
  if port is None:
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT") or 8765)
  token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN") or None
  server = make_server(args.host, port, token=token)
  logger.info("Sanjaya %s listening on http://%s:%d (app dirs: %s, agentapi: %s)",
              VERSION, args.host, server.server_address[1],
              ", ".join(str(p) for p in server.store.app_dirs),
              server.agentapi or "not found")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    server.server_close()
  return 0


if __name__ == "__main__":
  sys.exit(main())
