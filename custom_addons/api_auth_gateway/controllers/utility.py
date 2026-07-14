from odoo import http
from odoo.http import request
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta

IST_OFFSET = timedelta(hours=5, minutes=30)
import functools
import json
import calendar
import mimetypes
import time
import re
import uuid
import os


EXECUTIVE_ROLE_REFS = (
    'api_auth_gateway.role_cto_technical',
    'api_auth_gateway.role_cfo_technical',
)


def executive_role_ids(env):
    ids = []
    for ref in EXECUTIVE_ROLE_REFS:
        rec = env.ref(ref, raise_if_not_found=False)
        if rec:
            ids.append(rec.id)
    return ids


def is_executive_role(user):
    if not user or not user.user_role:
        return False
    return user.user_role.id in executive_role_ids(user.env)


def return_Response(message, status=200, errors=[], data=None):
    res = {
        'message': message,
        "errors": errors
    }
    if data:
        res.update(data)
    if status == 200:
        res.update({
            "status_code": 200
        })
    if status == 400:
        if res.get('errors'):
            error_list = []
            for e in res.get('errors'):
                if isinstance(e, dict):
                    val = e.get('error', str(e))
                    error_list.append(val)
                else:
                    error_list.append(str(e))
            res['message'] = ", ".join(error_list)
        res.update({
            "status_code": 400
        })
    if status == 404:
        res.update({
            "status_code": 404
        })
    if status == 403:
        res.update({
            "status_code": 403
        })
    if status == 401:
        res.update({
            "status_code": 401
        })
    return http.Response(
        json.dumps(res),
        status=status,
        mimetype='application/json'
    )

def validate_token(func):
    @functools.wraps(func)
    def wrap(self, *args, **kwargs):
        access_token = request.httprequest.headers.get('access_token')
        if not access_token:
            return return_Response(message="missing access token in request header", status=401)
        access_token_data = request.env['api.access_token'].sudo().search(
            [('access_token', '=', access_token)], order='id DESC', limit=1)
        if not access_token_data:
            return return_Response(message="token seems to have expired or invalid", status=401)
        if access_token_data.has_expired():
            return return_Response(message="token seems to have expired or invalid", status=401)
        request.update_env(user=access_token_data.user_id.id)
        if not access_token_data.user_id or access_token_data.user_id._is_public():
            return return_Response(message="Authentication required.", status=401)

        if not access_token_data.user_id.user_role:
            return return_Response(message="No role assigned to your account.", status=403)

        url_pattern = _current_url_pattern()
        print(url_pattern,'---------------', access_token_data.user_id.user_role)
        method = _current_method()
        if not url_pattern:
            return return_Response(message="Endpoint not resolvable.", status=403)

        allowed = access_token_data.user_id.user_role.sudo().endpoint_ids.filtered(
            lambda e: e.url_pattern == url_pattern
        )
        print(allowed)
        if not allowed:
            return return_Response(
                message="Your role is not allowed to access this endpoint.",
                status=403,
            )

        return func(self, *args, **kwargs)
    return wrap


def _current_url_pattern():
    httpreq = getattr(request, 'httprequest', None)
    if httpreq is None:
        return None
    rule = getattr(httpreq, 'url_rule', None)
    if rule is not None and getattr(rule, 'rule', None):
        return rule.rule
    return httpreq.path


def _current_method():
    httpreq = getattr(request, 'httprequest', None)
    return httpreq.method.upper() if httpreq else None


def require_endpoint_access(func):
    @functools.wraps(func)
    def wrap(self, *args, **kwargs):
        user = request.env.user
        if not user or user._is_public():
            return return_Response(message="Authentication required.", status=401)

        if not user.user_role:
            return return_Response(message="No role assigned to your account.", status=403)

        url_pattern = _current_url_pattern()
        method = _current_method()
        if not url_pattern or not method:
            return return_Response(message="Endpoint not resolvable.", status=403)

        allowed = user.user_role.sudo().endpoint_ids.filtered(
            lambda e: e.url_pattern == url_pattern and e.method == method
        )
        if not allowed:
            return return_Response(
                message="Your role is not allowed to access this endpoint.",
                status=403,
            )
        return func(self, *args, **kwargs)
    return wrap



