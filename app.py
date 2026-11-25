"""本地直接启动入口：python app.py

Docker / gunicorn 使用 ``app:app``（本文件中的 app 对象）。
"""
import os

from archive_app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
