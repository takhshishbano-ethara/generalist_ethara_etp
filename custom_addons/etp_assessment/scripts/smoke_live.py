#!/usr/bin/env python3
"""ETP Assessment — LIVE smoke test for Vertex (text + image) + S3.

Run this the moment real creds are pasted into System Parameters. It exercises
the REAL Vertex Gemini text call, the REAL Vertex Imagen image call, the REAL
S3 upload, and confirms the resulting URL is publicly fetchable (the 403 trap).
Nothing is mocked. Rolls back any DB writes.

USAGE (from the addon repo root, with the venv):
  .venv/bin/python src/odoo-bin shell -c odoo.conf -d <db> --log-level=error \
      < custom_addons/etp_assessment/scripts/smoke_live.py

It prints PASS/FAIL per stage so you see exactly which link breaks.
"""
import json
import logging
logging.disable(logging.CRITICAL)

env = env  # noqa  (provided by odoo shell)
from odoo.addons.etp_assessment.services import (
    vertex_questions as bq,
    vertex_images as bi,
    s3_service as s3,
)

results = []
def stage(name, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"PASS {name}")
    except Exception as exc:
        results.append((name, False, str(exc)[:300]))
        print(f"FAIL {name}: {str(exc)[:300]}")


# ---- 0. config presence (fast fail with a clear message) ----
def _cfg():
    assert bq._param(env, "etp_assessment.vertex_project_id") or \
        bq._param(env, "etp_assessment.vertex_api_key"), \
        "Vertex not configured (need vertex_api_key OR vertex_access_token+project)"
stage("0. Vertex configured", _cfg)

# ---- 1. Vertex TEXT call (Gemini) — the question-generation transport ----
_text = {}
def _text_call():
    out = bq._call_vertex(
        env,
        "You are a test harness. Reply with a tiny JSON array.",
        'Return exactly: [{"ok": true}]',
        max_tokens=100, temperature=0.0)
    _text["raw"] = out
    assert out and len(out) > 1, "empty text response"
stage("1. Vertex Gemini text call", _text_call)

# ---- 2. Vertex IMAGE call (Imagen) — returns base64 ----
_img = {}
def _img_call():
    assert bi.is_configured(env), \
        "image model not configured (vertex_image_model + creds)"
    b64 = bi.generate_image_b64(
        env, "a plain red square on a white background, simple, flat")
    assert b64 and len(b64) > 100, "image base64 too small / empty"
    _img["b64"] = b64
stage("2. Vertex Imagen image call -> base64", _img_call)

# ---- 3. S3 upload of the generated image ----
_s3 = {}
def _s3_upload():
    assert s3.is_configured(env), \
        "S3 not configured (s3_bucket + access_key_id + secret_key)"
    url, key = s3.upload_image_b64(env, _img["b64"], key_hint="smoke")
    _s3["url"] = url
    _s3["key"] = key
    assert url.startswith("http"), f"unexpected url: {url}"
stage("3. S3 upload -> URL", _s3_upload)

# ---- 4. THE 403 TRAP: can a browser actually load that URL? ----
def _fetch():
    import httpx
    url = _s3["url"]
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        r = c.get(url)
    assert r.status_code == 200, (
        f"GET {url} -> HTTP {r.status_code}. If 403, the bucket objects are "
        f"NOT publicly readable — the candidate's browser will not show the "
        f"image. Fix the bucket read policy or front it with a CDN.")
    ctype = r.headers.get("content-type", "")
    assert "image" in ctype or len(r.content) > 100, \
        f"fetched but doesn't look like an image (content-type={ctype})"
stage("4. Generated image is publicly fetchable (no 403)", _fetch)

# ---- 5. cleanup the smoke object so we don't litter the bucket ----
def _cleanup():
    if _s3.get("key"):
        s3.delete_key(env, _s3["key"])
stage("5. Cleanup smoke object", _cleanup)

print()
passed = sum(1 for _, ok, _ in results if ok)
print(f"RESULT: {passed}/{len(results)} stages passed")
if passed < len(results):
    print("\nFAILURES (fix in order — each gates the next):")
    for name, ok, err in results:
        if not ok:
            print(f"  - {name}\n      {err}")
    print("\nNOTE: stage 4 (403) is the most common real failure — it means the "
          "upload worked but the bucket isn't browser-readable.")
else:
    print("\nALL GREEN — generation, upload, and display are live end-to-end. "
          "Generated question images will render in the candidate portal.")

env.cr.rollback()
