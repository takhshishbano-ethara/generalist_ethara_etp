#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone fake Vertex AI / Gemini generateContent server (stdlib only).

PURPOSE
-------
A zero-dependency HTTP server that returns responses shaped EXACTLY like the
Vertex ``:generateContent`` envelope that services/vertex.py parses, so an
end-to-end run against a LIVE Odoo server can exercise the real LLM code paths
(extract_skills / generate_questions / scoring / image render) WITHOUT a Vertex
project, credentials, or budget.

For UNIT / INTEGRATION tests, prefer the patch-based mock in
``tests/vertex_fixtures.py`` (``mock_vertex`` / ``score_payload`` / ...). It is
simpler, deterministic, and needs no network. This server exists only for the
"drive a real browser/portal against a real Odoo, with a fake brain" scenario.

=========================================================================
IMPORTANT LIMITATION — READ BEFORE USING (verified against services/vertex.py)
=========================================================================
services/vertex.py:_gemini_request (vertex.py:195-232) HARD-CODES the Google
hostnames and builds the URL itself. There is NO ir.config_parameter for a
custom base URL / host. The three branches it can produce are:

  bearer token set  -> https://<location>-aiplatform.googleapis.com/...    (regional)
                       https://aiplatform.googleapis.com/...               (global)
  api_key "AQ."     -> https://aiplatform.googleapis.com/v1/publishers/...
  api_key otherwise -> https://generativelanguage.googleapis.com/v1beta/...

Because the host is fixed AND https, you cannot point the module at this server
with a config parameter alone. To route the module here you must intercept the
hostname at the machine/network level, e.g.:

  1. Add a hosts entry so the Google host resolves to this server, e.g.
        127.0.0.1  generativelanguage.googleapis.com
     (or aiplatform.googleapis.com), then run THIS server on 443 with a TLS
     cert your Odoo host trusts (httpx verifies TLS). Self-signed certs need
     SSL_CERT_FILE / REQUESTS_CA_BUNDLE (or httpx verify=False, which the module
     does NOT set) to be trusted.
  2. OR run behind a local reverse proxy / mitmproxy that terminates the Google
     host TLS and forwards to this plain-HTTP server.

Given that friction, the honest recommendation is: use the patch-based mock for
CI and normal testing; use THIS server only for a manual live-server smoke test
where you control DNS/TLS on the Odoo host.

CONFIG PARAMETERS TO SET ON THE ODOO SERVER (ir.config_parameter keys, exactly)
-------------------------------------------------------------------------------
These are the only Vertex-related keys the module reads (vertex.py:108-192):

  etp_assessment_pro.vertex_api_key             set to a NON-"AIza" value so the
                                                api_key branch is taken and NO
                                                bearer/service-account path runs.
                                                Any dummy like "fake-key" works;
                                                do NOT prefix with "AQ." unless
                                                you also intercept
                                                aiplatform.googleapis.com. With a
                                                plain key the module targets
                                                generativelanguage.googleapis.com
                                                — intercept THAT host.
  etp_assessment_pro.vertex_model               a model name; the URL becomes
                                                .../models/<model>:generateContent
                                                (this server ignores the model).
  etp_assessment_pro.vertex_project_id          "" (unused on the api_key branch)
  etp_assessment_pro.vertex_location            "global" (unused on api_key branch)
  etp_assessment_pro.vertex_access_token        "" (MUST be empty or the bearer
                                                branch targets aiplatform host)
  etp_assessment_pro.vertex_service_account_json "" (MUST be empty/PLACEHOLDER so
                                                no token minting is attempted)

NB: any value containing "PLACEHOLDER" is treated as unset (vertex.py:_param,
103-105), so a leftover placeholder is safely ignored.

