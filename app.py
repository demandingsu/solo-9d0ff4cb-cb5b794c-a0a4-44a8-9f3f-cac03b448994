"""gunicorn / 本地启动入口：``app:app``。"""

from hydrology import create_app

app = create_app()

if __name__ == "__main__":
    # 本地调试：python app.py
    app.run(host="127.0.0.1", port=8000, debug=False)