def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return isinstance(email, str) and bool(re.match(pattern, email))

def is_valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def is_valid_gst(value):
    pattern = r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
    return isinstance(value, str) and bool(re.match(pattern, value))

def is_valid_aadhaar(value):
    pattern = r'^[2-9]{1}[0-9]{11}$'
    return isinstance(value, str) and bool(re.match(pattern, value))

def is_valid_fssai(value):
    pattern = r'^[0-9]{14}$'
    return isinstance(value, str) and bool(re.match(pattern, value))

def is_valid_pan(value):
    pattern = r'^[A-Z]{5}[0-9]{4}[A-Z]{1}$'
    return isinstance(value, str) and bool(re.match(pattern, value))

def is_valid_mobile(value):
    pattern = r'^[6-9][0-9]{9}$'
    return isinstance(value, str) and bool(re.match(pattern, value))


def is_valid_indian_zip(value):
    pattern = r'^[1-9][0-9]{5}$'
    return isinstance(value, str) and bool(re.match(pattern, value))
# ------------------------------
# Validators and Examples
# ------------------------------
TYPE_VALIDATORS = {
    'int': lambda v: isinstance(v, int),
    'float': lambda v: isinstance(v, float),
    'string': lambda v: isinstance(v, str),
    'email': is_valid_email,
    'list': lambda v: isinstance(v, list),
    'dict': lambda v: isinstance(v, dict),
    'date': is_valid_date,
    'gst': is_valid_gst,
    'aadhaar': is_valid_aadhaar,
    'fssai': is_valid_fssai,
    'pan': is_valid_pan,
    'mobile': is_valid_mobile,
    'zip': is_valid_indian_zip,
}

TYPE_EXAMPLES = {
    'int': 123,
    'float': 12.34,
    'string': "example text",
    'str': "example text",
    'email': "user@example.com",
    'list': ["item1", "item2"],
    'dict': {"key": "value"},
    'date': "2025-11-12",
    'gst': "27ABCDE1234F1Z5",
    'aadhaar': "234512341234",
    'fssai': "12345678901234",
    'pan': "ABCDE1234F",
    'mobile': "9876543210",
    'zip': "111111",
}

def validate_data(data, schema, parent_key=None):
    errors = []
    for field, rule in schema.items():
        required = rule.get('required', False)
        expected_type = rule.get('type')
        subfields = rule.get('fields')  # for dict
        items_schema = rule.get('items')  # for list of dicts

        full_key = f"{parent_key}.{field}" if parent_key else field

        # Required check
        if required and field not in data:
            errors.append({
                "field": full_key,
                "error": f"'{full_key}' is required but was not provided.",
                "expected_type": expected_type,
                "example": TYPE_EXAMPLES.get(expected_type)
            })
            continue
        elif required and not data.get(field):
            errors.append({
                "field": full_key,
                "error": f"'{full_key}' cannot be empty.",
                "expected_type": expected_type,
                "example": TYPE_EXAMPLES.get(expected_type)
            })

        if field not in data:
            continue

        value = data[field]

        # Dictionary check
        if expected_type == 'dict':
            if not isinstance(value, dict):
                errors.append({
                    "field": full_key,
                    "error": f"'{full_key}' should be an object with details inside.",
                    "expected_type": "dict",
                    "example": TYPE_EXAMPLES["dict"]
                })
                continue
            if subfields:
                errors.extend(validate_data(value, subfields, parent_key=full_key))
            continue

        # List check (simple or list of dict)
        if expected_type == 'list':
            if not isinstance(value, list):
                errors.append({
                    "field": full_key,
                    "error": f"'{full_key}' should be a list of items.",
                    "expected_type": "list",
                    "example": TYPE_EXAMPLES["list"]
                })
                continue

            # List of dict validation
            if items_schema and isinstance(items_schema, dict):
                for idx, item in enumerate(value):
                    if not isinstance(item, dict):
                        errors.append({
                            "field": f"{full_key}[{idx}]",
                            "error": f"Each item in '{full_key}' should contain proper details.",
                            "example": items_schema
                        })
                        continue
                    nested = validate_data(item, items_schema, parent_key=f"{full_key}[{idx}]")
                    errors.extend(nested)
            continue

        # Regular types (string, email, etc.)
        validator = TYPE_VALIDATORS.get(expected_type)
        if validator and not validator(value):
            errors.append({
                "field": full_key,
                "error": f"Please enter a valid data for '{full_key}'.",
                "expected_type": expected_type,
                "example": TYPE_EXAMPLES.get(expected_type)
            })

    return errors

