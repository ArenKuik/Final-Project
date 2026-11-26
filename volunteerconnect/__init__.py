"""VolunteerConnect application factory.

create_app() builds the Flask app: it reads config from environment
variables, turns on CSRF protection, makes sure the database schema exists,
seeds the coordinator, and registers the routes.
"""
import os

from dotenv import load_dotenv
from flask import Flask, g, render_template
from flask_wtf.csrf import CSRFProtect

from . import db

csrf = CSRFProtect()


def _require_env(name):
    """Return an environment variable, or stop with a clear message if it is missing."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def create_app(test_config=None):
    # Locally, read variables from .env. On Cloud Run there is no .env file,
    # and the variables come from the service configuration instead.
    load_dotenv()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_require_env("SECRET_KEY"),
        DATABASE_URL=_require_env("DATABASE_URL"),
        DEBUG=os.environ.get("FLASK_DEBUG", "0") == "1",
        # Session cookie: only sent over HTTPS, hidden from JavaScript,
        # and not sent on cross-site POSTs.
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    # Every POST form must carry a CSRF token (used by later features).
    csrf.init_app(app)

    # Create any missing tables and the coordinator account.
    db.init_db(app.config["DATABASE_URL"])

    from .auth import bp as auth_bp, load_current_user
    from .forms import LogoutForm
    from .main import bp as main_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)

    # Load the logged-in user (or None) into g.user before every request.
    app.before_request(load_current_user)

    @app.context_processor
    def inject_user():
        # Makes `current_user` and the navbar's logout form available in every template.
        return {"current_user": g.get("user"), "logout_form": LogoutForm()}

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("403.html"), 403

    return app
