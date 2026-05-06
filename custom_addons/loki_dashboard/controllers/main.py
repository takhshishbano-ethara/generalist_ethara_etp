import json
import os
import glob

from odoo import http
from odoo.http import request

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "ecg_records",
)


def _get_record_list():
    json_files = sorted(glob.glob(os.path.join(_DATA_DIR, "*.json")))
    records = []
    for path in json_files:
        basename = os.path.basename(path)
        stem = basename.rsplit(".json", 1)[0]
        num_prefix = stem.split(".")[0].strip()
        records.append({
            "index": int(num_prefix) if num_prefix.isdigit() else 0,
            "stem": stem,
            "json_file": basename,
            "png_file": stem + ".png",
        })
    records.sort(key=lambda r: r["index"])
    return records


def _compute_stats():
    json_files = sorted(glob.glob(os.path.join(_DATA_DIR, "*.json")))
    total = 0
    diseased = 0
    normal = 0
    hr_sum = 0
    age_sum = 0
    male = 0
    female = 0
    reviewed = 0
    diagnoses = {}

    for path in json_files:
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            continue

        total += 1
        patient = data.get("patient", {})
        measurements = data.get("measurements", {})
        interpretation = data.get("interpretation", {})

        if interpretation.get("diseased"):
            diseased += 1
        else:
            normal += 1

        hr = measurements.get("heart_rate_bpm", 0)
        hr_sum += hr

        age = patient.get("age_years", 0)
        age_sum += age

        gender = patient.get("gender", "")
        if gender == "M":
            male += 1
        elif gender == "F":
            female += 1

        if interpretation.get("doctor_reviewed"):
            reviewed += 1

        label = interpretation.get("label", "Unknown")
        diagnoses[label] = diagnoses.get(label, 0) + 1

    avg_hr = round(hr_sum / total) if total else 0
    avg_age = round(age_sum / total) if total else 0

    return {
        "total_records": total,
        "diseased": diseased,
        "normal": normal,
        "avg_hr": avg_hr,
        "avg_age": avg_age,
        "male": male,
        "female": female,
        "reviewed": reviewed,
        "diagnoses": diagnoses,
    }


class LokiDashboardController(http.Controller):

    @http.route("/loki", type="http", auth="public", website=True, sitemap=True)
    def dashboard_page(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        stats = _compute_stats()
        values = {
            "dashboard_title": ICP.get_param(
                "loki_dashboard.title", ""
            ) or "Dashboard",
            "stats": stats,
        }
        return request.render("loki_dashboard.portal_dashboard", values)

    @http.route("/loki/api/records", type="http", auth="public", cors="*")
    def api_records(self, **kw):
        records = _get_record_list()
        return http.Response(
            json.dumps(records), content_type="application/json", status=200
        )

    @http.route("/loki/api/record/<int:index>", type="http", auth="public", cors="*")
    def api_record_detail(self, index, **kw):
        records = _get_record_list()
        record = None
        for r in records:
            if r["index"] == index:
                record = r
                break
        if not record:
            return http.Response(
                json.dumps({"error": "Record not found"}),
                content_type="application/json",
                status=404,
            )
        json_path = os.path.join(_DATA_DIR, record["json_file"])
        try:
            with open(json_path, "r") as f:
                data = f.read()
        except FileNotFoundError:
            data = json.dumps({"error": "File not found"})
        return http.Response(data, content_type="application/json", status=200)
