"""Entry point. gunicorn runs `app:app`, which is the Flask app built below."""
from volunteerconnect import create_app

app = create_app()

if __name__ == "__main__":
    # Local run only; debug mode comes from FLASK_DEBUG (off by default).
    app.run()
