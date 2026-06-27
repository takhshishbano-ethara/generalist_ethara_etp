import importlib
import io
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any

_logger = logging.getLogger(__name__)


def _try_import(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_PIL_Image = _try_import('PIL.Image')
_pyzbar = _try_import('pyzbar.pyzbar')
_pytesseract = _try_import('pytesseract')
_fitz = _try_import('fitz')

_pyaadhaar_mod = _try_import('pyaadhaar.decode') or _try_import('pyaadhaar.utils')
_PYAADHAAR_SECURE = getattr(_pyaadhaar_mod, 'AadhaarSecureQr', None) if _pyaadhaar_mod else None
_PYAADHAAR_OLD = getattr(_pyaadhaar_mod, 'AadhaarOldQr', None) if _pyaadhaar_mod else None

# Performance bounds — keep the synchronous OCR/QR work well under the gateway
# (Cloudflare/nginx) request timeout so the endpoint never hangs the client.
_RENDER_DPI = 200          # PDF page render resolution (enough for QR + OCR)
_MAX_PDF_PAGES = 2         # Aadhaar data is on page 1; never OCR a whole booklet
_OCR_PAGE_TIMEOUT = 18     # hard per-page Tesseract timeout (seconds)
_OCR_MAX_DIM = 2200        # downscale long edge before OCR to bound CPU time


_AADHAAR_RE = re.compile(r'(?<!\d)(\d{4})\s?(\d{4})\s?(\d{4})(?!\d)')
# [DP][O0Q][BS580] tolerates OCR drift on "DOB" — Tesseract often reads
# D as P at higher DPIs, B as S/8/5/0, and O as 0/Q. The trailing date
# pattern guards against false positives like the words "DOS" or "POS".
_DOB_RE = re.compile(
    r'(?:[DP][O0Q][BS580]|D\s*\.?\s*O\s*\.?\s*B\.?|Date\s*of\s*Birth)\s*[:\-]?\s*'
    r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})',
    re.IGNORECASE,
)
_YOB_RE = re.compile(r'(?:Year\s*of\s*Birth|YOB)\s*[:\-]?\s*(\d{4})', re.IGNORECASE)
_GENDER_RE = re.compile(r'\b(MALE|FEMALE|TRANSGENDER)\b', re.IGNORECASE)
_MASK_RE = re.compile(r'[Xx*]{4,}')
_NAME_SKIP_TOKENS = (
    'GOVERNMENT', 'GOVT', 'INDIA', 'UNIQUE', 'AUTHORITY', 'UIDAI',
    'AADHAAR', 'ADHAR', 'IDENTIFICATION', 'ENROLMENT', 'ENROLLMENT',
    'ADDRESS', 'FATHER', 'MOTHER', 'HUSBAND', 'WIFE', 'DOB', 'BIRTH',
    'MALE', 'FEMALE', 'TRANSGENDER', 'GENDER', 'PHONE', 'MOBILE', 'VID',
    'HELP', 'WWW', 'PIN', 'S/O', 'D/O', 'W/O', 'C/O',
)

# A real English name on an Aadhaar card is Title-case ("Akshita Dixit").
_NAME_WORD_TITLE_RE = re.compile(r'^[A-Z][a-z]+$')   # Akshita, Dixit
_NAME_WORD_CAPS_RE = re.compile(r'^[A-Z]{2,}$')       # RAVI (some cards print ALLCAPS)
_NAME_INITIAL_RE = re.compile(r'^[A-Z]\.?$')          # single initial, e.g. "A."

# Anchors that the name sits next to. The name is the clean line(s) directly
# ABOVE a relationship / dated-DOB line, and directly BELOW a "To" line.
_RELATION_RE = re.compile(
    r'^\s*(?:'
    r'(?:S|D|W|C)\s*/\s*O\b|'
    r'(?:SON|DAUGHTER|WIFE|CARE)\s+OF\b|'
    r'(?:FATHER|MOTHER|HUSBAND|GUARDIAN)\s*[:\-]'
    r')',
    re.IGNORECASE,
)
_TO_RE = re.compile(r'^\s*to\s*:?\s*$', re.IGNORECASE)

_GENDER_MAP = {
    'M': 'male', 'F': 'female', 'T': 'other',
    'MALE': 'male', 'FEMALE': 'female', 'TRANSGENDER': 'other',
}