WHAT THIS SERVER RETURNS
------------------------
On ANY POST whose path ends with ``:generateContent`` it returns a 200 with the
minimal-but-faithful envelope vertex.py reads:

  {
    "candidates": [
      {"content": {"parts": [{"text": "<json string>"}]},
       "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": ..,
                      "candidatesTokenCount": ..,
                      "thoughtsTokenCount": 0},
    "promptFeedback": {}
  }

The ``text`` part is chosen by INSPECTING the request body so the response is
valid for whichever operation Odoo is running:
  * scoring   (systemInstruction/user text mentions grading + "items") ->
               a JSON array of per-item {"id","score",...} echoing every item id
               (matches scoring._parse_results / _store_scored, scoring.py:229-293).
  * questions ("Generate exactly" / "SKILL TO TEST" in the user text) ->
               a JSON array of one valid mcq item
               (matches vertex._validate_question_item, vertex.py:901).
  * skills    (anything else) -> a JSON array of two skills
               (matches vertex.extract_skills, vertex.py:793-857).
For an image request (generationConfig.responseModalities contains "IMAGE", as
generate_image sends at vertex.py:520-523) it returns a candidate part with
``inlineData`` carrying a tiny 1x1 PNG (matches generate_image, vertex.py:542-550).

These payloads intentionally mirror tests/vertex_fixtures.py so behaviour is the
same whether you patch or run the server.

RUN
---
    python3 tests/load/fake_vertex.py            # binds 127.0.0.1:8799
    python3 tests/load/fake_vertex.py 0.0.0.0 443  # host:port override

This file has NO "test_" prefix and is under tests/load/ (no __init__.py), so
Odoo's test runner never imports it. It is stdlib-only (http.server, json).
"""
import base64
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --------------------------------------------------------------------------- #
# Canned payload bodies (kept in lockstep with tests/vertex_fixtures.py).
# --------------------------------------------------------------------------- #
SKILLS_ARRAY = [
    {"name": "Refund Policy Application",
     "description": "Apply the refund decision tree to edge cases.",
     "tags": "refunds,policy", "question_type": "mcq", "medium": "text",
     "question_count": 5, "time_minutes": 10, "difficulty": "medium"},
    {"name": "Customer Tone Calibration",
     "description": "Match the brand voice in a written reply.",
     "tags": "tone,writing", "question_type": "subjective_rubric",
     "medium": "text", "question_count": 3, "time_minutes": 20,
     "difficulty": "hard"},
]

MCQ_QUESTION_ARRAY = [{
    "name": "Refund within 24h",
    "prompt": "A customer requests a refund 12 hours after purchase. The stated "
              "window is 24 hours. What is the correct action?",
    "question_type": "mcq", "difficulty": "easy",
    "options": ["Issue the refund", "Deny it", "Escalate"],
    "correct_answer": 0,
}]

# A 1x1 transparent PNG (base64), returned for image generateContent calls.
_ONE_PX_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _detect_operation(system_prompt, user_text):
    """Classify the call the same way tests/vertex_fixtures._routing_side_effect
    does: scoring vs question-gen vs skills."""
    st = (system_prompt or "").lower()
    ut = (user_text or "")
    if ("grader" in st or "Grade every candidate answer" in ut
            or '"items"' in ut):
        return "scoring"
    if "Generate exactly" in ut or "SKILL TO TEST" in ut:
        return "questions"
    return "skills"


def _scoring_text_for(user_text):
    """Build the grader JSON array, echoing every submitted item id (the scorer
    matches results to responses by id — scoring.py:384-398)."""
    ids = []
    try:
        start = user_text.index("{")
        payload = json.loads(user_text[start:])
        for it in payload.get("items") or []:
            if isinstance(it, dict) and it.get("id") is not None:
                ids.append(it["id"])
    except Exception:
        ids = []
    results = [{
        "id": int(rid) if str(rid).lstrip("-").isdigit() else rid,
        "score": 85,
        "rubric_source": "generated",
        "gate": "none",
        "reference_answer": "A meets the bar because ...",
        "reasoning": "Checklist items satisfied by the answer.",
        "feedback": "Solid, evidence-backed answer.",
        "flags": [],
    } for rid in ids]
    return json.dumps(results)


def _extract_prompt_texts(body):
    """Pull the system instruction text and the user text out of the Vertex
    request body (systemInstruction.parts[].text + contents[].parts[].text —
    the shape vertex._call_vertex sends, vertex.py:420-424)."""
    system_prompt = ""
    for p in ((body.get("systemInstruction") or {}).get("parts") or []):
        if isinstance(p, dict) and p.get("text"):
            system_prompt += p["text"]
    user_text = ""
    for content in body.get("contents") or []:
        for p in (content.get("parts") or []):
            if isinstance(p, dict) and p.get("text"):
                user_text += p["text"]
    return system_prompt, user_text


def _is_image_request(body):
    """generate_image sends responseModalities including IMAGE (vertex.py:520-523)."""
    gc = body.get("generationConfig") or {}
    mods = gc.get("responseModalities") or []
    return any(str(m).upper() == "IMAGE" for m in mods)


def _text_envelope(text):
    """The generateContent JSON envelope carrying one text part."""
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": text}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": max(1, len(text) // 4),
            "candidatesTokenCount": max(1, len(text) // 4),
            "thoughtsTokenCount": 0,
        },
        "promptFeedback": {},
    }


def _image_envelope():
    """Envelope carrying one inlineData image part (generate_image reads
    candidates[0].content.parts[].inlineData.data, vertex.py:542-550)."""
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [{
                "inlineData": {"mimeType": "image/png", "data": _ONE_PX_PNG_B64},
            }]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": 8,
            "candidatesTokenCount": 0,
            "thoughtsTokenCount": 0,
        },
        "promptFeedback": {},
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeVertex/1.0"

    def _send_json(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        # A trivial health probe so you can curl the server to confirm it is up.
        if self.path in ("/", "/health", "/healthz"):
            self._send_json({"ok": True, "service": "fake-vertex"})
            return
        self._send_json({"error": {"code": 404, "message": "not found"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {}

        # Only handle the generateContent endpoint the module calls; the model
        # name in the path is ignored (vertex builds .../models/<m>:generateContent).
        if not re.search(r":generateContent/?$", self.path):
            self._send_json(
                {"error": {"code": 404,
                           "message": "fake-vertex only serves :generateContent"}},
                404)
            return

        if _is_image_request(body):
            self._send_json(_image_envelope())
            return

        system_prompt, user_text = _extract_prompt_texts(body)
        op = _detect_operation(system_prompt, user_text)
        if op == "scoring":
            text = _scoring_text_for(user_text)
        elif op == "questions":
            text = json.dumps(MCQ_QUESTION_ARRAY)
        else:
            text = json.dumps(SKILLS_ARRAY)
        self._send_json(_text_envelope(text))

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("[fake-vertex] %s - %s\n" % (
            self.address_string(), fmt % args))


def main(argv):
    host = argv[1] if len(argv) > 1 else "127.0.0.1"
    port = int(argv[2]) if len(argv) > 2 else 8799
    # Sanity-check the canned image payload decodes (fail fast on a typo).
    base64.b64decode(_ONE_PX_PNG_B64)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    sys.stderr.write(
        "[fake-vertex] listening on http://%s:%d  (POST .../:generateContent)\n"
        % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[fake-vertex] shutting down\n")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main(sys.argv)
