"""
weflow_headless.py — weflow-core Node.js 子进程管理器

负责：
- 启动 weflow-core (node dist/index.js)
- 将子进程 stdout 转发到当前控制台
- 等待 HTTP API 就绪
- 优雅停止（Ctrl+C）
"""

import subprocess
import threading
import time
import requests
import os
import sys
import signal


class WeFlowCore:
    """weflow-core 子进程管理器"""

    def __init__(self, project_dir: str):
        self.cwd = os.path.join(project_dir, "weflow-core")
        self.config = os.path.join(project_dir, "config.json")
        self.proc: subprocess.Popen | None = None
        self.api_base = "http://127.0.0.1:5031"
        self._reader_thread: threading.Thread | None = None
        self._started = False
        # connect 模式下为 True：weflow-core 由前端脚本独立启动，本类不拥有其生命周期
        self._attached = False

    # ============================================================
    # 📺 日志转发
    # ============================================================
    def _forward_logs(self):
        """守护线程：逐行读取子进程输出并打印"""
        assert self.proc is not None
        for line in iter(self.proc.stdout.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            print(f"[weflow] {text}", flush=True)
        self.proc.stdout.close()
        self.proc.wait()

    # ============================================================
    # 🚀 启动
    # ============================================================
    def start(self, timeout: int = 30) -> bool:
        """启动 weflow-core 并等待就绪"""
        if not os.path.isdir(self.cwd):
            print("[weflow] ❌ weflow-core 目录不存在", flush=True)
            return False

        dist_js = os.path.join(self.cwd, "dist", "index.js")
        if not os.path.isfile(dist_js):
            print(f"[weflow] ❌ 未找到 {dist_js}", flush=True)
            print("[weflow]    请先运行: cd weflow-core && npm install && node esbuild.config.mjs", flush=True)
            return False

        print("[weflow] 正在启动 weflow-core...", flush=True)

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        # 使用 WeFlow.exe（node.exe 重命名，绕过 DLL 进程校验）
        exe_name = "WeFlow.exe" if sys.platform == "win32" else "node"
        node_exe = os.path.join(self.cwd, exe_name)
        if not os.path.isfile(node_exe):
            # 兜底：使用系统 node
            node_exe = "node"

        self.proc = subprocess.Popen(
            [node_exe, "dist/index.js", self.config],
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            creationflags=creationflags,
        )

        self._reader_thread = threading.Thread(
            target=self._forward_logs, daemon=True
        )
        self._reader_thread.start()

        # 等待 API 就绪
        if self.wait_ready(timeout):
            return True
        if self.proc.poll() is not None:
            print("[weflow] ❌ weflow-core 异常退出", flush=True)
        else:
            print("[weflow] ❌ weflow-core 启动超时", flush=True)
        return False

    def connect(self, timeout: int = 60) -> bool:
        """连接模式：不 spawn 子进程，只等待独立启动的 weflow-core 就绪。

        前后端分离架构下，weflow-core 由 start_frontend.bat 启动，
        后端（main.py）只通过 HTTP/SSE 与其通信。
        """
        print(f"[weflow] 连接模式：等待 weflow-core 就绪（由前端脚本独立启动）...", flush=True)
        if self.wait_ready(timeout):
            self._attached = True
            return True
        print("[weflow] ❌ 等待 weflow-core 超时", flush=True)
        print("[weflow]    请先运行 start_frontend.bat 启动前端服务", flush=True)
        return False

    def wait_ready(self, timeout: int = 30) -> bool:
        """轮询 /health 直到 weflow-core 就绪"""
        port = self._read_api_port()
        self.api_base = f"http://127.0.0.1:{port}"

        for i in range(timeout * 2):
            try:
                r = requests.get(f"{self.api_base}/health", timeout=1)
                if r.status_code == 200:
                    data = r.json()
                    # 验证响应来自 weflow-core（防止误判其他服务）
                    if data.get("service") == "weflow-core":
                        print(f"[weflow] ✅ weflow-core 就绪 ({self.api_base})", flush=True)
                        self._started = True
                        return True
                    else:
                        print(f"[weflow] ⚠️ 端口 {port} 已被占用 ({data.get('service', 'unknown')})，等待 weflow-core...", flush=True)
            except Exception:
                pass
            if self.proc is not None and self.proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def _read_api_port(self) -> int:
        """从 config.json 读取 API 端口"""
        try:
            import json
            with open(self.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return int((cfg.get("weflow") or {}).get("apiPort", 5031))
        except Exception:
            return 5031

    @property
    def is_running(self) -> bool:
        if self._attached:
            # connect 模式：不拥有进程，以健康检查为准
            try:
                r = requests.get(f"{self.api_base}/health", timeout=1)
                return r.status_code == 200
            except Exception:
                return False
        return self._started and self.proc is not None and self.proc.poll() is None

    # ============================================================
    # 🛑 停止
    # ============================================================
    def stop(self):
        """优雅停止 weflow-core（connect 模式下为 no-op，不杀独立进程）"""
        if self._attached:
            self._started = False
            return
        if self.proc is None or self.proc.poll() is not None:
            self._started = False
            return

        print("\n[weflow] 正在停止 weflow-core...", flush=True)
        try:
            if sys.platform == "win32":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("[weflow] 强制终止...", flush=True)
            self.proc.kill()
            self.proc.wait()

        print("[weflow] 已停止", flush=True)
        self._started = False
