# -*- coding: utf-8 -*-
"""ethara_project budget-dashboard HTTP API (Group C — heaviest phase).

Ports the 8 budget-dashboard endpoints from
``etp_projects/controllers/project_budget_dashboard.py`` onto the ethara
data models. All aggregation logic lives on the ``ethara.project.budget.dashboard``
abstract model (methods ``_dash_*``); these controllers only parse the POST
body, dispatch to the model, and wrap the result in the standard response
envelope.

Canonical routes (all POST, ``type='http'``, ``auth='none'``, ``csrf=False``,
``cors='*'`` + ``@validate_token``), sub-pathed under
``project_budget_dashboard/*`` to match the Flutter constant paths:

  * /api/v1/ethara_project/project_budget_dashboard/kpis
  * /api/v1/ethara_project/project_budget_dashboard/actual_vs_estimated
  * /api/v1/ethara_project/project_budget_dashboard/spend_by_model
  * /api/v1/ethara_project/project_budget_dashboard/phase_budget
  * /api/v1/ethara_project/project_budget_dashboard/burn_rate
  * /api/v1/ethara_project/project_budget_dashboard/project_table
  * /api/v1/ethara_project/project_budget_dashboard/project_performance_list
  * /api/v1/ethara_project/project_budget_dashboard/phase_health

The response envelope and every JSON key are identical to the etp responses.
"""

import json
import logging

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.api_auth_gateway.controllers.utility import (
    return_Response,
    validate_token,
)

_logger = logging.getLogger(__name__)

DASHBOARD_MODEL = "ethara.project.budget.dashboard"

BASE_ROUTE = "/api/v1/ethara_project/project_budget_dashboard"


def _read_json_body():
    """Parse the request body as a JSON object, returning {} on any failure."""
    raw = b""
    try:
        raw = request.httprequest.stream.read() or b""
    except Exception:
        raw = request.httprequest.data or b""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class EtharaProjectBudgetDashboardController(http.Controller):

    def _dashboard(self):
        return request.env[DASHBOARD_MODEL].sudo()

    # -------------------------------------------------------------- kpis
    @http.route(
        BASE_ROUTE + "/kpis",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def kpis(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_kpis(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception("ethara_project project_budget_dashboard.kpis failed")
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # ------------------------------------------------ actual_vs_estimated
    @http.route(
        BASE_ROUTE + "/actual_vs_estimated",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def actual_vs_estimated(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_actual_vs_estimated(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.actual_vs_estimated failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # ----------------------------------------------------- spend_by_model
    @http.route(
        BASE_ROUTE + "/spend_by_model",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def spend_by_model(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_spend_by_model(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.spend_by_model failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # ------------------------------------------------------- phase_budget
    @http.route(
        BASE_ROUTE + "/phase_budget",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def phase_budget(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_phase_budget(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.phase_budget failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # ---------------------------------------------------------- burn_rate
    @http.route(
        BASE_ROUTE + "/burn_rate",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def burn_rate(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_burn_rate(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.burn_rate failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # ------------------------------------------------------- project_table
    @http.route(
        BASE_ROUTE + "/project_table",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def project_table(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_project_table(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.project_table failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # -------------------------------------------- project_performance_list
    @http.route(
        BASE_ROUTE + "/project_performance_list",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def project_performance_list(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_project_performance_list(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.project_performance_list failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )

    # ------------------------------------------------------- phase_health
    @http.route(
        BASE_ROUTE + "/phase_health",
        type="http", auth="none", methods=["POST"], csrf=False, cors="*",
    )
    @validate_token
    def phase_health(self, **params):
        try:
            jdata = _read_json_body()
            payload = self._dashboard()._dash_phase_health(jdata)
            return return_Response(message="OK", status=200, data={"data": payload})
        except (UserError, ValidationError) as e:
            return return_Response(message=str(e), status=400)
        except Exception as e:
            _logger.exception(
                "ethara_project project_budget_dashboard.phase_health failed"
            )
            return return_Response(
                message="Something went wrong.", status=400, errors=[str(e)],
            )
