from odoo import http
from odoo.http import request
from odoo.addons.api_auth_gateway.controllers.utility import return_Response, validate_token


_URL_COLUMNS = [
    {"key": "url", "label": "Website URL", "type": "string"},
    {"key": "category", "label": "Category", "type": "string"},
    {"key": "added_by", "label": "Added by", "type": "string"},
    {"key": "assigned_ql_name", "label": "Assigned QL", "type": "string"},
    {"key": "source", "label": "Source", "type": "string"},
    {"key": "created_date", "label": "Date added", "type": "date"},
]

_URL_ROWS = [
    {"id": 1, "url": "https://example.com/login", "name": "https://example.com/login", "category": "Authentication", "added_by": "Priya Sharma", "assigned_ql_name": "Anjali Singh", "source": "Single", "created_date": "2026-05-30", "task_id": "KEN-0001"},
    {"id": 2, "url": "https://shop.example.com/checkout", "name": "https://shop.example.com/checkout", "category": "E-commerce", "added_by": "Rahul Verma", "assigned_ql_name": "Vikram Iyer", "source": "Bulk Excel", "created_date": "2026-06-01", "task_id": "KEN-0002"},
    {"id": 3, "url": "https://docs.example.com/api", "name": "https://docs.example.com/api", "category": "Documentation", "added_by": "Priya Sharma", "assigned_ql_name": "Anjali Singh", "source": "Single", "created_date": "2026-06-02", "task_id": "KEN-0003"},
    {"id": 4, "url": "https://mail.example.com/inbox", "name": "https://mail.example.com/inbox", "category": "Communication", "added_by": "Administrator", "assigned_ql_name": "Vikram Iyer", "source": "Bulk Excel", "created_date": "2026-06-02", "task_id": "KEN-0004"},
    {"id": 5, "url": "https://crm.example.com/leads", "name": "https://crm.example.com/leads", "category": "CRM", "added_by": "Rahul Verma", "assigned_ql_name": "Anjali Singh", "source": "Single", "created_date": "2026-06-03", "task_id": "KEN-0005"},
    {"id": 6, "url": "https://analytics.example.com/reports", "name": "https://analytics.example.com/reports", "category": "Analytics", "added_by": "Priya Sharma", "assigned_ql_name": "Vikram Iyer", "source": "Single", "created_date": "2026-06-04", "task_id": "KEN-0006"},
    {"id": 7, "url": "https://forum.example.com/threads", "name": "https://forum.example.com/threads", "category": "Community", "added_by": "Administrator", "assigned_ql_name": "Anjali Singh", "source": "Bulk Excel", "created_date": "2026-06-05", "task_id": "KEN-0007"},
    {"id": 8, "url": "https://files.example.com/share", "name": "https://files.example.com/share", "category": "File Sharing", "added_by": "Rahul Verma", "assigned_ql_name": "Vikram Iyer", "source": "Single", "created_date": "2026-06-06", "task_id": "KEN-0008"},
]


_REPOSITORY_ROWS = [
    {"id": 1, "name": "arrow", "repo_path": "arrow-py/arrow", "kai_id": "KAI-001", "language": "python", "pass_rate": 92.5, "cost": 12.40, "stubbing_mode": "auto", "build_status": "DONE", "error_summary": "", "added_on": "2026-05-15"},
    {"id": 2, "name": "httpx", "repo_path": "encode/httpx", "kai_id": "KAI-002", "language": "python", "pass_rate": 88.0, "cost": 8.20, "stubbing_mode": "manual", "build_status": "DONE", "error_summary": "", "added_on": "2026-05-18"},
    {"id": 3, "name": "typer", "repo_path": "fastapi/typer", "kai_id": "KAI-003", "language": "python", "pass_rate": 95.1, "cost": 5.60, "stubbing_mode": "auto", "build_status": "DONE", "error_summary": "", "added_on": "2026-05-20"},
    {"id": 4, "name": "requests", "repo_path": "psf/requests", "kai_id": "KAI-004", "language": "python", "pass_rate": 90.3, "cost": 14.10, "stubbing_mode": "auto", "build_status": "RUNNING", "error_summary": "", "added_on": "2026-06-01"},
    {"id": 5, "name": "jjwt", "repo_path": "jwtk/jjwt", "kai_id": "KAI-005", "language": "java", "pass_rate": 78.4, "cost": 18.90, "stubbing_mode": "manual", "build_status": "FAILED", "error_summary": "Dependency resolution failed", "added_on": "2026-06-02"},
    {"id": 6, "name": "fastjson", "repo_path": "alibaba/fastjson", "kai_id": "KAI-006", "language": "java", "pass_rate": 82.7, "cost": 22.30, "stubbing_mode": "auto", "build_status": "DONE", "error_summary": "", "added_on": "2026-06-03"},
    {"id": 7, "name": "pytest", "repo_path": "pytest-dev/pytest", "kai_id": "KAI-007", "language": "python", "pass_rate": 96.8, "cost": 7.80, "stubbing_mode": "auto", "build_status": "DONE", "error_summary": "", "added_on": "2026-06-04"},
    {"id": 8, "name": "okhttp", "repo_path": "square/okhttp", "kai_id": "KAI-008", "language": "java", "pass_rate": 85.5, "cost": 16.40, "stubbing_mode": "manual", "build_status": "DONE", "error_summary": "", "added_on": "2026-06-05"},
]


