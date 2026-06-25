#!/usr/bin/env python3
"""
Vertex AI / Gemini model health checker (global location).

Two independent auth surfaces are supported:

  1. Service-account JSON  -> Vertex AI  (aiplatform.googleapis.com, location=global)
  2. API key               -> Gemini API (generativelanguage.googleapis.com)

The script:
  * lists publisher models the endpoint reports as available, and
  * actively probes a candidate set with a tiny generateContent call,
    reporting HTTP status + latency so you can see which models actually
    answer (healthy) vs 404 (not enabled) / 403 (no access) / 429 (quota).

No third-party Python packages required. JWT is signed with the local
`openssl` binary, requests go out via stdlib urllib.

Usage:
    python3 vertex_health_check.py \
        --sa ./agon-development-499205-5a7716960fea.json

    # API-key (Gemini Developer API) surface as well / instead:
    python3 vertex_health_check.py --api-key "$GEMINI_API_KEY"

    # discovery only, skip the live generateContent probes:
    python3 vertex_health_check.py --sa ./key.json --list-only
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- candidate models to actively probe ---------------------------------
# Adjust freely. These are the publisher model ids as used by Vertex global.
VERTEX_CANDIDATES = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
# Gemini Developer API ids are prefixed "models/..."; we add the prefix later.
GEMINI_CANDIDATES = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]

TINY_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
    "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
}

# Image-generation probes. Gemini "*-image" models answer on generateContent
# with an IMAGE response modality; Imagen models use the :predict endpoint.
IMAGE_PROMPT = "a small red circle on a white background"
GEMINI_IMAGE_BODY = {
    "contents": [{"role": "user", "parts": [{"text": IMAGE_PROMPT}]}],
    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
}
IMAGEN_BODY = {
    "instances": [{"prompt": IMAGE_PROMPT}],
    "parameters": {"sampleCount": 1},
}
# Imagen ids aren't returned by the global Model Garden list, so probe a
# curated set directly.
IMAGEN_CANDIDATES = [
    "imagen-4.0-ultra-generate-001",
    "imagen-4.0-generate-001",
    "imagen-4.0-fast-generate-001",
    "imagen-3.0-generate-002",
    "imagen-3.0-fast-generate-001",
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def http(method, url, headers=None, body=None, timeout=30):
    """Return (status_code, parsed_json_or_text, latency_ms)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            ms = int((time.time() - start) * 1000)
            try:
                return r.status, json.loads(raw), ms
            except json.JSONDecodeError:
                return r.status, raw, ms
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        ms = int((time.time() - start) * 1000)
        try:
            return e.code, json.loads(raw), ms
        except json.JSONDecodeError:
            return e.code, raw, ms
    except urllib.error.URLError as e:
        ms = int((time.time() - start) * 1000)
        return 0, f"URLError: {e.reason}", ms


# ---- service account -> OAuth2 access token -----------------------------
def get_access_token(sa: dict) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claim = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": sa["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (_b64url(json.dumps(header).encode()) + "." +
                     _b64url(json.dumps(claim).encode())).encode("ascii")

    # Sign with openssl using the private key (kept in a 0600 temp file).
    keyfd, keypath = tempfile.mkstemp(suffix=".pem")
    try:
        os.fchmod(keyfd, 0o600)
        os.write(keyfd, sa["private_key"].encode())
        os.close(keyfd)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", keypath],
            input=signing_input, capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("openssl signing failed: " +
                               proc.stderr.decode("utf-8", "replace"))
        signature = proc.stdout
    finally:
        os.remove(keypath)

    assertion = signing_input.decode() + "." + _b64url(signature)
    form = ("grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer"
            "&assertion=" + urllib.parse.quote(assertion))
    req = urllib.request.Request(
        sa["token_uri"], data=form.encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read())
    return tok["access_token"]


def status_label(code):
    return {
        200: "HEALTHY",
        400: "BAD REQUEST",
        401: "UNAUTHENTICATED",
        403: "NO ACCESS (403)",
        404: "NOT FOUND (404)",
        429: "QUOTA (429)",
        0:   "NETWORK ERR",
    }.get(code, f"HTTP {code}")


def err_detail(payload):
    if isinstance(payload, dict):
        e = payload.get("error", payload)
        msg = e.get("message") if isinstance(e, dict) else str(e)
        return (msg or "")[:90]
    return str(payload)[:90]


# ---- Vertex AI (service account, global location) -----------------------
def check_vertex(sa, list_only):
    project = sa["project_id"]
    base = "https://aiplatform.googleapis.com/v1"
    print(f"\n=== VERTEX AI  project={project}  location=global ===")
    token = get_access_token(sa)
    print("OAuth token acquired OK")
    H = {"Authorization": f"Bearer {token}",
         "Content-Type": "application/json"}

    # Discovery: publisher models known to Model Garden (v1beta1 only).
    code, body, _ = http(
        "GET",
        "https://aiplatform.googleapis.com/v1beta1/"
        "publishers/google/models?pageSize=200",
        headers=H,
    )
    discovered = []
    if code == 200 and isinstance(body, dict):
        discovered = sorted(
            m.get("name", "").split("/")[-1]
            for m in body.get("publisherModels", [])
            if m.get("name", "").split("/")[-1].startswith("gemini")
        )
        print(f"\nModel Garden lists {len(discovered)} gemini* publisher "
              f"models:")
        for n in discovered:
            print(f"  - {n}")
    else:
        print(f"\n[discovery] list returned {status_label(code)}: "
              f"{err_detail(body)}")

    if list_only:
        return

    # Probe text-generation models: prefer the discovered ids (they carry the
    # exact version suffix the endpoint requires), skipping non-generateContent
    # variants. Fall back to the static candidate list if discovery failed.
    SKIP = ("-tts", "-image", "embedding", "-live", "native-audio")
    probe = [m for m in discovered if not any(s in m for s in SKIP)] \
        or VERTEX_CANDIDATES

    print("\nLive generateContent probe (global endpoint):")
    print(f"  {'model':<34} {'status':<18} {'ms':>6}  detail")
    for m in probe:
        url = (f"{base}/projects/{project}/locations/global/"
               f"publishers/google/models/{m}:generateContent")
        code, body, ms = http("POST", url, headers=H, body=TINY_BODY)
        detail = "" if code == 200 else err_detail(body)
        print(f"  {m:<34} {status_label(code):<18} {ms:>6}  {detail}")


