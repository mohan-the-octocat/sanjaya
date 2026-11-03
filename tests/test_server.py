"""HTTP-level tests for the Sanjaya sidecar server."""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import fixtures  # noqa: F401  (sets sys.path)
from fixtures import CHILD_ID, PARENT_ID, make_app_dir
import server

TOKEN = "test-token"


class ServerTest(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.tmp = tempfile.TemporaryDirectory()
    tmp = pathlib.Path(cls.tmp.name)
    cls.app = make_app_dir(tmp)
    cls.srv = server.make_server("127.0.0.1", 0, app_dirs=[cls.app],
                                 data_dir=tmp / "data", token=TOKEN)
    cls.srv.agentapi = None  # never shell out from tests
    cls.port = cls.srv.server_address[1]
    cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
    cls.thread.start()

  @classmethod
  def tearDownClass(cls):
    cls.srv.shutdown()
    cls.srv.server_close()
    cls.tmp.cleanup()

  def req(self, path, *, method="GET", body=None, token=TOKEN):
    url = f"http://127.0.0.1:{self.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
      r.add_header("X-Sidecar-Token", token)
    if data is not None:
      r.add_header("Content-Type", "application/json")
    try:
      with urllib.request.urlopen(r, timeout=5) as resp:
        raw = resp.read()
        return resp.status, resp.headers.get("Content-Type", ""), raw
    except urllib.error.HTTPError as err:
      return err.code, err.headers.get("Content-Type", ""), err.read()

  def json(self, path, **kw):
    status, _, raw = self.req(path, **kw)
    return status, json.loads(raw.decode())

  # ---- static ------------------------------------------------------------

  def test_static_assets_load_without_token(self):
    for path, ctype in (("/", "text/html"), ("/index.html?token=x&conversationId=y", "text/html"),
                        ("/app.js", "javascript"), ("/styles.css", "text/css"),
                        ("/preload.js", "javascript"), ("/logo.svg", "image/svg")):
      status, got, body = self.req(path, token=None)
      self.assertEqual(status, 200, path)
      self.assertIn(ctype, got, path)
      self.assertTrue(body, path)

  def test_index_references_bridge(self):
    _, _, body = self.req("/", token=None)
    self.assertIn(b'src="/preload.js"', body)

  def test_healthz(self):
    status, data = self.json("/healthz", token=None)
    self.assertEqual(status, 200)
    self.assertTrue(data["ok"])

  # ---- auth --------------------------------------------------------------

  def test_api_requires_token(self):
    status, _ = self.json("/api/conversations", token=None)
    self.assertEqual(status, 401)
    status, _ = self.json("/api/conversations", token="wrong")
    self.assertEqual(status, 401)

  def test_query_token_fallback(self):
    status, _ = self.json(f"/api/conversations?token={TOKEN}", token=None)
    self.assertEqual(status, 200)

  def test_post_requires_token(self):
    status, _ = self.json("/api/annotations", method="POST",
                          body={"id": PARENT_ID, "note": "x"}, token=None)
    self.assertEqual(status, 401)

  # ---- read API ----------------------------------------------------------

  def test_conversations(self):
    status, data = self.json("/api/conversations")
    self.assertEqual(status, 200)
    self.assertEqual([c["id"] for c in data["conversations"]], [PARENT_ID, CHILD_ID])

  def test_trajectory_and_meta(self):
    status, model = self.json(f"/api/trajectory?id={PARENT_ID}")
    self.assertEqual(status, 200)
    self.assertEqual(model["stats"]["tool_calls"], 5)
    status, meta = self.json(f"/api/meta?id={PARENT_ID}")
    self.assertEqual(status, 200)
    self.assertEqual(meta["version"], model["version"])

  def test_invalid_and_missing_ids(self):
    self.assertEqual(self.json("/api/trajectory?id=../../etc")[0], 400)
    self.assertEqual(self.json("/api/trajectory")[0], 400)
    self.assertEqual(self.json("/api/trajectory?id=33333333-3333-4333-8333-333333333333")[0], 404)

  def test_step_detail(self):
    status, d = self.json(f"/api/step?id={PARENT_ID}&i=3")
    self.assertEqual(status, 200)
    self.assertIn("cc_library", d["content"])
    self.assertEqual(self.json(f"/api/step?id={PARENT_ID}&i=abc")[0], 400)
    self.assertEqual(self.json(f"/api/step?id={PARENT_ID}&i=999")[0], 404)

  def test_subagents(self):
    status, tree = self.json(f"/api/subagents?id={PARENT_ID}")
    self.assertEqual(status, 200)
    self.assertEqual(tree["children"][0]["id"], CHILD_ID)

  def test_export(self):
    status, ctype, body = self.req(f"/api/export?id={PARENT_ID}")
    self.assertEqual(status, 200)
    self.assertIn("text/markdown", ctype)
    self.assertIn(b"# Trajectory: Fix the flaky build", body)

  def test_unknown_routes(self):
    self.assertEqual(self.json("/nope", token=None)[0], 404)
    self.assertEqual(self.json(f"/api/nope?id={PARENT_ID}")[0], 404)

  # ---- write API ---------------------------------------------------------

  def test_annotations_roundtrip(self):
    status, data = self.json("/api/annotations", method="POST",
                             body={"id": PARENT_ID, "step": 4, "note": "flaky"})
    self.assertEqual(status, 200)
    self.assertEqual(data["annotations"]["4"][0]["note"], "flaky")
    status, data = self.json("/api/annotations", method="POST",
                             body={"id": PARENT_ID, "note": "whole run"})
    self.assertEqual(data["annotations"]["_"][0]["note"], "whole run")
    status, data = self.json(f"/api/annotations?id={PARENT_ID}")
    self.assertIn("4", data["annotations"])
    status, data = self.json("/api/annotations", method="POST",
                             body={"id": PARENT_ID, "step": 4, "action": "delete", "index": 0})
    self.assertEqual(status, 200)
    self.assertNotIn("4", data["annotations"])
    status, data = self.json("/api/annotations", method="POST",
                             body={"id": PARENT_ID, "step": "_", "action": "delete", "index": 0})
    self.assertEqual(status, 200)

  def test_annotation_validation(self):
    self.assertEqual(self.json("/api/annotations", method="POST",
                               body={"id": PARENT_ID, "note": "  "})[0], 400)
    self.assertEqual(self.json("/api/annotations", method="POST",
                               body={"id": "../x", "note": "a"})[0], 400)
    self.assertEqual(self.json("/api/annotations", method="POST",
                               body={"id": PARENT_ID, "step": "abc", "note": "a"})[0], 400)
    self.assertEqual(self.json("/api/annotations", method="POST",
                               body={"id": PARENT_ID, "step": 1, "action": "delete", "index": 7})[0], 400)
    status, _, _ = self.req("/api/annotations", method="POST", body=[1, 2])
    self.assertEqual(status, 400)

  def test_fork_prompt(self):
    status, data = self.json("/api/fork-prompt", method="POST",
                             body={"id": PARENT_ID, "step": 6, "guidance": "use bazel"})
    self.assertEqual(status, 200)
    self.assertIn("use bazel", data["prompt"])
    self.assertEqual(self.json("/api/fork-prompt", method="POST",
                               body={"id": PARENT_ID, "step": "x"})[0], 400)

  def test_agent_proxy_reports_missing_cli(self):
    status, data = self.json("/_sidecar/send-message", method="POST",
                             body={"conversationId": PARENT_ID, "message": "hi"})
    self.assertEqual(status, 500)
    self.assertIn("agentapi", data["error"])
    self.assertEqual(self.json("/_sidecar/send-message", method="POST", body={})[0], 400)


class AnnotationStoreTest(unittest.TestCase):

  def test_persists_across_instances(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = pathlib.Path(tmp) / "a" / "notes.json"
      server.AnnotationStore(path).add("c1", "3", "hello")
      self.assertEqual(server.AnnotationStore(path).get("c1")["3"][0]["note"], "hello")

  def test_corrupt_file_is_treated_as_empty(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = pathlib.Path(tmp) / "notes.json"
      path.write_text("{not json")
      self.assertEqual(server.AnnotationStore(path).get("c1"), {})


if __name__ == "__main__":
  unittest.main()
