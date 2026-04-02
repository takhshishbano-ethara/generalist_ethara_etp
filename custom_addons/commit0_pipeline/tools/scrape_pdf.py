"""3-layer PDF generation fallback for spec scraping."""

import bz2
import hashlib
import logging
import os
import re
import urllib.request
import urllib.error
from collections import deque
from urllib.parse import urljoin, urlparse

_log = logging.getLogger(__name__)

_CSS = """\
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; font-size: 11pt; line-height: 1.6; color: #333; margin: 0; padding: 20px 40px; }
.page-section { page-break-before: always; padding-top: 10px; }
.page-section:first-child { page-break-before: avoid; }
.page-title { font-size: 20pt; color: #1a1a1a; border-bottom: 2px solid #ddd; padding-bottom: 8px; margin-bottom: 16px; }
h1,h2,h3,h4,h5,h6 { color: #1a1a1a; margin-top: 1.2em; margin-bottom: 0.6em; page-break-after: avoid; }
h2 { font-size: 16pt; border-bottom: 1px solid #eee; padding-bottom: 4px; }
h3 { font-size: 13pt; }
pre,code { font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 9pt; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px; padding: 12px; overflow-x: visible; white-space: pre-wrap; word-wrap: break-word; page-break-inside: avoid; }
code { background: #f0f0f0; padding: 2px 4px; border-radius: 3px; }
pre code { background: none; padding: 0; }
img { max-width: 100%; height: auto; display: block; margin: 10px 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 10pt; page-break-inside: avoid; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
a { color: #0366d6; text-decoration: none; }
blockquote { border-left: 4px solid #ddd; margin: 10px 0; padding: 8px 16px; color: #555; }
"""

_SKIP_PATH_SEGMENTS = (
    "/_sources/",
    "/_static/",
    "/_modules/",
    "/_images/",
    "/genindex",
    "/search",
    "/py-modindex",
    "/_downloads/",
    "/changelog",
)
_SKIP_EXTENSIONS = (".txt", ".json", ".xml", ".zip", ".gz", ".tar", ".whl", ".egg")

_LOCALE_RE = re.compile(r"/(?:en|de|fr|es|ja|zh|ko|pt|ru|it)/")
_OLD_VERSION_RE = re.compile(r"/(?:v?\d+\.\d+|latest|stable|dev|master)/")

_MAIN_SELECTORS = (
    "[role='main']",
    "main",
    "article",
    ".document",
    ".body",
    ".rst-content",
    ".wy-nav-content",
    ".content",
    ".md-content",
    "#content",
    ".main-content",
)

_JUNK_SELECTOR = (
    "nav, .sidebar, .nav-side, .wy-nav-side, .wy-side-scroll, "
    ".rst-versions, header, footer, .headerlink, .mobile-header, "
    ".toctree-wrapper, .breadcrumb, .page-nav, .prev-next-area, "
    "script, style"
)

MAX_CRAWL_PAGES = 30


def _save_with_bz2(data: bytes, pdf_path: str) -> None:
    with open(pdf_path, "wb") as f:
        f.write(data)
    with open(pdf_path + ".bz2", "wb") as f:
        f.write(bz2.compress(data))