def validate_request(expected_params):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            data = {}
            # Merge URL args + JSON body
            if request.params:
                data.update(request.params)
            try:
                jdata = json.loads(request.httprequest.stream.read())
            except:
                try:
                    jdata = json.loads(request.httprequest.data)
                except:
                    jdata = {}
            if jdata:
                data.update(jdata)

            # Validate data
            errors = validate_data(data, expected_params)
            if errors:
                # return {"errors": errors}
                return return_Response(message=", ".join([e['error'] for e in errors]), status=400, errors=errors, data=None)
                # return return_Response({"message": ", ".join([e['error'] for e in errors]),"errors": errors}, 400)
                # http.Response(
                #     json.dumps({"errors": errors}),
                #     status=400,
                #     mimetype='application/json'
                # )

            return func(*args, **kwargs, jdata=data)
        return wrapper
    return decorator


def safe_get_value(record, field_path, expected_type=str):
    type_map = {
        'str': str,
        'int': int,
        'float': float,
        'bool': bool,
        'list': list,
        'dict': dict,
        'date': date,
        'datetime': datetime,
    }

    if isinstance(expected_type, str):
        expected_type = type_map.get(expected_type, str)

    value = record
    for part in field_path.split('.'):
        value = getattr(value, part, None)
        if not value:
            if expected_type in (date, datetime):
                return ""
            return expected_type()

    try:
        if expected_type == date:
            if isinstance(value, datetime):
                return value.date().isoformat()
            elif isinstance(value, date):
                return value.isoformat()
            elif isinstance(value, str):
                try:
                    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
                except ValueError:
                    return ""
            return ""
        elif expected_type == datetime:
            if isinstance(value, datetime):
                return (value + IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, date):
                return datetime.combine(value, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, str):
                try:
                    return (datetime.fromisoformat(value) + IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return ""
            return ""
        else:
            return expected_type(value)
    except Exception:
        if expected_type in (date, datetime):
            return ""
        return expected_type()

def generate_s3_link(img_data, prefix='profile', uid=None, filename=None):
    ts = time.time_ns()
    unique_id = uuid.uuid4().hex[:12]
    s3_connector_id = request.env['s3.connector'].sudo().search([], limit=1)

    if filename:
        _, ext = os.path.splitext(filename)
        if ext:
            extension = ext
            mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        else:
            mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            extension = mimetypes.guess_extension(mime_type) or '.bin'
    else:
        mime_type = "image/jpeg"
        extension = '.jpeg'

    safe_name = secure_filename(f"{ts}_{uid}_{unique_id}_{prefix}") if uid else secure_filename(f"{ts}_{unique_id}_{prefix}")
    images_name = f"{safe_name}{extension}"

    img_req = request.env['s3.upload.wizard'].sudo().create({
        's3_connector_id': s3_connector_id.id,
        'upload_file': img_data,
        'prefix': prefix,
        'file_name': images_name
    })

    if img_req:
        return img_req.upload_images_in_s3_get_url()
    else:
        return ""
