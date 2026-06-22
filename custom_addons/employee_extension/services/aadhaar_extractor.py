import importlib
import io
import logging
import re
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


_AADHAAR_RE = re.compile(r'(?<!\d)(\d{4})\s?(\d{4})\s?(\d{4})(?!\d)')
_DOB_RE = re.compile(
    r'(?:DOB|D\s*\.?\s*O\s*\.?\s*B\.?|Date\s*of\s*Birth)\s*[:\-]?\s*'
    r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})',
    re.IGNORECASE,
)
_YOB_RE = re.compile(r'(?:Year\s*of\s*Birth|YOB)\s*[:\-]?\s*(\d{4})', re.IGNORECASE)
_GENDER_RE = re.compile(r'\b(MALE|FEMALE|TRANSGENDER)\b', re.IGNORECASE)
_MASK_RE = re.compile(r'[Xx*]{4,}')
_NAME_SKIP_TOKENS = (
    'GOVERNMENT', 'GOVT', 'INDIA', 'UNIQUE', 'AUTHORITY', 'UIDAI',
    'AADHAAR', 'ADHAR', 'IDENTIFICATION', 'ENROLMENT', 'ENROLLMENT',
    'ADDRESS', 'FATHER', 'MOTHER', 'HUSBAND', 'WIFE',
)
_NAME_LINE_RE = re.compile(r'^[A-Z][A-Za-z][A-Za-z\s\.]{1,60}$')

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
            for page in doc:
                pix = page.get_pixmap(dpi=220)
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


def _try_ocr(images):
    if _pytesseract is None:
        return None
    chunks = []
    for img in images:
        try:
            chunks.append(_pytesseract.image_to_string(img, lang='eng'))
        except Exception:
            _logger.debug("Tesseract failed on page", exc_info=True)
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

    for m in _AADHAAR_RE.finditer(text):
        digits = m.group(1) + m.group(2) + m.group(3)
        if digits[0] in '23456789':
            out['aadhaar_number'] = digits
            out['aadhaar_last4'] = digits[-4:]
            break

    m = _DOB_RE.search(text)
    if m:
        out['dob'] = _normalize_dob(m.group(1))
    if not out['dob']:
        m = _YOB_RE.search(text)
        if m:
            out['dob'] = f"{m.group(1)}-01-01"

    m = _GENDER_RE.search(text)
    if m:
        v = m.group(1).upper()
        if v in _GENDER_MAP:
            out['gender'] = _GENDER_MAP[v]

    out['name'] = _extract_name_from_text(text)

    return out


def _extract_name_from_text(text):
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    dob_idx = None
    for i, ln in enumerate(lines):
        if re.search(r'DOB|Date\s*of\s*Birth|Year\s*of\s*Birth|YOB', ln, re.IGNORECASE):
            dob_idx = i
            break
    if dob_idx is None:
        return None
    for offset in range(1, 5):
        idx = dob_idx - offset
        if idx < 0:
            break
        candidate = lines[idx]
        upper = candidate.upper()
        if any(skip in upper for skip in _NAME_SKIP_TOKENS):
            continue
        if not _NAME_LINE_RE.match(candidate):
            continue
        if re.search(r'\d', candidate):
            continue
        tokens = [t for t in candidate.split() if len(t) > 1]
        if len(tokens) < 2:
            continue
        return ' '.join(tokens)
    return None


def _names_match(extracted_name, entered_name):
    e = re.sub(r'[^a-zA-Z\s]', ' ', extracted_name).lower()
    n = re.sub(r'[^a-zA-Z\s]', ' ', entered_name).lower()
    e_tokens = {t for t in e.split() if len(t) > 1}
    n_tokens = {t for t in n.split() if len(t) > 1}
    if not e_tokens or not n_tokens:
        return False
    return e_tokens == n_tokens


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

    ocr = _try_ocr(images)
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

    if entered_dob and extracted.get('dob'):
        checks['dob_match'] = (extracted['dob'] == entered_dob)

    if entered_name and extracted.get('name'):
        checks['name_match'] = _names_match(extracted['name'], entered_name)

    failed = []
    if checks['aadhaar_match'] is False:
        failed.append('aadhaar')
    if checks['dob_match'] is False:
        failed.append('dob')
    if checks['name_match'] is False:
        failed.append('name')

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
