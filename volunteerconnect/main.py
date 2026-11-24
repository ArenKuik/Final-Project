"""The homepage and the logged-in dashboard."""
from flask import Blueprint, render_template

from .decorators import login_required

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    return render_template("index.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    # Placeholder: each role's real dashboard comes with later features.
    return render_template("dashboard.html")
