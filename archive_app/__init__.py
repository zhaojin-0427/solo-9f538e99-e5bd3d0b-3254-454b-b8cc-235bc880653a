"""Flask 应用工厂。"""
from __future__ import annotations

import os

from flask import Flask

from .db import init_app as init_db_app
from .db import init_db
from .views import bp, register_error_handlers


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get(
        "ARCHIVE_DB",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "archive.db"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-secret-key")
    # 模板 / 静态目录默认就在包内，显式声明便于阅读
    app.template_folder = "templates"
    app.static_folder = "static"

    init_db(app.config["DB_PATH"])
    init_db_app(app)
    app.register_blueprint(bp)
    register_error_handlers(app)

    @app.template_filter("from_json")
    def _from_json(value):
        import json
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    return app