_TRAJECTORY_ROWS = [
    {"id": 1, "label": "trajectory-claude-001.json", "created": "2026-06-07T10:15:00", "tasker": "Neha Gupta"},
    {"id": 2, "label": "trajectory-gpt-002.json", "created": "2026-06-07T11:42:00", "tasker": "Arjun Reddy"},
    {"id": 3, "label": "trajectory-claude-003.json", "created": "2026-06-07T14:08:00", "tasker": "Sneha Kapoor"},
    {"id": 4, "label": "trajectory-1pa-004.json", "created": "2026-06-07T15:30:00", "tasker": "Karan Mehta"},
    {"id": 5, "label": "trajectory-golden-005.json", "created": "2026-06-08T09:12:00", "tasker": "Divya Nair"},
    {"id": 6, "label": "trajectory-1pb-006.json", "created": "2026-06-08T10:55:00", "tasker": "Aditya Joshi"},
    {"id": 7, "label": "trajectory-claude-007.json", "created": "2026-06-08T12:20:00", "tasker": "Neha Gupta"},
    {"id": 8, "label": "trajectory-gpt-008.json", "created": "2026-06-08T13:45:00", "tasker": "Arjun Reddy"},
]


_BATCH_ROWS = [
    {"id": 1, "batch_id": "BATCH-001", "trajectory_count": 12, "added_by": "Priya Sharma", "submitted_on": "2026-06-05T09:30:00", "trajectories": [{"id": "TRAJ-001", "repo_model": "claude/arrow", "cost": 3.20}, {"id": "TRAJ-002", "repo_model": "gpt/arrow", "cost": 2.80}]},
    {"id": 2, "batch_id": "BATCH-002", "trajectory_count": 8, "added_by": "Rahul Verma", "submitted_on": "2026-06-05T14:15:00", "trajectories": [{"id": "TRAJ-003", "repo_model": "claude/httpx", "cost": 2.10}, {"id": "TRAJ-004", "repo_model": "gpt/httpx", "cost": 1.95}]},
    {"id": 3, "batch_id": "BATCH-003", "trajectory_count": 15, "added_by": "Administrator", "submitted_on": "2026-06-06T10:00:00", "trajectories": [{"id": "TRAJ-005", "repo_model": "claude/typer", "cost": 4.50}, {"id": "TRAJ-006", "repo_model": "gpt/typer", "cost": 4.10}]},
    {"id": 4, "batch_id": "BATCH-004", "trajectory_count": 10, "added_by": "Priya Sharma", "submitted_on": "2026-06-06T16:25:00", "trajectories": [{"id": "TRAJ-007", "repo_model": "claude/requests", "cost": 3.75}]},
    {"id": 5, "batch_id": "BATCH-005", "trajectory_count": 6, "added_by": "Rahul Verma", "submitted_on": "2026-06-07T08:45:00", "trajectories": [{"id": "TRAJ-008", "repo_model": "claude/jjwt", "cost": 5.20}]},
    {"id": 6, "batch_id": "BATCH-006", "trajectory_count": 14, "added_by": "Administrator", "submitted_on": "2026-06-07T13:10:00", "trajectories": [{"id": "TRAJ-009", "repo_model": "gpt/fastjson", "cost": 4.80}]},
    {"id": 7, "batch_id": "BATCH-007", "trajectory_count": 9, "added_by": "Priya Sharma", "submitted_on": "2026-06-08T07:30:00", "trajectories": [{"id": "TRAJ-010", "repo_model": "claude/pytest", "cost": 2.40}]},
    {"id": 8, "batch_id": "BATCH-008", "trajectory_count": 11, "added_by": "Rahul Verma", "submitted_on": "2026-06-08T11:55:00", "trajectories": [{"id": "TRAJ-011", "repo_model": "claude/okhttp", "cost": 3.90}]},
]


class KenseiProjectExtrasController(http.Controller):

    @http.route('/api/v1/kensei_ext/urls_added', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    @validate_token
    def kensei_ext_urls_added(self, **kwargs):
        return return_Response(
            message='Success',
            status=200,
            data={'urls': _URL_ROWS, 'columns': _URL_COLUMNS, 'total': len(_URL_ROWS)},
        )

    @http.route('/api/v1/kensei_ext/repository', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    @validate_token
    def kensei_ext_repository(self, **kwargs):
        return return_Response(
            message='Success',
            status=200,
            data={'repositories': _REPOSITORY_ROWS, 'total': len(_REPOSITORY_ROWS)},
        )

    @http.route('/api/v1/kensei_ext/trajectory', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    @validate_token
    def kensei_ext_trajectory(self, **kwargs):
        return return_Response(
            message='Success',
            status=200,
            data={'trajectories': _TRAJECTORY_ROWS, 'total': len(_TRAJECTORY_ROWS)},
        )

    @http.route('/api/v1/kensei_ext/batch', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    @validate_token
    def kensei_ext_batch(self, **kwargs):
        return return_Response(
            message='Success',
            status=200,
            data={'batches': _BATCH_ROWS, 'total': len(_BATCH_ROWS)},
        )
