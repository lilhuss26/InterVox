from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from src.api.routers.interview import interview_bp
from src.api.routers.report import report_bp
from src.api.sockets.interview import socketio

UI_DIR = Path(__file__).resolve().parents[2] / "ui"


def create_app():
    app = Flask(__name__, static_folder=str(UI_DIR), static_url_path="/ui")

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/ping")
    def ping():
        return jsonify({"pong": True}), 200

    app.register_blueprint(interview_bp, url_prefix="/api/interview")
    app.register_blueprint(report_bp, url_prefix="/api/interview")
    socketio.init_app(app, cors_allowed_origins="*")
    return app
