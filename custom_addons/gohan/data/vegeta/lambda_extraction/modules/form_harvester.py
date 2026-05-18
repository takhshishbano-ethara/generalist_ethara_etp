"""
Form harvester.

Visits a curated set of auth-flow routes (signup, login, forgot-password,
verify-email, MFA, onboarding) and captures every form's field shape:
type, name, label, placeholder, autocomplete, required, pattern, length
bounds. The signup form is the strongest ground-truth signal for the User
entity in PRD Section 6; the presence of reset / verify / MFA routes drives
PRD Section 4 (Auth & Onboarding).

The harvester opens its own page on the existing browser context, visits one
URL per logical purpose (signup OR sign-up OR register, whichever responds
first; same for the other groups), then closes that page. The caller's
original page is never navigated. A 404 or timeout per route is the expected
case; the function returns whatever it finds and never raises.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


_NAV_TIMEOUT_MS = 12_000
_SETTLE_MS = 1_000
_MAX_FORMS_PER_ROUTE = 4
_MAX_FIELDS_PER_FORM = 24


_PURPOSE_GROUPS: list[tuple[str, list[str]]] = [
    ("signup", ["/signup", "/sign-up", "/register", "/create-account"]),
    ("login", ["/login", "/signin", "/sign-in"]),
    ("password_reset", ["/forgot-password", "/password-reset", "/reset-password"]),
    ("email_verification", ["/verify-email", "/verify"]),
    ("mfa", ["/mfa", "/two-factor"]),
    ("onboarding", ["/onboarding"]),
]


_FORM_EXTRACT_JS = r"""
() => {
    const formEls = Array.from(document.querySelectorAll('form'));
    const forms = [];
    for (const form of formEls) {
        const rect = form.getBoundingClientRect();
        if (rect.width < 40 || rect.height < 40) continue;
        const fields = [];
        const inputs = form.querySelectorAll('input, select, textarea');
        for (const el of inputs) {
            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
            if (type === 'hidden' || type === 'submit' || type === 'button') continue;
            const name = el.getAttribute('name') || '';
            const id = el.getAttribute('id') || '';
            let label = '';
            if (id) {
                const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
                if (lbl) label = (lbl.textContent || '').replace(/\s+/g, ' ').trim();
            }
            if (!label) {
                const parentLabel = el.closest('label');
                if (parentLabel) label = (parentLabel.textContent || '').replace(/\s+/g, ' ').trim();
            }
            if (!label) {
                const ariaLabel = el.getAttribute('aria-label');
                if (ariaLabel) label = ariaLabel.trim();
            }
            const placeholder = el.getAttribute('placeholder') || '';
            const autocomplete = el.getAttribute('autocomplete') || '';
            const required = el.hasAttribute('required') || el.getAttribute('aria-required') === 'true';
            const pattern = el.getAttribute('pattern') || '';
            const minLength = el.getAttribute('minlength') || '';
            const maxLength = el.getAttribute('maxlength') || '';
            const options = [];
            if (type === 'select-one' || type === 'select-multiple' || el.tagName === 'SELECT') {
                for (const opt of el.querySelectorAll('option')) {
                    const v = (opt.value || '').trim();
                    const t = (opt.textContent || '').trim();
                    if (v || t) options.push({ value: v, text: t.slice(0, 60) });
                    if (options.length >= 20) break;
                }
            }
            fields.push({
                type, name, id, label: label.slice(0, 120),
                placeholder, autocomplete, required,
                pattern, minLength, maxLength, options,
            });
            if (fields.length >= 40) break;
        }
        const submit = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
        const submitText = submit ? (submit.textContent || submit.value || '').replace(/\s+/g, ' ').trim().slice(0, 60) : '';
        forms.push({
            action: form.getAttribute('action') || '',
            method: (form.getAttribute('method') || 'get').toLowerCase(),
            id: form.getAttribute('id') || '',
            name: form.getAttribute('name') || '',
            fieldCount: fields.length,
            fields,
            submitText,
        });
        if (forms.length >= 6) break;
    }
    const ssoButtons = Array.from(document.querySelectorAll('a, button')).filter(el => {
        const text = (el.textContent || '').toLowerCase();
        return /sign\s*in\s*with|log\s*in\s*with|continue\s*with|connect\s*with|sign\s*up\s*with/.test(text);
    }).slice(0, 12).map(el => ({
        text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 50),
        href: el.tagName === 'A' ? (el.getAttribute('href') || '') : '',
    }));
    const totpHints = Array.from(document.querySelectorAll('input')).some(i => {
        const t = (i.getAttribute('autocomplete') || '') + ' ' + (i.getAttribute('name') || '') + ' ' + (i.getAttribute('placeholder') || '');
        return /one-?time-?code|otp|totp|verification[- ]code|2fa|mfa/i.test(t);
    });
    return {
        forms,
        ssoButtons,
        totpHints,
        title: document.title || '',
    };
}
"""


async def harvest_forms(page, base_url: str, observed_routes: list[str]) -> dict[str, Any]:
    """Visit auth-flow routes and capture form field shapes.

    Args:
        page: Playwright Page with a live BrowserContext. A scratch page is
            opened on its context; the caller's page is not navigated.
        base_url: The site origin URL (scheme + host). Path is ignored.
        observed_routes: Routes already discovered. Used to prefer the variant
            the site actually uses (e.g., /sign-up over /signup).

    Returns:
        {
            "forms_by_route": {route_url: [{action, method, fields[...], submit_text}, ...], ...},
            "signup_user_fields": [{name, type, label, autocomplete, required}, ...],
            "login_form": {...} or None,
            "password_reset_present": bool,
            "email_verification_present": bool,
            "mfa_present": bool,
            "onboarding_present": bool,
            "sso_providers": [str, ...],
            "purpose_routes": {purpose: route_url, ...},
            "probe_log": [{purpose, path, status, found_form:bool}, ...],
        }
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    candidates_by_purpose = _select_candidates_by_purpose(observed_routes, origin)

    result: dict[str, Any] = {
        "forms_by_route": {},
        "signup_user_fields": [],
        "login_form": None,
        "password_reset_present": False,
        "email_verification_present": False,
        "mfa_present": False,
        "onboarding_present": False,
        "sso_providers": [],
        "purpose_routes": {},
        "probe_log": [],
    }

    if not candidates_by_purpose:
        return result

    context = page.context
    scratch = None
    try:
        scratch = await context.new_page()
    except Exception as exc:
        logger.warning("form_harvester: could not open scratch page: %s", exc)
        return result

    sso_providers_set: set[str] = set()
    try:
        for purpose, candidates in candidates_by_purpose:
            found_route: str | None = None
            for candidate in candidates:
                log_entry = {"purpose": purpose, "path": candidate, "status": None, "found_form": False}
                try:
                    response = await scratch.goto(candidate, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    log_entry["status"] = response.status if response else None
                    if not response or response.status >= 400:
                        result["probe_log"].append(log_entry)
                        continue
                    await scratch.wait_for_timeout(_SETTLE_MS)
                    raw = await scratch.evaluate(_FORM_EXTRACT_JS)
                except Exception as exc:
                    log_entry["status"] = f"error: {type(exc).__name__}"
                    result["probe_log"].append(log_entry)
                    continue

                forms = _normalise_forms(raw.get("forms") or [])
                sso_buttons = raw.get("ssoButtons") or []
                totp_hint = bool(raw.get("totpHints"))

                for prov in _infer_sso_providers(sso_buttons):
                    sso_providers_set.add(prov)

                if forms or sso_buttons or totp_hint:
                    log_entry["found_form"] = bool(forms)
                    result["forms_by_route"][candidate] = forms
                    result["purpose_routes"][purpose] = candidate

                    if purpose == "signup" and forms:
                        result["signup_user_fields"] = _select_user_fields(forms)
                    if purpose == "login" and forms and not result["login_form"]:
                        result["login_form"] = forms[0]
                    if purpose == "password_reset":
                        result["password_reset_present"] = True
                    if purpose == "email_verification":
                        result["email_verification_present"] = True
                    if purpose == "mfa" or totp_hint:
                        result["mfa_present"] = True
                    if purpose == "onboarding":
                        result["onboarding_present"] = True

                    found_route = candidate
                    result["probe_log"].append(log_entry)
                    break

                result["probe_log"].append(log_entry)

            if not found_route:
                continue
    finally:
        try:
            await scratch.close()
        except Exception:
            pass

    result["sso_providers"] = sorted(sso_providers_set)
    return result


def _select_candidates_by_purpose(observed_routes: list[str], origin: str) -> list[tuple[str, list[str]]]:
    observed_paths: set[str] = set()
    origin_host = urlparse(origin).netloc
    for route in observed_routes or []:
        if not route:
            continue
        full = route if route.startswith("http") else urljoin(origin + "/", route.lstrip("/"))
        parsed = urlparse(full)
        if parsed.netloc and parsed.netloc != origin_host:
            continue
        path = parsed.path.rstrip("/").lower() or "/"
        observed_paths.add(path)

    out: list[tuple[str, list[str]]] = []
    for purpose, paths in _PURPOSE_GROUPS:
        ordered: list[str] = []
        for path in paths:
            if path.lower() in observed_paths:
                ordered.append(urljoin(origin + "/", path.lstrip("/")))
        for path in paths:
            full = urljoin(origin + "/", path.lstrip("/"))
            if full not in ordered:
                ordered.append(full)
        out.append((purpose, ordered))
    return out


def _normalise_forms(raw_forms: list[dict]) -> list[dict]:
    out: list[dict] = []
    for raw in raw_forms[:_MAX_FORMS_PER_ROUTE]:
        fields_raw = raw.get("fields") or []
        fields_out: list[dict] = []
        for f in fields_raw[:_MAX_FIELDS_PER_FORM]:
            type_ = (f.get("type") or "").strip()
            if type_ in ("hidden", "submit", "button", "reset", "image"):
                continue
            fields_out.append({
                "name": (f.get("name") or "").strip() or None,
                "type": type_ or "text",
                "label": (f.get("label") or "").strip() or None,
                "placeholder": (f.get("placeholder") or "").strip() or None,
                "autocomplete": (f.get("autocomplete") or "").strip() or None,
                "required": bool(f.get("required")),
                "pattern": (f.get("pattern") or "").strip() or None,
                "min_length": (f.get("minLength") or "").strip() or None,
                "max_length": (f.get("maxLength") or "").strip() or None,
                "options": f.get("options") or [],
            })
        out.append({
            "action": (raw.get("action") or "").strip() or None,
            "method": (raw.get("method") or "get").lower(),
            "id": (raw.get("id") or "").strip() or None,
            "name": (raw.get("name") or "").strip() or None,
            "field_count": raw.get("fieldCount") or len(fields_out),
            "fields": fields_out,
            "submit_text": (raw.get("submitText") or "").strip() or None,
        })
    return out


def _select_user_fields(forms: list[dict]) -> list[dict]:
    best_form: dict | None = None
    best_score = -1
    for form in forms:
        fields = form.get("fields") or []
        score = 0
        has_password = False
        has_email = False
        for f in fields:
            name = (f.get("name") or "").lower()
            ac = (f.get("autocomplete") or "").lower()
            type_ = (f.get("type") or "").lower()
            if type_ == "password" or "password" in name or "password" in ac:
                has_password = True
            if type_ == "email" or "email" in name or ac == "email":
                has_email = True
            if any(k in name or k in ac for k in ("name", "user", "company", "phone", "title", "role")):
                score += 1
        if has_password and has_email:
            score += 5
        if score > best_score:
            best_score = score
            best_form = form

    if not best_form:
        return []

    return [
        {
            "name": f.get("name"),
            "type": f.get("type"),
            "label": f.get("label"),
            "autocomplete": f.get("autocomplete"),
            "required": f.get("required"),
        }
        for f in (best_form.get("fields") or [])
        if (f.get("name") or f.get("label"))
    ]


_SSO_PROVIDER_PATTERNS = (
    ("google", ("google",)),
    ("github", ("github",)),
    ("facebook", ("facebook",)),
    ("apple", ("apple",)),
    ("microsoft", ("microsoft", "azure ad", "azuread")),
    ("twitter", ("twitter", "x.com")),
    ("discord", ("discord",)),
    ("linkedin", ("linkedin",)),
    ("slack", ("slack",)),
    ("okta", ("okta",)),
    ("saml", ("saml",)),
    ("sso", ("single sign-on", "sso",)),
)


def _infer_sso_providers(sso_buttons: list[dict]) -> list[str]:
    out: set[str] = set()
    for btn in sso_buttons:
        text = ((btn.get("text") or "") + " " + (btn.get("href") or "")).lower()
        for canonical, patterns in _SSO_PROVIDER_PATTERNS:
            if any(p in text for p in patterns):
                out.add(canonical)
    return sorted(out)