def _try_direct_pdf_download(spec_url, repo_name, output_dir):
    candidates = []
    parsed = urlparse(spec_url)
    host = parsed.hostname or ""

    if "readthedocs.io" in host or "readthedocs.org" in host:
        project = host.split(".")[0]
        candidates.append(
            f"https://{project}.readthedocs.io/_/downloads/en/latest/pdf/"
        )
        candidates.append(
            f"https://{project}.readthedocs.io/_/downloads/en/stable/pdf/"
        )

    if spec_url.lower().endswith(".pdf"):
        candidates.append(spec_url)

    pdf_path = os.path.join(output_dir, f"{repo_name}.pdf")

    for url in candidates:
        try:
            _log.info("Trying direct PDF download: %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=60)
            ctype = resp.headers.get("Content-Type", "")
            if "pdf" not in ctype.lower():
                continue
            data = resp.read()
            if len(data) < 500:
                continue
            _save_with_bz2(data, pdf_path)
            _log.info("Direct PDF download OK (%d bytes): %s", len(data), url)
            return pdf_path
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.debug("Direct download failed for %s: %s", url, exc)
    return None


def _crawl_and_pdf_playwright(spec_url, repo_name, output_dir):
    from playwright.sync_api import sync_playwright

    base_parsed = urlparse(spec_url)
    base_netloc = base_parsed.netloc

    visited = set()
    queue = deque([spec_url])
    pages_html = []
    seen_hashes = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0 Safari/537.36",
            java_script_enabled=True,
        )
        page = context.new_page()

        while queue and len(pages_html) < MAX_CRAWL_PAGES:
            url = queue.popleft()
            normalized = url.split("#")[0].rstrip("/")
            if normalized in visited:
                continue
            visited.add(normalized)

            try:
                resp = page.goto(url, wait_until="networkidle", timeout=30000)
                if not resp or resp.status >= 400:
                    continue
            except Exception:
                continue

            content_html = None
            for sel in _MAIN_SELECTORS:
                el = page.query_selector(sel)
                if el:
                    content_html = el.inner_html()
                    break
            if not content_html:
                body_el = page.query_selector("body")
                content_html = body_el.inner_html() if body_el else None
            if not content_html or len(content_html.strip()) < 50:
                continue

            for junk_el in page.query_selector_all(_JUNK_SELECTOR):
                try:
                    junk_el.evaluate("el => el.remove()")
                except Exception:
                    pass

            content_html = None
            for sel in _MAIN_SELECTORS:
                el = page.query_selector(sel)
                if el:
                    content_html = el.inner_html()
                    break
            if not content_html:
                body_el = page.query_selector("body")
                content_html = body_el.inner_html() if body_el else None
            if not content_html or len(content_html.strip()) < 50:
                continue

            h = hashlib.md5(content_html.encode("utf-8", errors="replace")).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            title = page.title() or url
            pages_html.append((title, content_html))

            for link_el in page.query_selector_all("a[href]"):
                try:
                    href = link_el.get_attribute("href")
                except Exception:
                    continue
                if not href:
                    continue
                abs_url = urljoin(url, href).split("#")[0].rstrip("/")
                link_parsed = urlparse(abs_url)
                if link_parsed.netloc != base_netloc:
                    continue
                path_lower = link_parsed.path.lower()
                if any(seg in path_lower for seg in _SKIP_PATH_SEGMENTS):
                    continue
                if any(path_lower.endswith(ext) for ext in _SKIP_EXTENSIONS):
                    continue
                if _LOCALE_RE.search(path_lower):
                    base_path = base_parsed.path.lower()
                    if not _LOCALE_RE.search(base_path):
                        continue
                if abs_url not in visited:
                    queue.append(abs_url)

        if not pages_html:
            browser.close()
            return None

        sections = []
        for i, (title, html) in enumerate(pages_html):
            cls = "page-section"
            sections.append(
                f'<div class="{cls}"><h1 class="page-title">{title}</h1>{html}</div>'
            )

        combined_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{_CSS}</style></head><body>"
            + "\n".join(sections)
            + "</body></html>"
        )

        page.set_content(combined_html, wait_until="networkidle")
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "20mm", "bottom": "20mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()

    if len(pdf_bytes) < 1000:
        return None

    pdf_path = os.path.join(output_dir, f"{repo_name}.pdf")
    _save_with_bz2(pdf_bytes, pdf_path)
    _log.info(
        "Playwright BFS crawl OK (%d pages, %d bytes)", len(pages_html), len(pdf_bytes)
    )
    return pdf_path


def _readme_fallback_pdf(full_name, repo_name, output_dir, github_token=""):
    from playwright.sync_api import sync_playwright

    headers = {"Accept": "application/vnd.github.v3.html", "User-Agent": "Mozilla/5.0"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    url = f"https://api.github.com/repos/{full_name}/readme"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=30)
    readme_html = resp.read().decode("utf-8", errors="replace")

    if not readme_html or len(readme_html.strip()) < 20:
        return None

    full_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{repo_name} — README</h1>"
        f"{readme_html}"
        "</body></html>"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(full_html, wait_until="networkidle")
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "20mm", "bottom": "20mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()

    if len(pdf_bytes) < 500:
        return None

    pdf_path = os.path.join(output_dir, f"{repo_name}.pdf")
    _save_with_bz2(pdf_bytes, pdf_path)
    _log.info("README fallback OK (%d bytes)", len(pdf_bytes))
    return pdf_path


def scrape_spec_sync(spec_url, repo_name, output_dir, github_token="", full_name=""):
    """3-layer fallback PDF generation. Returns path to PDF file, or None."""
    os.makedirs(output_dir, exist_ok=True)

    # Strategy 1: Direct download
    if spec_url:
        path = _try_direct_pdf_download(spec_url, repo_name, output_dir)
        if path:
            return path

    # Strategy 2: Playwright BFS crawl
    if spec_url:
        try:
            path = _crawl_and_pdf_playwright(spec_url, repo_name, output_dir)
            if path:
                return path
        except Exception as exc:
            _log.warning("Playwright crawl failed: %s", exc)

    # Strategy 3: README fallback
    if full_name:
        try:
            path = _readme_fallback_pdf(full_name, repo_name, output_dir, github_token)
            if path:
                return path
        except Exception as exc:
            _log.warning("README fallback failed: %s", exc)

    _log.error("All 3 PDF strategies failed for %s", repo_name)
    return None
