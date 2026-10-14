"""The hub: stores what collectors push, serves the web UI and the JSON API on loopback.

Public interface: ``app.create_app``, ``server.serve``, the API bodies in ``api_models``, the
repositories in ``store``, and ``langfuse.LangfuseExporter`` for trace export.
"""
