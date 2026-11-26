import os
from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("端口", "8500"))
    # threaded=True：收尾“并发双请求打同一把端点锁”在同一进程内真并发
    app.run(host="0.0.0.0", port=port, threaded=True)
