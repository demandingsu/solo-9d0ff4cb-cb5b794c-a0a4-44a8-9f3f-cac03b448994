"""Flask 应用工厂。"""

from __future__ import annotations

from flask import Flask

from .db import close_db, init_database, resolve_database_path


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=resolve_database_path(),
        JSON_ENSURE_ASCII=False,
    )
    if test_config:
        app.config.update(test_config)

    # Flask 3 的 JSON Provider
    try:
        app.json.ensure_ascii = False  # type: ignore[attr-defined]
    except Exception:
        pass

    init_database(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    from .api import bp as api_bp
    from .archive_api import bp as archive_api_bp
    from .archive_web import bp as archive_web_bp
    from .web import bp as web_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(archive_web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(archive_api_bp, url_prefix="/api/archive")
    return app
