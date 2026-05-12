"""Phase 6 / Section 10: Content safety scan — ~40 regex patterns."""

from __future__ import annotations

import re

from ..types import Finding, Severity


# ---------------------------------------------------------------------------
# Pattern registry: (compiled_regex, severity, category, description)
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[re.Pattern, Severity, str, str, bool]] = []


def _p(
    pattern: str, severity: Severity, category: str, description: str,
    *, all_fields: bool = False, flags: int = re.IGNORECASE,
) -> None:
    """Register a pattern."""
    _PATTERNS.append((re.compile(pattern, flags), severity, category, description, all_fields))


# --- Category 1: Project / codename / benchmark leakage ---
_p(r'\[CONTEXT:\s*Current\s+Project\s*=\s*arc-?agents', Severity.CRITICAL, 'leakage', 'OpenCode injection context marker')
_p(r'project_switcher\.py', Severity.CRITICAL, 'leakage', 'Project switcher script reference')
_p(r'\.config/opencode', Severity.CRITICAL, 'leakage', 'OpenCode config path')
_p(r'\barc-?agents\b', Severity.CRITICAL, 'leakage', 'arc-agents project name')
_p(r'Current\s+Project\s*=', Severity.CRITICAL, 'leakage', 'Current Project variable')
_p(r'python3\s+~/', Severity.CRITICAL, 'leakage', 'Home-relative Python execution')
_p(r'To\s+switch[,]?\s+run', Severity.CRITICAL, 'leakage', 'Project switch instruction')
_p(r'\btalos\b', Severity.CRITICAL, 'leakage', 'Talos project name')
_p(r'\bopenclaw\b', Severity.CRITICAL, 'leakage', 'OpenClaw project name')
_p(r'anthropic\s+internal', Severity.CRITICAL, 'leakage', 'Internal org reference')
_p(r'openai\s+internal', Severity.CRITICAL, 'leakage', 'Internal org reference')
_p(r'google\s+internal', Severity.CRITICAL, 'leakage', 'Internal org reference')
_p(r'\bcode[- ]?name\b', Severity.CRITICAL, 'leakage', 'Codename reference')
_p(r'\bconfidential\b', Severity.CRITICAL, 'leakage', 'Confidentiality marker')
_p(r'\bproprietary\b', Severity.CRITICAL, 'leakage', 'Proprietary marker')
_p(r'internal\s+use\s+only', Severity.CRITICAL, 'leakage', 'Internal use marker')
_p(r'do\s+not\s+share', Severity.CRITICAL, 'leakage', 'Do-not-share marker')
_p(r'\bNDA\b', Severity.CRITICAL, 'leakage', 'NDA reference', flags=0)
_p(r'arc[- ]?agi', Severity.CRITICAL, 'leakage', 'ARC-AGI reference')
_p(r'arc\s*prize', Severity.CRITICAL, 'leakage', 'ARC Prize reference')
_p(r'\bchollet\b', Severity.CRITICAL, 'leakage', 'Chollet name reference')
_p(r'\bkaggle\b', Severity.CRITICAL, 'leakage', 'Kaggle reference')
_p(r'eval(?:uation)?\s+harness', Severity.CRITICAL, 'leakage', 'Eval harness reference')
_p(r'\bleaderboard\b', Severity.CRITICAL, 'leakage', 'Leaderboard reference')
_p(r'\bbenchmark(?:ed|ing|s)?\b', Severity.CRITICAL, 'leakage', 'Benchmark reference')
_p(r'\b(?:I am|I\'m|you are|we\'re)\s+being\s+(?:tested|evaluated|benchmarked|scored|graded|judged)\b', Severity.CRITICAL, 'leakage', 'Self-awareness of evaluation')
_p(r'\bthis\s+is\s+a\s+(?:test|benchmark|eval|evaluation)\b', Severity.CRITICAL, 'leakage', 'Test/benchmark acknowledgement')
_p(r'actions\s+(?:are\s+)?being\s+recorded', Severity.CRITICAL, 'leakage', 'Recording awareness')
_p(r'\b(?:deepmind|keen\s+games|lark\s+labs|ndea|arc\s*prize)\b', Severity.CRITICAL, 'leakage', 'Organization reference')
_p(r'OMO_INTERNAL', Severity.CRITICAL, 'leakage', 'OMO internal marker', flags=0)

