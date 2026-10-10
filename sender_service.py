"""
前端发送服务（独立进程）

职责：通过 HTTP JSON（127.0.0.1:9999/wxSend）接收发送请求，
用 UIA（pyautogui/win32gui）驱动微信窗口发出消息。

与后端（main.py）完全解耦：后端热重载/重启/崩溃都不影响本进程。
启动方式：python sender_service.py   （或由 start_frontend.bat 启动）
"""
import json
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 强制 UTF-8 输出，防止 GBK 控制台打印 emoji 炸掉主线程
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _setup_file_logging():
    """在 wechat_hook_server 的 basicConfig 之前配置日志：文件按天滚动（留 7 天）。
    注意：root 一旦已有 handler，wechat_hook_server 的 basicConfig(level=INFO) 会变 no-op，
    导致 root 停在默认 WARNING 级别、INFO 全丢，所以这里显式设置级别。"""
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = TimedRotatingFileHandler(
        os.path.join(log_dir, "sender.log"),
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root.addHandler(handler)


def main():
    config_path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[sender-service] config.json 读取失败：{e}，使用空配置继续", flush=True)
        config = {}

    _setup_file_logging()

    from core.wechat_sender.wechat_hook_server import run_server

    host = "127.0.0.1"
    port = int(((config.get("frontend") or {}).get("sender_port")) or 9999)
    print(f"[sender-service] 前端发送服务启动: http://{host}:{port}/wxSend", flush=True)
    run_server(host, port, config)


if __name__ == "__main__":
    main()
