import importlib
import io
import logging
import re
import threading
from collections import OrderedDict
from hashlib import sha256

_logger = logging.getLogger(__name__)


def _try_import(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_fitz = _try_import('fitz')
_PIL_Image = _try_import('PIL.Image')
_pytesseract = _try_import('pytesseract')
_easyocr = _try_import('easyocr')
_np = _try_import('numpy')

_RENDER_DPI = 150
_MAX_PDF_PAGES = 2
_IMAGE_MAX_DIM = 1800

_AADHAAR_12_SPACED_RE = re.compile(r'\b(\d{4})\s+(\d{4})\s+(\d{4})\b')
_AADHAAR_12_SOLID_RE = re.compile(r'\b(\d{12})\b')
_AADHAAR_16_RE = re.compile(r'\b\d{4}\s+\d{4}\s+\d{4}\s+\d{4}\b')
_VID_LABEL_RE = re.compile(r'\bVID\b', re.IGNORECASE)
_AADHAAR_MASKED_RE = re.compile(
    r'\b[X*\u2A2F]{4}\s*[X*\u2A2F]{4}\s*\d{4}\b', re.IGNORECASE
)
_PAN_RE = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b')

_AADHAAR_WHITELIST = '0123456789 Xx'
_PAN_WHITELIST = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

_CACHE_MAX = 32
_cache_lock = threading.Lock()
_cache = OrderedDict()


def _cache_get(kind, file_bytes):
    if not file_bytes:
        return None
    key = f'{kind}:{sha256(file_bytes).hexdigest()}'
    with _cache_lock:
        if key not in _cache:
            return None
        _cache.move_to_end(key)
        return _cache[key]


def _cache_set(kind, file_bytes, value):
    if not file_bytes:
        return
    key = f'{kind}:{sha256(file_bytes).hexdigest()}'
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _pdf_to_images(file_bytes):
    if _fitz is None or _PIL_Image is None:
        return []
    images = []
    try:
        with _fitz.open(stream=file_bytes, filetype='pdf') as doc:
            for idx, page in enumerate(doc):
                if idx >= _MAX_PDF_PAGES:
                    break
                pix = page.get_pixmap(dpi=_RENDER_DPI)
                img = _PIL_Image.open(io.BytesIO(pix.tobytes('png')))
                img.load()
                images.append(img)
    except Exception:
        _logger.debug("identity_extractor: PDF render failed", exc_info=True)
    return images


def _image_from_bytes(file_bytes):
    if _PIL_Image is None:
        return []
    try:
        img = _PIL_Image.open(io.BytesIO(file_bytes))
        img.load()
    except Exception:
        _logger.debug("identity_extractor: image open failed", exc_info=True)
        return []
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    w, h = img.size
    max_dim = max(w, h)
    if max_dim > _IMAGE_MAX_DIM:
        scale = _IMAGE_MAX_DIM / max_dim
        img = img.resize((int(w * scale), int(h * scale)), _PIL_Image.LANCZOS)
    return [img]


def _bytes_to_images(file_bytes, filename):
    if not file_bytes:
        return []
    ext = (filename or '').rsplit('.', 1)[-1].lower() if filename else ''
    if ext == 'pdf':
        return _pdf_to_images(file_bytes)
    return _image_from_bytes(file_bytes)


def _tesseract_lines(img, whitelist=None):
    if _pytesseract is None:
        return []
    config = '--psm 6'
    if whitelist:
        config += f' -c tessedit_char_whitelist={whitelist}'
    try:
        text = _pytesseract.image_to_string(img, config=config)
    except Exception:
        _logger.debug("identity_extractor: tesseract failed", exc_info=True)
        return []
    return [ln.strip() for ln in text.split('\n') if ln.strip()]


_easyocr_reader = None
_easyocr_lock = threading.Lock()


def _ensure_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is False:
        return None
    if _easyocr_reader is not None:
        return _easyocr_reader
    if _easyocr is None or _np is None:
        _easyocr_reader = False
        return None
    with _easyocr_lock:
        if _easyocr_reader is None:
            try:
                _easyocr_reader = _easyocr.Reader(
                    ['en'], gpu=False, verbose=False
                )
            except Exception:
                _logger.warning(
                    "identity_extractor: easyocr Reader init failed",
                    exc_info=True,
                )
                _easyocr_reader = False
    return _easyocr_reader or None


def _easyocr_lines(img):
    reader = _ensure_easyocr_reader()
    if reader is None:
        return []
    try:
        rgb = img if img.mode == 'RGB' else img.convert('RGB')
        arr = _np.array(rgb)
        lines = reader.readtext(arr, detail=0, paragraph=False)
        return [str(line) for line in lines if line]
    except Exception:
        _logger.debug("identity_extractor: easyocr readtext failed", exc_info=True)
        return []


def _find_full_aadhaar(lines):
    for line in lines:
        if _VID_LABEL_RE.search(line):
            continue
        if _AADHAAR_16_RE.search(line):
            continue
        m = _AADHAAR_12_SPACED_RE.search(line)
        if m:
            return ''.join(m.groups())
        m = _AADHAAR_12_SOLID_RE.search(line)
        if m:
            return m.group(1)
    return None


def _detect_masked_aadhaar(lines):
    for line in lines:
        if _AADHAAR_MASKED_RE.search(line):
            return True
    return False


def _find_pan(lines):
    for line in lines:
        m = _PAN_RE.search(line.upper())
        if m:
            return m.group(0)
    return None


def _extract_aadhaar_impl(file_bytes, filename):
    images = _bytes_to_images(file_bytes, filename)
    if not images:
        return None

    any_masked = False
    tess_saw_text = False
    for img in images:
        lines = _tesseract_lines(img, whitelist=_AADHAAR_WHITELIST)
        if lines:
            tess_saw_text = True
        full = _find_full_aadhaar(lines)
        if full:
            return {'aadhaar_number': full}
        if _detect_masked_aadhaar(lines):
            any_masked = True

    if any_masked:
        return {'is_masked': True}
    if tess_saw_text:
        return None

    for img in images:
        lines = _easyocr_lines(img)
        if not lines:
            continue
        full = _find_full_aadhaar(lines)
        if full:
            return {'aadhaar_number': full}
        if _detect_masked_aadhaar(lines):
            any_masked = True

    if any_masked:
        return {'is_masked': True}
    return None


def _extract_pan_impl(file_bytes, filename):
    images = _bytes_to_images(file_bytes, filename)
    if not images:
        return None

    tess_saw_text = False
    for img in images:
        lines = _tesseract_lines(img, whitelist=_PAN_WHITELIST)
        if lines:
            tess_saw_text = True
        pan = _find_pan(lines)
        if pan:
            return {'pan_number': pan}

    if tess_saw_text:
        return None

    for img in images:
        lines = _easyocr_lines(img)
        if not lines:
            continue
        pan = _find_pan(lines)
        if pan:
            return {'pan_number': pan}

    return None


def extract_aadhaar_full_from_bytes(file_bytes, filename):
    cached = _cache_get('aadhaar', file_bytes)
    if cached is not None:
        return cached or None
    result = _extract_aadhaar_impl(file_bytes, filename)
    _cache_set('aadhaar', file_bytes, result or {})
    return result


def extract_pan_from_bytes(file_bytes, filename):
    cached = _cache_get('pan', file_bytes)
    if cached is not None:
        return cached or None
    result = _extract_pan_impl(file_bytes, filename)
    _cache_set('pan', file_bytes, result or {})
    return result


threading.Thread(
    target=_ensure_easyocr_reader, name='identity-extractor-warmup', daemon=True
).start()