# ---- Vertex AI image generation (service account, global location) ------
def _has_image(resp):
    """True if a generateContent response carries inline image bytes."""
    if not isinstance(resp, dict):
        return False
    for c in resp.get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            if "inlineData" in p or "inline_data" in p:
                return True
    return False


def check_vertex_images(sa):
    project = sa["project_id"]
    base = "https://aiplatform.googleapis.com/v1"
    print(f"\n=== VERTEX AI IMAGE GEN  project={project}  location=global ===")
    token = get_access_token(sa)
    H = {"Authorization": f"Bearer {token}",
         "Content-Type": "application/json"}

    # Discover Gemini image models from Model Garden (ids contain "-image").
    code, body, _ = http(
        "GET",
        "https://aiplatform.googleapis.com/v1beta1/"
        "publishers/google/models?pageSize=200",
        headers=H,
    )
    gemini_image = []
    if code == 200 and isinstance(body, dict):
        gemini_image = sorted(
            m.get("name", "").split("/")[-1]
            for m in body.get("publisherModels", [])
            if "-image" in m.get("name", "").split("/")[-1]
        )

    print(f"\n  {'model':<34} {'status':<18} {'ms':>6}  img?  detail")
    # Gemini native image models -> generateContent + IMAGE modality.
    for m in gemini_image:
        url = (f"{base}/projects/{project}/locations/global/"
               f"publishers/google/models/{m}:generateContent")
        code, resp, ms = http("POST", url, headers=H,
                              body=GEMINI_IMAGE_BODY, timeout=120)
        img = "yes" if _has_image(resp) else "no"
        detail = "" if code == 200 else err_detail(resp)
        print(f"  {m:<34} {status_label(code):<18} {ms:>6}  {img:<4}  {detail}")

    # Imagen models -> :predict (returns base64 in predictions[].bytesBase64).
    for m in IMAGEN_CANDIDATES:
        url = (f"{base}/projects/{project}/locations/global/"
               f"publishers/google/models/{m}:predict")
        code, resp, ms = http("POST", url, headers=H,
                              body=IMAGEN_BODY, timeout=120)
        img = "no"
        if isinstance(resp, dict) and resp.get("predictions"):
            img = "yes"
        detail = "" if code == 200 else err_detail(resp)
        print(f"  {m:<34} {status_label(code):<18} {ms:>6}  {img:<4}  {detail}")


# ---- Gemini Developer API (API key) -------------------------------------
def check_gemini(api_key, list_only):
    base = "https://generativelanguage.googleapis.com/v1beta"
    print("\n=== GEMINI DEVELOPER API  (API key) ===")
    code, body, _ = http("GET", f"{base}/models?key={api_key}&pageSize=200")
    if code == 200 and isinstance(body, dict):
        names = [m.get("name", "").split("/")[-1]
                 for m in body.get("models", [])]
        print(f"API key can see {len(names)} models (showing gemini*):")
        for n in sorted(x for x in names if x.startswith("gemini")):
            print(f"  - {n}")
    else:
        print(f"[discovery] list returned {status_label(code)}: "
              f"{err_detail(body)}")
        if code in (400, 401, 403):
            return  # key invalid -> probing is pointless

    if list_only:
        return

    print("\nLive generateContent probe:")
    print(f"  {'model':<26} {'status':<18} {'ms':>6}  detail")
    for m in GEMINI_CANDIDATES:
        url = f"{base}/models/{m}:generateContent?key={api_key}"
        code, body, ms = http("POST", url,
                              headers={"Content-Type": "application/json"},
                              body=TINY_BODY)
        detail = "" if code == 200 else err_detail(body)
        print(f"  {m:<26} {status_label(code):<18} {ms:>6}  {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sa", help="path to service-account JSON")
    ap.add_argument("--api-key", help="Gemini API key (or env GEMINI_API_KEY)")
    ap.add_argument("--list-only", action="store_true",
                    help="discovery only; skip live generateContent probes")
    ap.add_argument("--images", action="store_true",
                    help="probe image-generation models (Gemini *-image + Imagen)")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY") \
        or os.environ.get("GOOGLE_API_KEY")

    if not args.sa and not api_key:
        ap.error("provide --sa <json> and/or --api-key/GEMINI_API_KEY")

    if args.sa:
        with open(args.sa) as f:
            sa = json.load(f)
        if args.images:
            try:
                check_vertex_images(sa)
            except Exception as e:
                print(f"[vertex-images] FAILED: {e}", file=sys.stderr)
        else:
            try:
                check_vertex(sa, args.list_only)
            except Exception as e:
                print(f"[vertex] FAILED: {e}", file=sys.stderr)

    if api_key:
        try:
            check_gemini(api_key, args.list_only)
        except Exception as e:
            print(f"[gemini] FAILED: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