# --- Category 2: Prompt injection / roleplay / jailbreak ---
_p(r'ignore\s+(?:all\s+)?(?:previous|prior|above|the\s+above)\s+(?:instructions?|prompts?|messages?|context)', Severity.CRITICAL, 'injection', 'Instruction override attempt')
_p(r'disregard\s+(?:the\s+)?(?:above|previous|all\s+previous)', Severity.CRITICAL, 'injection', 'Disregard instruction')
_p(r'forget\s+(?:everything|all\s+prior|what\s+I\s+told\s+you)', Severity.CRITICAL, 'injection', 'Memory wipe instruction')
_p(r'\b(?:new|updated|revised)\s+instructions\b', Severity.MEDIUM, 'injection', 'New instructions reference')
_p(r'\bSYSTEM\s*:', Severity.CRITICAL, 'injection', 'SYSTEM: prompt marker', flags=0)
_p(r'\[SYSTEM\]', Severity.CRITICAL, 'injection', '[SYSTEM] marker')
_p(r'\[CONTEXT[:\]]', Severity.CRITICAL, 'injection', '[CONTEXT] marker')
_p(r'</system>', Severity.CRITICAL, 'injection', '</system> tag')
_p(r'</assistant>', Severity.CRITICAL, 'injection', '</assistant> tag')
_p(r'<\|im_(?:start|end)\|>', Severity.CRITICAL, 'injection', 'ChatML marker')
_p(r'<\|endoftext\|>', Severity.CRITICAL, 'injection', 'End-of-text marker')
_p(r'\bjailbreak\b', Severity.CRITICAL, 'injection', 'Jailbreak reference')
_p(r'\bDAN\s+mode\b', Severity.CRITICAL, 'injection', 'DAN mode reference')
_p(r'developer\s+mode', Severity.CRITICAL, 'injection', 'Developer mode reference')
_p(r'\bact\s+as\s+(?:a|an|the)\b', Severity.MEDIUM, 'injection', 'Roleplay: act as')
_p(r'pretend\s+(?:you\s+are|to\s+be)', Severity.MEDIUM, 'injection', 'Roleplay: pretend')
_p(r'roleplay\s+as', Severity.MEDIUM, 'injection', 'Roleplay instruction')
_p(r'(?:^|\n)\s*(?:user|assistant|human)\s*:', Severity.CRITICAL, 'injection', 'Role prefix injection')
_p(r'curl\s+[^\s]+\s*\|\s*(?:bash|sh)', Severity.CRITICAL, 'injection', 'Shell pipe execution')
_p(r'rm\s+-rf\s+/', Severity.CRITICAL, 'injection', 'Destructive rm command')
_p(r'\bsudo\s+\w+', Severity.CRITICAL, 'injection', 'Sudo execution')
_p(r'\$\([^)]{5,}\)', Severity.CRITICAL, 'injection', 'Shell command substitution')
_p(r'python3?\s+[~/]\S+\.py', Severity.CRITICAL, 'injection', 'Python script execution')
_p(r'(?:(?<=\s)|^)[A-Za-z0-9+/]{100,400}={0,2}(?=\s|$)', Severity.CRITICAL, 'injection', 'Base64 encoded block')

# --- Category 3: Corruption / encoding ---
_p(r'[\xc3\xc2][\x80-\xbf]', Severity.MEDIUM, 'corruption', 'Mojibake signature', all_fields=True)

# --- Category 4: Malicious content ---
_p(r'\b(?:fuck|shit|bitch|asshole|bastard|cunt|dick|pussy|motherfucker|cock|wanker|twat)\b', Severity.CRITICAL, 'malicious', 'Profanity')
_p(r'how\s+to\s+(?:make|build|synthesize|create)\s+(?:a\s+)?(?:bomb|weapon|explosive|meth|ricin|nerve\s+agent|sarin|fentanyl)', Severity.CRITICAL, 'malicious', 'Weapon/drug synthesis')
_p(r'\b(?:kill\s+yourself|suicide\s+method|how\s+to\s+overdose)\b', Severity.CRITICAL, 'malicious', 'Self-harm content')
_p(r'\b(?:cp|csam|child\s+(?:porn|sex))\b', Severity.CRITICAL, 'malicious', 'CSAM reference')

