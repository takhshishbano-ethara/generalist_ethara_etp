# Pod-only worker code. NOT imported by the Odoo addon and intentionally
# NOT added to ``models/__init__.py``. ``run_prd.py`` is executed directly as
# the entrypoint of the per-job Kubernetes worker pod.
