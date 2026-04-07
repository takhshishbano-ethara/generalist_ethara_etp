import os
import json

from odoo import http
from odoo.http import request


class SwaggerController(http.Controller):
    @http.route(
        "/api/docs", type="http", auth="none", csrf=False, cors="*", methods=["GET"]
    )
    def swagger_ui(self, **kwargs):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ethara ETP - API Documentation</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css">
    <style>
        body { margin: 0; padding: 0; }
        #swagger-ui .topbar { display: none; }
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({
            url: '/api/docs/openapi.yaml',
            dom_id: '#swagger-ui',
            presets: [
                SwaggerUIBundle.presets.apis,
                SwaggerUIBundle.SwaggerUIStandalonePreset
            ],
            layout: 'BaseLayout',
            deepLinking: true,
            defaultModelsExpandDepth: 1,
            defaultModelExpandDepth: 1,
            docExpansion: 'list',
            filter: true,
            showExtensions: true,
            showCommonExtensions: true,
            persistAuthorization: true,
        });
    </script>
</body>
</html>"""
        return request.make_response(
            html, headers=[("Content-Type", "text/html; charset=utf-8")]
        )

    @http.route(
        "/api/docs/openapi.yaml",
        type="http",
        auth="none",
        csrf=False,
        cors="*",
        methods=["GET"],
    )
    def openapi_spec(self, **kwargs):
        spec_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static",
            "src",
            "openapi.yaml",
        )
        with open(spec_path, "r", encoding="utf-8") as f:
            content = f.read()
        return request.make_response(
            content,
            headers=[
                ("Content-Type", "text/yaml; charset=utf-8"),
                ("Access-Control-Allow-Origin", "*"),
            ],
        )