# --- Category 5: PII ---
_p(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', Severity.CRITICAL, 'pii', 'Email address')
_p(r'(?<!\d)(?:\+?1[-. ])?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)', Severity.CRITICAL, 'pii', 'US phone number')
_p(r'(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)', Severity.CRITICAL, 'pii', 'SSN pattern')
_p(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', Severity.CRITICAL, 'pii', 'Credit card number')
_p(r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b', Severity.CRITICAL, 'pii', 'IPv4 address')
_p(r'\b[A-Z]{1,2}\d{6,9}\b', Severity.MEDIUM, 'pii', 'Passport number', flags=0)
_p(r'\d+\s+(?:[A-Z][a-z]+\s+){1,3}(?:St|Ave|Blvd|Dr|Ln|Rd|Way|Ct|Pl|Cir|Pkwy|Hwy)\b', Severity.MEDIUM, 'pii', 'US address', flags=0)

# --- Category 6: Third-party citation markers ---
_p(r'according\s+to\s+(?:wikipedia|reddit|stack\s*overflow|stackoverflow|hacker\s*news|twitter|x\.com|youtube|medium)', Severity.CRITICAL, 'citation', 'Third-party citation')
_p(r'\b(?:web\s+search\s+results?|search\s+results?|google\s+says)\b', Severity.CRITICAL, 'citation', 'Search result reference')
_p(r'\b(?:as\s+per|per)\s+(?:google|bing|wikipedia|chatgpt|claude)', Severity.CRITICAL, 'citation', 'Per-source citation')
_p(r'\[\d+\]', Severity.LOW, 'citation', 'Weak citation marker [N]')
_p(r'\(\d{4}\)', Severity.LOW, 'citation', 'Weak citation marker (year)')
_p(r'^\s*(?:source|ref|reference)\s*:', Severity.MEDIUM, 'citation', 'Source/reference label')


# ---------------------------------------------------------------------------
# Known-contamination shortcut patterns
# ---------------------------------------------------------------------------
CONTAMINATION_SHORTCUT = re.compile(
    r'project_switcher\.py|arc-?agents|\.config/opencode|\[CONTEXT:',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# PII validators (false-positive filters)
# ---------------------------------------------------------------------------

_FAKE_EMAIL_DOMAINS = {'example.com', 'test.com', 'example.org', 'test.org'}

_GRID_CONTEXT_RE = re.compile(r'\b(?:Row|Col|64x64)\b', re.IGNORECASE)

_SINGLE_DIGIT_TOKEN_RE = re.compile(r'(?:\d\s){10,}')

_INVALID_NANP_PREFIXES = frozenset({'0', '1'})

# --- False-positive filter helpers ---

# Filter 1: Credit card — small-integer sequence (ARC grid values 0-30)
_SMALL_INT_SEQUENCE_RE = re.compile(r'(?:\b(?:[12]?\d|30)\b[\s,]+){5,}')

# Filter 2: SYSTEM: — preceded by alphabetic char = compound noun
# (handled inline via match_obj.start() check)

# Filter 3: 'actions are being recorded' — speculation prefix
_SPECULATION_PREFIX_RE = re.compile(r'\b(?:my|if\s+my|whether\s+my|maybe\s+my|perhaps\s+my)\s+$')

# Filter 4: Base64 — digit-only strings are not base64
_BASE64_DIGIT_ONLY_RE = re.compile(r'^[\d]+$')

# Filter 5: 'act as a/an/the' — only flag if followed by AI/role noun (actual injection)
_INJECTION_ROLE_RE = re.compile(
    r'\bact\s+as\s+(?:a|an|the)\s+(?:\w+\s+)?'
    r'(?:AI|assistant|bot|chatbot|model|system|agent|helper|human|person|'
    r'expert|doctor|lawyer|therapist|teacher|professor|villain|'
    r'hacker|programmer|developer|admin|root|superuser)\b',
    re.IGNORECASE,
)

# Filter 6: 'new instructions' — game-context words nearby
_GAME_CONTEXT_RE = re.compile(
    r'\b(?:await|prepare|future|level|progression|next|puzzle|game|round|stage)\b',
    re.IGNORECASE,
)

# Filter 7: Citation density — bracket-number patterns nearby
_BRACKET_NUMBER_DENSITY_RE = re.compile(r'\[\w*\d+\w*\]|\[\.\]')


def _is_valid_nanp_area_code(phone_match: str) -> bool:
    """Check if a US phone number match has a valid NANP area code."""
    digits = re.sub(r'[^\d]', '', phone_match)
    # Strip leading country code '1' if present
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) != 10:
        return False
    area_code = digits[:3]
    # Area code cannot start with 0 or 1
    if area_code[0] in _INVALID_NANP_PREFIXES:
        return False
    return True


def _is_fake_email(match: str) -> bool:
    """Check if an email match is a known fake domain."""
    domain = match.rsplit('@', 1)[-1].lower()
    return domain in _FAKE_EMAIL_DOMAINS


def _is_grid_context(text: str, match_start: int, match_end: int) -> bool:
    """§10.5: Skip credit card if adjacent to Row/Col/64x64 or ≥10 single-digit space-sep tokens."""
    context_start = max(0, match_start - 100)
    context_end = min(len(text), match_end + 100)
    context = text[context_start:context_end]
    if _GRID_CONTEXT_RE.search(context):
        return True
    if _SINGLE_DIGIT_TOKEN_RE.search(context):
        return True
    if _SMALL_INT_SEQUENCE_RE.search(context):
        return True
    return False


def _luhn_check(digits: str) -> bool:
    """Luhn algorithm validation for credit card numbers."""
    digits = digits.replace(' ', '').replace('-', '')
    if not digits.isdigit() or len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _is_private_ip(ip: str) -> bool:
    """Check if IP is RFC1918/loopback."""
    parts = ip.split('.')
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if octets[0] == 10:
        return True
    if octets[0] == 172 and 16 <= octets[1] <= 31:
        return True
    if octets[0] == 192 and octets[1] == 168:
        return True
    if octets[0] == 127:
        return True
    return False


# ---------------------------------------------------------------------------
# §10.6.1 Puzzle-geometry context filter
# ---------------------------------------------------------------------------

_COORD_TOKEN_RE = re.compile(
    r'\bR\d{1,2}C\d{1,2}\b'
    r'|\b[xy]\s*=\s*\d{1,2}\b'
    r'|\(\s*\d{1,2}\s*,\s*\d{1,2}\s*\)'
    r'|\btiles?\b|\bcells?\b|\bgrid\b',
    re.IGNORECASE,
)

_PUZZLE_COMPANION_RE = re.compile(
    r'\bSinks?:\b|\bReceptacles?:\b|\bDrains?:\b|\bTargets?:\b'
    r'|\bGoals?:\b|\bStarts?:\b|\bOrigins?:\b|\bPath\s+plan:\b'
    r'|\bFluid\s+flow\b|\bPipe\b|\bRotate\b|\bCursor\b',
    re.IGNORECASE,
)


def _is_puzzle_geometry_context(text: str, match_start: int) -> bool:
    """§10.6.1: Check if source:/ref:/reference: is puzzle geometry, not citation."""
    lines = text.split('\n')
    match_line_idx = text[:match_start].count('\n')
    window_start = max(0, match_line_idx - 5)
    window_end = min(len(lines), match_line_idx + 6)
    window_text = '\n'.join(lines[window_start:window_end])

    if _COORD_TOKEN_RE.search(window_text):
        return True
    if _PUZZLE_COMPANION_RE.search(window_text):
        return True
    return False


# ---------------------------------------------------------------------------
# §10.7 Whitelist patterns (silently ignored per §10.7)
# ---------------------------------------------------------------------------

_FIM_TOKEN_RE = re.compile(r'<\|(?:fim_prefix|fim_middle|fim_suffix)\|>')
_URL_RE = re.compile(r'https?://[^\s<>"\']+')


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan(
    mdir_name: str,
    game_id: str,
    steps_data: list[dict],
    fpath: str,
) -> list[Finding]:
    """Scan all steps for content safety violations."""
    findings: list[Finding] = []
    prefix = f'{game_id}/{mdir_name}'

    scan_fields_reasoning_notepad = ('reasoning', 'notepad_contents')
    scan_field_observation = 'observation'

    for line_num, record in enumerate(steps_data, start=1):
        line_prefix = f'{prefix}/steps.jsonl:{line_num}'

        for field in (*scan_fields_reasoning_notepad, scan_field_observation):
            text = record.get(field, '')
            if not isinstance(text, str) or not text:
                continue
            if CONTAMINATION_SHORTCUT.search(text):
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    phase='content_safety',
                    code='CONTAMINATION_SHORTCUT',
                    message=(
                        f'{line_prefix}: contamination hit in {field}: '
                        f'matches known-contamination shortcut pattern'
                    ),
                    file_path=fpath,
                    line_number=line_num,
                    field_name=field,
                    spec_ref='Section 10.8',
                ))

        for field in (*scan_fields_reasoning_notepad, scan_field_observation):
            text = record.get(field, '')
            if not isinstance(text, str) or not text:
                continue

            is_observation = (field == scan_field_observation)

            for regex, severity, category, description, all_fields in _PATTERNS:
                if is_observation and not all_fields:
                    continue

                for match_obj in regex.finditer(text):
                    matched_text = match_obj.group()

                    # --- §10.7 Whitelist: FIM tokens and URLs silently ignored ---
                    if _FIM_TOKEN_RE.search(matched_text):
                        continue
                    match_in_url = False
                    for url_match in _URL_RE.finditer(text):
                        if url_match.start() <= match_obj.start() and match_obj.end() <= url_match.end():
                            match_in_url = True
                            break
                    if match_in_url:
                        continue

                    # --- PII false-positive filters ---
                    if category == 'pii':
                        if description == 'Email address' and _is_fake_email(matched_text):
                            continue
                        if description == 'US phone number':
                            if not _is_valid_nanp_area_code(matched_text):
                                continue
                        if description == 'SSN pattern':
                            if matched_text.startswith('000-') or matched_text.startswith('666-'):
                                continue
                        if description == 'Credit card number':
                            digits = matched_text.replace(' ', '').replace('-', '')
                            if not _luhn_check(digits):
                                continue
                            if _is_grid_context(text, match_obj.start(), match_obj.end()):
                                continue
                        if description == 'IPv4 address':
                            if _is_private_ip(matched_text):
                                severity = Severity.MEDIUM
                            else:
                                severity = Severity.CRITICAL
                        if description == 'Passport number':
                            if _is_grid_context(text, match_obj.start(), match_obj.end()):
                                continue
                        if description == 'US address':
                            if is_observation:
                                continue

                    # --- §10.6.1 Puzzle-geometry context filter ---
                    if description == 'Source/reference label' and not is_observation:
                        if _is_puzzle_geometry_context(text, match_obj.start()):
                            continue

                    # --- False-positive filters ---

                    # Filter 2: SYSTEM: preceded by alphabetic = compound noun
                    if description == 'SYSTEM: prompt marker':
                        pos = match_obj.start()
                        if pos > 0 and text[pos - 1].isalpha():
                            continue
                        if pos > 1 and text[pos - 1] == ' ' and text[pos - 2].isalpha():
                            continue

                    # Filter 3: 'actions are being recorded' — model speculation
                    if description == 'Recording awareness':
                        pre_start = max(0, match_obj.start() - 30)
                        pre_text = text[pre_start:match_obj.start()]
                        if _SPECULATION_PREFIX_RE.search(pre_text):
                            continue

                    # Filter 4: Base64 — digit-only strings are not base64
                    if description == 'Base64 encoded block':
                        stripped = matched_text.rstrip('=')
                        if _BASE64_DIGIT_ONLY_RE.match(stripped):
                            continue

                    # Filter 5: 'act as a/an/the' — only flag if it's an injection role
                    if description == 'Roleplay: act as':
                        after_start = match_obj.start()
                        after_text = text[after_start:after_start + 80]
                        if not _INJECTION_ROLE_RE.match(after_text):
                            continue

                    # Filter 6: 'new instructions' — game-context reasoning
                    if description == 'New instructions reference':
                        ctx_start = max(0, match_obj.start() - 80)
                        ctx_end = min(len(text), match_obj.end() + 80)
                        ctx_window = text[ctx_start:ctx_end]
                        if _GAME_CONTEXT_RE.search(ctx_window):
                            continue

                    # Filter 7: Weak citation markers — suppress grid values and non-years
                    if description == 'Weak citation marker [N]':
                        inner = matched_text.strip('[]')
                        if inner.isdigit() and int(inner) <= 30:
                            continue
                        density_start = max(0, match_obj.start() - 200)
                        density_end = min(len(text), match_obj.end() + 200)
                        density_window = text[density_start:density_end]
                        if len(_BRACKET_NUMBER_DENSITY_RE.findall(density_window)) >= 2:
                            continue
                        if _is_puzzle_geometry_context(text, match_obj.start()):
                            continue

                    if description == 'Weak citation marker (year)':
                        inner = matched_text.strip('()')
                        if inner.isdigit():
                            val = int(inner)
                            if val < 1900 or val > 2100:
                                continue

                    actual_severity = severity

                    evidence = matched_text[:200]

                    findings.append(Finding(
                        severity=actual_severity,
                        phase='content_safety',
                        code=f'CONTENT_SAFETY_{category.upper()}',
                        message=f'{line_prefix}: {description} in {field}: {evidence!r}',
                        file_path=fpath,
                        line_number=line_num,
                        field_name=field,
                        spec_ref='Section 10',
                    ))

    return findings
