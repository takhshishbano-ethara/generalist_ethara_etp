import importlib
import io
import logging
import re
import zlib

_logger = logging.getLogger(__name__)


def _try_import(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_fitz = _try_import('fitz')
_PIL_Image = _try_import('PIL.Image')
_zxingcpp = _try_import('zxingcpp')
_easyocr = _try_import('easyocr')
_np = _try_import('numpy')

_RENDER_DPI = 300
_MAX_PDF_PAGES = 2

_FIELDS_V5 = (
    'version', 'email_mobile_status', 'referenceid', 'name', 'dob', 'gender',
    'careof', 'district', 'landmark', 'house', 'location', 'pincode',
    'postoffice', 'state', 'street', 'subdistrict', 'vtc',
    'last_4_digits_mobile_no',
)
_FIELDS_V1 = _FIELDS_V5[1:-1]

_ADDRESS_FIELDS = (
    'house', 'street', 'landmark', 'location', 'vtc',
    'postoffice', 'subdistrict', 'district', 'state',
)


def _decode_secure_qr_payload(qr_text):
    if not qr_text or not qr_text.isdigit() or len(qr_text) < 80:
        return None
    try:
        big_int = int(qr_text)
        bytes_array = big_int.to_bytes(5000, 'big').lstrip(b'\x00')
        decompressed = zlib.decompress(bytes_array, 16 + zlib.MAX_WBITS)
    except Exception:
        _logger.debug("Aadhaar secure QR decompress failed", exc_info=True)
        return None

    head = decompressed[:2].decode('ISO-8859-1', errors='replace')
    is_versioned = (
        len(head) == 2 and head[0] == 'V' and head[1].isdigit()
    )
    fields = _FIELDS_V5 if is_versioned else _FIELDS_V1

    delims = [-1] + [i for i, b in enumerate(decompressed) if b == 255]
    if len(delims) < len(fields) + 1:
        return None

    data = {}
    for i, key in enumerate(fields):
        try:
            data[key] = decompressed[
                delims[i] + 1:delims[i + 1]
            ].decode('ISO-8859-1', errors='replace').strip()
        except Exception:
            data[key] = ''
    return data


def _compose_indian_address(payload):
    if not isinstance(payload, dict):
        return ''
    parts = []
    careof = (payload.get('careof') or '').strip()
    if careof:
        parts.append(careof)
    for key in _ADDRESS_FIELDS:
        val = (payload.get(key) or '').strip()
        if val:
            parts.append(val)
    if not parts:
        return ''
    addr = ', '.join(parts)
    pin = (payload.get('pincode') or '').strip()
    if pin:
        addr = f"{addr} - {pin}"
    return addr


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
        _logger.debug("Aadhaar PDF render failed", exc_info=True)
    return images


_IMAGE_UPSCALE_TARGETS = (2500, 4000)


def _image_to_pil_variants(file_bytes):
    if _PIL_Image is None:
        return []
    try:
        img = _PIL_Image.open(io.BytesIO(file_bytes))
        img.load()
    except Exception:
        _logger.debug("Aadhaar image open failed", exc_info=True)
        return []
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    w, h = img.size
    max_dim = max(w, h)
    variants = [img]
    for target in _IMAGE_UPSCALE_TARGETS:
        if max_dim < target:
            scale = target / max_dim
            variants.append(
                img.resize(
                    (int(w * scale), int(h * scale)),
                    _PIL_Image.LANCZOS,
                )
            )
    return variants


_easyocr_reader = None

_PIN_RE = re.compile(r'\b([1-9]\d{5})\b')
_AADHAAR_NUM_RE = re.compile(r'\b\d{4}\s+\d{4}\s+\d{4}\b')
_VID_RE = re.compile(r'\bVID\b', re.IGNORECASE)
_UIDAI_NOISE_RE = re.compile(
    r'(UIDAI|AADHAAR|Unique Identification|Authority of India|gov\.in|'
    r'help@|GOVERNMENT|^\s*1947\s*$|wwwuidai|www\.uidai)',
    re.IGNORECASE,
)
_ADDRESS_ANCHOR_RE = re.compile(
    r'^\s*(Address\s*:?|S\s*[/I1]\s*O\b|D\s*[/I1]\s*O\b|'
    r'W\s*[/I1]\s*O\b|C\s*[/I1]\s*O\b)',
    re.IGNORECASE,
)
_SIO_NORM_RE = re.compile(r'\b([SDWC])\s*[/I1]\s*O\b')


def _ensure_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is False:
        return None
    if _easyocr_reader is not None:
        return _easyocr_reader
    if _easyocr is None or _np is None:
        _easyocr_reader = False
        return None
    try:
        _easyocr_reader = _easyocr.Reader(['en'], gpu=False, verbose=False)
    except Exception:
        _logger.warning("easyocr Reader init failed; OCR fallback disabled", exc_info=True)
        _easyocr_reader = False
        return None
    return _easyocr_reader


def _ocr_image_to_lines(reader, img):
    try:
        rgb = img if img.mode == 'RGB' else img.convert('RGB')
        arr = _np.array(rgb)
        lines = reader.readtext(arr, detail=0, paragraph=False)
        return [str(line) for line in lines if line]
    except Exception:
        _logger.debug("easyocr readtext failed", exc_info=True)
        return []


def _is_address_text_line(line):
    stripped = line.strip()
    if len(stripped) < 4:
        return False
    ascii_chars = sum(
        1 for c in stripped
        if c.isascii() and (c.isalnum() or c in ' ,;:./-+')
    )
    return (ascii_chars / max(len(stripped), 1)) >= 0.7


def _clean_ocr_line(line):
    cleaned = line.strip().strip('|').strip()
    cleaned = cleaned.replace(';', ',')
    cleaned = re.sub(r',\s*,+', ',', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip(' ,.;:-')


def _normalize_sio_prefix(text):
    return _SIO_NORM_RE.sub(lambda m: f'{m.group(1)}/O', text)


def _parse_address_from_ocr_lines(lines):
    if not lines:
        return ''

    anchor_idx = -1
    for i, line in enumerate(lines):
        if _ADDRESS_ANCHOR_RE.search(line):
            anchor_idx = i
            break

    search_from = anchor_idx + 1 if anchor_idx >= 0 else 0
    pin_idx = -1
    pin_value = ''
    for i in range(search_from, len(lines)):
        if _AADHAAR_NUM_RE.search(lines[i]):
            continue
        m = _PIN_RE.search(lines[i])
        if m:
            pin_idx = i
            pin_value = m.group(1)
            break

    if pin_idx < 0:
        return ''

    if anchor_idx < 0:
        anchor_idx = max(pin_idx - 10, 0)

    collected = []
    for k in range(anchor_idx, pin_idx + 1):
        line = lines[k]
        if re.fullmatch(r'\s*Address\s*:?\s*', line, re.IGNORECASE):
            continue
        if line.strip() == pin_value:
            continue
        if _AADHAAR_NUM_RE.search(line):
            continue
        if _VID_RE.search(line):
            continue
        if _UIDAI_NOISE_RE.search(line):
            continue
        if not _is_address_text_line(line):
            continue
        cleaned = _clean_ocr_line(line)
        if cleaned and not cleaned.isdigit():
            collected.append(cleaned)

    if not collected:
        return ''

    seen = set()
    deduped = []
    for ln in collected:
        key = ln.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(ln)

    address = ', '.join(deduped)
    address = _normalize_sio_prefix(address)
    address = re.sub(r',\s*,+', ',', address)
    address = re.sub(r'\s+', ' ', address)
    address = address.strip(' ,.;:-')

    if pin_value and pin_value not in address:
        address = f'{address} - {pin_value}'

    return address


def _extract_via_easyocr(images):
    if not images:
        return None
    reader = _ensure_easyocr_reader()
    if reader is None:
        return None

    all_lines = []
    for img in images:
        all_lines.extend(_ocr_image_to_lines(reader, img))

    if not all_lines:
        return None

    address = _parse_address_from_ocr_lines(all_lines)
    full_text = '\n'.join(all_lines)

    last4 = ''
    m = _AADHAAR_NUM_RE.search(full_text)
    if m:
        digits = re.sub(r'\s+', '', m.group(0))
        if len(digits) == 12:
            last4 = digits[-4:]

    is_aadhaar = bool(last4) or bool(_UIDAI_NOISE_RE.search(full_text))
    if not (address or is_aadhaar):
        return None

    return {
        'address': address,
        'aadhaar_last4': last4,
        'is_masked': False,
    }


def extract_aadhaar_info_from_bytes(file_bytes, filename):
    if not file_bytes:
        return None

    ext = (filename or '').rsplit('.', 1)[-1].lower() if filename else ''
    if ext == 'pdf':
        images = _pdf_to_images(file_bytes)
    else:
        images = _image_to_pil_variants(file_bytes)

    if not images:
        return None

    if _zxingcpp is not None:
        for img in images:
            try:
                results = _zxingcpp.read_barcodes(img)
            except Exception:
                _logger.debug("zxing read_barcodes failed", exc_info=True)
                continue
            for r in results:
                text = getattr(r, 'text', '') or ''
                payload = _decode_secure_qr_payload(text)
                if not payload:
                    continue
                address = _compose_indian_address(payload)
                referenceid = (payload.get('referenceid') or '').strip()
                last4 = referenceid[:4] if len(referenceid) >= 4 else ''
                return {
                    'address': address,
                    'aadhaar_last4': last4,
                    'is_masked': False,
                }

    ocr_result = _extract_via_easyocr(images)
    if ocr_result is not None:
        return ocr_result

    return None
