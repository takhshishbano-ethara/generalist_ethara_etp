{
    "name": "AI Services",
    "version": "19.0.1.0.0",
    "category": "Tools",
    "summary": "AI-powered document extraction using AWS Bedrock",
    "description": """
        AI Services module for extracting structured project details
        from uploaded documents (PDF, DOCX, TXT) using AWS Bedrock
        LLM (Kimi K2.5) via the Converse API.
    """,
    "author": "Ethara",
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter_data.xml",
        "views/ai_extraction_views.xml",
        "views/res_config_settings_views.xml",
        "views/menu_views.xml",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
