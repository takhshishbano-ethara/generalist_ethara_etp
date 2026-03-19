from odoo import http
from odoo.http import request
from werkzeug.utils import secure_filename
from datetime import datetime, date
import functools
import json
import calendar
import mimetypes
import time
import re
import uuid


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
    """."""
    @functools.wraps(func)
    def wrap(self, *args, **kwargs):
        """."""
        access_token = request.httprequest.headers.get('access_token')
        if not access_token:
            return return_Response(message="missing access token in request header", status=401)
        access_token_data = request.env['api.access_token'].sudo().search([('access_token', '=', access_token)], order='id DESC', limit=1)

        if access_token_data.find_one_or_create_token(user_id=access_token_data.user_id.id)[0] != access_token:
            return return_Response(message="token seems to have expired or invalid", status=401)
        request.update_env(user=access_token_data.user_id.id)
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
                return value.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, date):
                return datetime.combine(value, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, str):
                try:
                    # Normalize datetime string
                    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return ""
            return ""
        else:
            return expected_type(value)
    except Exception:
        if expected_type in (date, datetime):
            return ""
        return expected_type()
