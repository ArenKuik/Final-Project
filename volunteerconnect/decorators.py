"""Permission decorators. These run on the server for every request to the
route; hiding a button in a template is never a permission check.

Usage:
    @bp.route("/dashboard")
    @login_required
    def dashboard(): ...

    @bp.route("/schools/new", methods=["POST"])
    @role_required("coordinator")
    def create_school(): ...
"""
from functools import wraps

from flask import abort, g, redirect, url_for


def login_required(view):
    """Send logged-out visitors to the login page."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    """Require login, then return 403 unless the user has one of `roles`."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