def _normalize_dob(raw):
    if not raw:
        return None
    s = str(raw).strip()
    s_norm = s.replace('.', '/').replace('-', '/')
    for fmt in ('%d/%m/%Y', '%Y/%m/%d', '%d/%m/%y'):
        try:
            return datetime.strptime(s_norm, fmt).date().isoformat()
        except ValueError:
            continue
    digits = re.sub(r'\D', '', s)
    if len(digits) == 8:
        for fmt in ('%d%m%Y', '%Y%m%d'):
            try:
                return datetime.strptime(digits, fmt).date().isoformat()
            except ValueError:
                continue
    if len(digits) == 4:
        try:
            year = int(digits)
        except ValueError:
            return None
        if 1900 <= year <= 2100:
            return f"{digits}-01-01"
    return None


def _bytes_to_images(file_bytes, filename):
    if _PIL_Image is None:
        return [], "Server is missing the Pillow library (cannot read images)"
    if not file_bytes:
        return [], "Uploaded file is empty"

    name = (filename or '').lower()
    is_pdf = name.endswith('.pdf') or file_bytes[:4] == b'%PDF'

    if is_pdf:
        if _fitz is None:
            return [], "Server is missing PyMuPDF (cannot read PDF Aadhaar)"
        try:
            doc = _fitz.open(stream=file_bytes, filetype='pdf')
        except Exception as e:
            return [], f"Could not open PDF: {e}"
        try:
            if getattr(doc, 'needs_pass', False) or getattr(doc, 'is_encrypted', False):
                return [], "Encrypted or password-protected PDFs are not accepted. Please upload an unencrypted Aadhaar."
            images = []
            for i, page in enumerate(doc):
                if i >= _MAX_PDF_PAGES:
                    break
                pix = page.get_pixmap(dpi=_RENDER_DPI)
                images.append(_PIL_Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB'))
        finally:
            doc.close()
        return images, None

    try:
        img = _PIL_Image.open(io.BytesIO(file_bytes))
        img.load()
        if img.mode != 'RGB':
            img = img.convert('RGB')
        return [img], None
    except Exception as e:
        return [], f"Could not decode image: {e}"


def _parse_pyaadhaar_payload(payload):
    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {
        'aadhaar_number': None,
        'aadhaar_last4': None,
        'dob': None,
        'name': None,
        'gender': None,
    }

    ref = payload.get('referenceid') or payload.get('reference_id') or ''
    if ref and len(str(ref)) >= 4:
        out['aadhaar_last4'] = str(ref)[:4]
    if payload.get('aadhaar_last_4_digit'):
        out['aadhaar_last4'] = str(payload['aadhaar_last_4_digit'])[-4:]

    if payload.get('uid'):
        uid = re.sub(r'\D', '', str(payload['uid']))
        if len(uid) == 12:
            out['aadhaar_number'] = uid
            out['aadhaar_last4'] = uid[-4:]

    out['dob'] = _normalize_dob(payload.get('dob') or payload.get('yob'))
    out['name'] = payload.get('name') or None

    g = (payload.get('gender') or '').upper()
    if g in _GENDER_MAP:
        out['gender'] = _GENDER_MAP[g]

    return out


def _try_qr(images):
    if _pyzbar is None:
        return None
    for img in images:
        try:
            results = _pyzbar.decode(img)
        except Exception:
            _logger.debug("zbar decode raised", exc_info=True)
            continue
        for r in results:
            raw = r.data
            try:
                text = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else str(raw)
            except Exception:
                continue
            if not text:
                continue

            if _PYAADHAAR_SECURE and text.isdigit() and len(text) > 80:
                try:
                    qr = _PYAADHAAR_SECURE(int(text))
                    data = _parse_pyaadhaar_payload(qr.decodeddata())
                    if data:
                        return data
                except Exception:
                    _logger.debug("Secure QR decode failed", exc_info=True)

            if _PYAADHAAR_OLD and ('<?xml' in text or 'PrintLetterBarcodeData' in text):
                try:
                    qr = _PYAADHAAR_OLD(text)
                    data = _parse_pyaadhaar_payload(qr.decodeddata())
                    if data:
                        return data
                except Exception:
                    _logger.debug("Old QR decode failed", exc_info=True)
    return None


def _downscale_for_ocr(img):
    """Shrink oversized renders so a single Tesseract pass stays fast."""
    try:
        w, h = img.size
    except Exception:
        return img
    long_edge = max(w, h)
    if long_edge <= _OCR_MAX_DIM:
        return img
    scale = _OCR_MAX_DIM / float(long_edge)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    try:
        return img.resize(new_size, _PIL_Image.LANCZOS)
    except Exception:
        return img


_DIGIT_OCR_CFG = '--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789 '
_NUMBER_STRIP_THRESHOLDS = (80, 100, 120, 60)


def _ocr_aadhaar_from_bottom(img):
    """Last-resort digit pass: the green-gradient watermark behind the bottom
    number row blurs Tesseract's digit recognition (it usually returns only
    the first 4 digits). Crop+4x upscale+low-threshold binarise erases the
    watermark so all 12 digits read; the threshold sweep handles cards with
    different shading."""
    if _pytesseract is None or _PIL_Image is None:
        return None
    try:
        w, h = img.size
    except Exception:
        return None
    if w < 200 or h < 200:
        return None
    strip = img.crop(
        (int(w * 0.05), int(h * 0.55), w, h)
    ).convert('L')
    strip = strip.resize(
        (strip.size[0] * 4, strip.size[1] * 4), _PIL_Image.LANCZOS,
    )
    for threshold in _NUMBER_STRIP_THRESHOLDS:
        binarized = strip.point(lambda p, t=threshold: 0 if p < t else 255)
        try:
            text = _pytesseract.image_to_string(
                binarized, lang='eng', config=_DIGIT_OCR_CFG,
                timeout=_OCR_PAGE_TIMEOUT,
            )
        except Exception:
            _logger.debug("Bottom-strip OCR failed", exc_info=True)
            continue
        for m in _AADHAAR_RE.finditer(text):
            digits = m.group(1) + m.group(2) + m.group(3)
            if digits[0] in '23456789':
                return digits
    return None


def _ocr_page(img, config=''):
    if _pytesseract is None:
        return ''
    try:
        return _pytesseract.image_to_string(
            _downscale_for_ocr(img), lang='eng',
            config=config, timeout=_OCR_PAGE_TIMEOUT,
        )
    except Exception:
        # Includes Tesseract's timeout — skip the page rather than hang.
        _logger.debug("Tesseract failed or timed out", exc_info=True)
        return ''


def _parse_fields(text, out):
    if not out.get('aadhaar_number'):
        for m in _AADHAAR_RE.finditer(text):
            digits = m.group(1) + m.group(2) + m.group(3)
            if digits[0] in '23456789':
                out['aadhaar_number'] = digits
                out['aadhaar_last4'] = digits[-4:]
                break
    if not out.get('dob'):
        m = _DOB_RE.search(text)
        if m:
            out['dob'] = _normalize_dob(m.group(1))
    if not out.get('dob'):
        m = _YOB_RE.search(text)
        if m:
            out['dob'] = f"{m.group(1)}-01-01"
    if not out.get('gender'):
        m = _GENDER_RE.search(text)
        if m:
            v = m.group(1).upper()
            if v in _GENDER_MAP:
                out['gender'] = _GENDER_MAP[v]
    if not out.get('name'):
        out['name'] = _extract_name_from_text(text)


def _try_ocr(images):
    if _pytesseract is None:
        return None
    chunks = [_ocr_page(img) for img in images]
    chunks = [c for c in chunks if c]
    if not chunks:
        return None

    text = '\n'.join(chunks)
    out: dict[str, Any] = {
        'aadhaar_number': None,
        'aadhaar_last4': None,
        'dob': None,
        'name': None,
        'gender': None,
        'is_masked': bool(_MASK_RE.search(text)),
    }
    _parse_fields(text, out)

    # PSM 3 (default, auto-segmentation) sometimes drops a whole text block
    # near the photo — most commonly the cardholder's English name. PSM 6
    # treats the page as one block and picks those up. Only retry when a
    # critical field is still missing, to keep clean cards on the fast path.
    if not (out['name'] and out['dob'] and out['aadhaar_number']):
        psm6_chunks = [_ocr_page(img, config='--oem 3 --psm 6') for img in images]
        psm6_text = '\n'.join(c for c in psm6_chunks if c)
        if psm6_text.strip():
            out['is_masked'] = out['is_masked'] or bool(_MASK_RE.search(psm6_text))
            _parse_fields(psm6_text, out)

    if not out['aadhaar_number']:
        for img in images:
            recovered = _ocr_aadhaar_from_bottom(img)
            if recovered:
                out['aadhaar_number'] = recovered
                out['aadhaar_last4'] = recovered[-4:]
                break

    return out


def _name_token_kind(tok):
    """Classify an OCR token. Returns 'title' | 'caps' | 'initial' | None."""
    if _NAME_INITIAL_RE.match(tok):
        return 'initial'
    bare = tok.strip('.')
    if _NAME_WORD_TITLE_RE.match(bare):
        return 'title'
    if _NAME_WORD_CAPS_RE.match(bare):
        return 'caps'
    return None


def _name_candidate(line, allow_single=False):
    """Score a single OCR line as a possible person name.

    Returns ``(clean_name, score)`` or ``(None, 0.0)`` when the line is not a
    plausible name. A real English name on an Aadhaar card is Title-case
    ("Akshita Dixit"); OCR of the Devanagari name line above it (run in
    ``lang='eng'``) tends to come out as ALLCAPS/short junk ("STU FT HY a"),
    so Title-case words are scored far higher and win the tie-break.

    ``allow_single=True`` accepts single-token names (e.g. "Sahiba") — used
    only when the line sits next to a strong anchor (DOB, relationship, To)
    so we don't have to settle for noisy fallback hits. Single tokens are
    still required to be Title-case and >= 4 chars to filter OCR junk.
    """
    upper = line.upper()
    if any(skip in upper for skip in _NAME_SKIP_TOKENS):
        return None, 0.0
    if re.search(r'\d', line):
        return None, 0.0

    # Keep only the leading run of name-like tokens, so transliteration junk
    # merged onto the same line ("Akshita Dixit अ" -> "Akshita Dixit STU") is
    # dropped rather than appended to the name.
    kept = []
    for tok in line.split():
        kind = _name_token_kind(tok)
        if kind is None:
            break
        kept.append((kind, tok.strip('.')))
    if not kept:
        return None, 0.0

    title = sum(1 for kind, _ in kept if kind == 'title')
    caps = sum(1 for kind, _ in kept if kind == 'caps')

    primary = 'title' if title else 'caps'
    words = [t for kind, t in kept if kind in (primary, 'initial')]

    if len(words) < 2:
        if not (allow_single and len(words) == 1 and title == 1 and len(words[0]) >= 4):
            return None, 0.0

    # Reject lines that are only short ALLCAPS tokens (classic OCR noise from
    # the Hindi name line), e.g. "STU FT HY".
    avg_len = sum(len(t) for t in words) / len(words)
    if title == 0 and avg_len < 4:
        return None, 0.0

    score = title * 2.0 + caps * 0.5
    return ' '.join(words), score


def _extract_name_from_text(text):
    """Pull the cardholder's name out of OCR'd Aadhaar text.

    The English name OCRs cleanly and appears more than once (in the "To"
    address block and again on the card next to the photo), while the Hindi
    name line above it OCRs to different garbage each time. We therefore look
    next to several reliable anchors and let repetition break ties:

      * directly ABOVE a dated "DOB: 27/06/2001" line   (must contain a date,
        so the legal "...date of birth (DOB)." disclaimer never matches)
      * directly ABOVE a "Year of Birth" line
      * directly ABOVE a relationship line ("D/O Ashwani Kumar")
      * directly BELOW a "To" line
    """
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    n = len(lines)

    # Build (anchor_index, direction) markers. direction=-1 -> name is above.
    markers = []
    for i, ln in enumerate(lines):
        if _DOB_RE.search(ln) or _YOB_RE.search(ln) or _RELATION_RE.match(ln):
            markers.append((i, -1))
        elif _TO_RE.match(ln):
            markers.append((i, +1))

    # Collect name candidates from the 1-3 lines next to each anchor.
    candidates = []  # (score, distance, name)
    for idx, direction in markers:
        for off in range(1, 4):
            j = idx + direction * off
            if 0 <= j < n:
                name, score = _name_candidate(lines[j], allow_single=True)
                if name:
                    candidates.append((score, off, name))

    # Fall back to the best name-like line anywhere if no anchor was found.
    if not candidates:
        for ln in lines:
            name, score = _name_candidate(ln)
            if name:
                candidates.append((score, 99, name))
        if not candidates:
            return None

    # The genuine name repeats across anchors; OCR garbage does not. Rank by
    # (base score + how many times this exact text was seen), then proximity.
    freq = Counter(name for _, _, name in candidates)
    best = max(candidates, key=lambda c: (c[0] + freq[c[2]], -c[1]))
    return best[2]


_NAME_HONORIFICS = frozenset({
    'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'shri', 'sri', 'smt',
    'shrimati', 'ji', 'late',
})


def _name_tokens(name):
    cleaned = re.sub(r'[^a-zA-Z\s]', ' ', name or '').lower()
    words, initials = [], []
    for tok in cleaned.split():
        if tok in _NAME_HONORIFICS:
            continue
        if len(tok) == 1:
            initials.append(tok)
        else:
            words.append(tok)
    return words, initials


def _initials_compatible(short_words, short_initials, long_words):
    """Every short-side token must map to a long-side token.

    A short word matches if it equals a long word. A short initial matches
    if some long word starts with that letter (and isn't already claimed by
    another short token).
    """
    if not short_words and not short_initials:
        return False
    remaining = list(long_words)
    for w in short_words:
        if w in remaining:
            remaining.remove(w)
        else:
            return False
    for i in short_initials:
        match = next((w for w in remaining if w.startswith(i)), None)
        if match is None:
            return False
        remaining.remove(match)
    return True


def _names_match(extracted_name, entered_name):
    """Identity-check name match — strict enough for KYC, lenient enough to
    accept the same person spelled with or without middle names, honorifics,
    or initials.

    Accepts:
      * exact token set match ("Bhushan Diwakar" == "Bhushan Diwakar")
      * one name is a subset of the other AND they share >= 2 tokens
        (covers "Bhushan Diwakar" vs "Bhushan Kumar Diwakar")
      * first AND last name match (covers reordered middle names)
      * initial-vs-full-name match ("B Diwakar" vs "Bhushan Diwakar")
      * single-token names if that token appears in the other name

    Honorifics (Mr, Mrs, Dr, Shri, Smt, ...) are stripped from both sides.
    """
    e_words, e_initials = _name_tokens(extracted_name)
    n_words, n_initials = _name_tokens(entered_name)
    if not (e_words or e_initials) or not (n_words or n_initials):
        return False

    e_set, n_set = set(e_words), set(n_words)
    if e_set and e_set == n_set:
        return True

    if e_set and n_set:
        shared = e_set & n_set
        if len(shared) >= 2 and (e_set <= n_set or n_set <= e_set):
            return True
        if (
            len(e_words) >= 2
            and len(n_words) >= 2
            and e_words[0] == n_words[0]
            and e_words[-1] == n_words[-1]
        ):
            return True

    # Initial-vs-full handling: try matching the shorter side against the
    # longer side both ways round so it's symmetric.
    if _initials_compatible(e_words, e_initials, n_words):
        return True
    if _initials_compatible(n_words, n_initials, e_words):
        return True

    # Single-token fallback: a single word on one side that appears on the
    # other (handles users entering just first or last name when the card
    # only has two name tokens).
    if len(e_words) == 1 and len(n_words) >= 1 and e_words[0] in n_set:
        return True
    if len(n_words) == 1 and len(e_words) >= 1 and n_words[0] in e_set:
        return True

    return False


def extract_aadhaar(file_bytes, filename):
    result: dict[str, Any] = {
        'aadhaar_number': None,
        'aadhaar_last4': None,
        'dob': None,
        'name': None,
        'gender': None,
        'is_masked': False,
        'source': None,
        'error': None,
    }
    images, err = _bytes_to_images(file_bytes, filename)
    if err:
        result['error'] = err
        return result

    qr = _try_qr(images)
    if qr:
        for k, v in qr.items():
            if v and not result.get(k):
                result[k] = v
        result['source'] = 'qr'

    # The signed QR is authoritative; if it already gave us everything, skip the
    # expensive OCR pass entirely (this is the fast, no-timeout-risk path).
    qr_complete = bool(result['aadhaar_number'] and result['dob'] and result['name'])

    ocr = None if qr_complete else _try_ocr(images)
    if ocr:
        if not result['aadhaar_number'] and ocr.get('aadhaar_number'):
            result['aadhaar_number'] = ocr['aadhaar_number']
        if not result['aadhaar_last4'] and ocr.get('aadhaar_last4'):
            result['aadhaar_last4'] = ocr['aadhaar_last4']
        if not result['dob'] and ocr.get('dob'):
            result['dob'] = ocr['dob']
        if not result['gender'] and ocr.get('gender'):
            result['gender'] = ocr['gender']
        if not result['name'] and ocr.get('name'):
            result['name'] = ocr['name']
        if ocr.get('is_masked'):
            result['is_masked'] = True
        result['source'] = 'qr+ocr' if result['source'] == 'qr' else 'ocr'

    has_any_marker = bool(
        result['aadhaar_number'] or result['aadhaar_last4'] or result['dob']
    )
    if not result['source'] or not has_any_marker:
        result['error'] = (
            "Invalid file. The uploaded file does not appear to be an Aadhaar "
            "card. Please upload a clear image or PDF of an original, unmasked "
            "Aadhaar card."
        )

    return result


def mask_aadhaar(number):
    if not number or not isinstance(number, str):
        return number
    digits = re.sub(r'\D', '', number)
    if len(digits) != 12:
        return number
    return f"{digits[:4]} XXXX {digits[-4:]}"


def verify_against_input(extracted, entered_aadhaar, entered_dob=None, entered_name=None):
    checks = {'aadhaar_match': None, 'dob_match': None, 'name_match': None}

    if extracted.get('is_masked'):
        return False, (
            "The uploaded Aadhaar is masked. Masked Aadhaar cards are not "
            "accepted. Please upload an original, unmasked Aadhaar."
        ), checks

    entered_digits = re.sub(r'\D', '', entered_aadhaar or '')

    if extracted.get('aadhaar_number') and len(entered_digits) == 12:
        checks['aadhaar_match'] = (extracted['aadhaar_number'] == entered_digits)

    if checks['aadhaar_match'] is None:
        if not extracted.get('aadhaar_number') and not extracted.get('dob') and not extracted.get('name'):
            return False, (
                "The uploaded file does not appear to be a valid Aadhaar card. "
                "Please upload a clear photo or scan of the original Aadhaar."
            ), checks
        return False, (
            "Could not read the full Aadhaar number from the card. "
            "Please upload a clearer, unmasked scan."
        ), checks

    if entered_dob:
        if extracted.get('dob'):
            checks['dob_match'] = (extracted['dob'] == entered_dob)
        else:
            checks['dob_match'] = False

    if entered_name:
        if extracted.get('name'):
            checks['name_match'] = _names_match(extracted['name'], entered_name)
        else:
            checks['name_match'] = False

    unreadable = []
    failed = []
    if checks['dob_match'] is False and not extracted.get('dob'):
        unreadable.append('dob')
    elif checks['dob_match'] is False:
        failed.append('dob')
    if checks['name_match'] is False and not extracted.get('name'):
        unreadable.append('name')
    elif checks['name_match'] is False:
        failed.append('name')
    if checks['aadhaar_match'] is False:
        failed.append('aadhaar')

    if unreadable:
        unreadable_labels = {
            'dob': 'Date of Birth',
            'name': 'name',
        }
        joined = ' and '.join(unreadable_labels[f] for f in unreadable)
        return False, (
            f"Could not read the {joined} from the uploaded Aadhaar card. "
            "Please upload a clearer image of the original Aadhaar."
        ), checks

    if failed:
        single_messages = {
            'aadhaar': "The Aadhaar number on the uploaded card does not match the number you entered.",
            'dob': "The Date of Birth on the uploaded card does not match the Date of Birth you entered.",
            'name': "The name on the uploaded card does not match the name you entered.",
        }
        if len(failed) == 1:
            return False, single_messages[failed[0]], checks
        labels = {'aadhaar': 'Aadhaar number', 'dob': 'Date of Birth', 'name': 'Name'}
        joined = ', '.join(labels[f] for f in failed)
        return False, (
            f"These fields don't match the uploaded card: {joined}."
        ), checks

    return True, "Aadhaar verified. You may proceed with registration.", checks
