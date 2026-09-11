"""
proton_auth_plugin — Saleor external-auth plugin for phone OTP login.

Deliberately minimal: this package's entry point name (see pyproject.toml's
`[project.entry-points."saleor.plugins"]`) is appended verbatim to Django's
INSTALLED_APPS by saleor/settings.py's plugin-discovery loop, so this must be
importable as a (bare, model-free) Django app — this empty __init__.py is
sufficient for that, nothing else is required.
"""
