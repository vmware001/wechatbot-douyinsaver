import atexit
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from ..source import MusicSourceError

_logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DAEMON_PATH = Path(__file__).resolve().parents[1] / "lx_daemon.mjs"


class LxDaemonError(MusicSourceError):
    def __init__(self, source_id: str, reason: str):
        super().__init__(source_id or "lx_daemon", reason)


class LxDaemonProcess:
    """Manage one long-lived lx_daemon.mjs process for a script file."""

    def __init__(
        self,
        source_id: str,
        script_path: Path,
        *,
        node_executable: str,
        daemon_path: Path | None = None,
        # 第三方 JS 脚本自动更新默认关闭：更新源被劫持/上游投毒时，
        # 下载的脚本会以 bot 进程权限被 Node 直接执行（供应链 RCE）。
        # 确需自动更新时，在 music 配置里显式设 auto_update_scripts = true。
        auto_update_scripts: bool = False,
        update_min_interval_hours: float = 24.0,
        default_timeout_ms: int = 20000,
        popen=None,
    ):
        self.source_id = str(source_id)
        self.script_path = Path(script_path).resolve()
        self.node_executable = node_executable
        self.daemon_path = Path(daemon_path or DEFAULT_DAEMON_PATH).resolve()
        self.auto_update_scripts = bool(auto_update_scripts)
        self.update_min_interval_ms = max(
            int(float(update_min_interval_hours) * 3600 * 1000),
            0,
        )
        self.default_timeout_ms = max(int(default_timeout_ms), 1000)
        self._popen = popen or subprocess.Popen
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._request_id = 0
        self._started = False
        self.supported_platforms: list[str] = []

        if not self.script_path.is_file():
            raise LxDaemonError(self.source_id, f"脚本不存在：{self.script_path}")
        if not self.daemon_path.is_file():
            raise LxDaemonError(self.source_id, f"守护进程不存在：{self.daemon_path}")

    def _build_command(self) -> list[str]:
        command = [
            self.node_executable,
            str(self.daemon_path),
            "--script-path",
            str(self.script_path),
            "--default-timeout-ms",
            str(self.default_timeout_ms),
            "--update-min-interval-ms",
            str(self.update_min_interval_ms),
        ]
        if not self.auto_update_scripts:
            command.append("--no-auto-update")
        return command

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            try:
                self._proc = self._popen(
                    self._build_command(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(PROJECT_ROOT),
                    env=os.environ.copy(),
                    bufsize=1,
                )
            except OSError as exc:
                raise LxDaemonError(self.source_id, f"无法启动 Node 守护进程：{exc}") from exc

            started = self._read_startup_event()
            if not started.get("ok"):
                detail = str(started.get("error") or "daemon startup failed")
                self._terminate_process()
                raise LxDaemonError(self.source_id, detail)
            self._started = True
            self.supported_platforms = [
                str(platform)
                for platform in (started.get("platforms") or [])
                if str(platform).strip()
            ]
            _logger.info(
                "[LX DAEMON] started source=%s script=%s md5=%s version=%s platforms=%s",
                self.source_id,
                self.script_path.name,
                started.get("scriptMd5"),
                started.get("version"),
                ",".join(self.supported_platforms) or "-",
            )

    def _readline_with_timeout(self, timeout: float) -> str:
        """带超时的 stdout.readline。

        裸 readline 在 daemon 假死（不退出也不输出）时会永久阻塞，
        而 request() 全程持有 _lock——该音源之后所有请求都会被排队锁死。
        超时后调用方负责 _terminate_process()，进程被杀后 reader 线程随管道关闭退出。
        """
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        result = {}

        def _reader():
            try:
                result["line"] = proc.stdout.readline()
            except Exception as exc:
                result["error"] = exc

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        reader.join(max(float(timeout), 1.0))
        if reader.is_alive():
            raise LxDaemonError(
                self.source_id, f"守护进程响应超时（{timeout:.0f}s），判定假死"
            )
        if "error" in result:
            raise LxDaemonError(self.source_id, f"读取守护进程失败：{result['error']}")
        return result.get("line") or ""

    def _read_startup_event(self) -> dict:
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            line = self._readline_with_timeout(30.0)
        except LxDaemonError:
            self._terminate_process()
            raise
        if not line:
            stderr = self._drain_stderr()
            raise LxDaemonError(self.source_id, stderr or "daemon exited during startup")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise LxDaemonError(self.source_id, f"daemon startup output invalid: {line}") from exc

    def _drain_stderr(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        return (self._proc.stderr.read() or "").strip()

    def _terminate_process(self) -> None:
        proc = self._proc
        self._proc = None
        self._started = False
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _ensure_running(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._started = False
            self.start()

    def request(self, action: str, timeout: float | None = None, **payload) -> dict:
        with self._lock:
            self._ensure_running()
            assert self._proc is not None
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None

            self._request_id += 1
            request_id = self._request_id
            message = {
                "id": request_id,
                "action": action,
                **payload,
            }
            if timeout is not None:
                message["timeoutMs"] = max(int(float(timeout) * 1000), 1000)

            try:
                self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                self._terminate_process()
                raise LxDaemonError(self.source_id, f"写入守护进程失败：{exc}") from exc

            # 裸 readline 改为带超时读取：超时即终止 daemon，让上层切换其它音源
            effective_timeout = (
                max(float(timeout), 1.0)
                if timeout is not None
                else self.default_timeout_ms / 1000.0
            ) + 5.0
            try:
                line = self._readline_with_timeout(effective_timeout)
            except LxDaemonError:
                self._terminate_process()
                raise
            except Exception as exc:
                self._terminate_process()
                raise LxDaemonError(self.source_id, f"读取守护进程失败：{exc}") from exc

            if not line:
                stderr = self._drain_stderr()
                self._terminate_process()
                raise LxDaemonError(self.source_id, stderr or "守护进程无响应")

            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self._terminate_process()
                raise LxDaemonError(self.source_id, f"守护进程返回非 JSON：{line}") from exc

            if response.get("id") != request_id:
                raise LxDaemonError(self.source_id, "守护进程响应 ID 不匹配")

            if not response.get("ok"):
                raise LxDaemonError(
                    self.source_id,
                    str(response.get("error") or "守护进程请求失败"),
                )
            return response

    def music_url(
        self,
        platform: str,
        quality: str,
        music_info: dict,
        timeout: float,
    ) -> str:
        if not self._started:
            self.start()
        response = self.request(
            "musicUrl",
            timeout=timeout,
            platform=platform,
            quality=quality,
            musicInfo=music_info,
        )
        url = str(response.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise LxDaemonError(self.source_id, "守护进程返回了无效 URL")
        return url

    def get_supported_platforms(self) -> list[str]:
        if not self._started:
            self.start()
        return list(self.supported_platforms)

    def close(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            proc = self._proc
            if proc.poll() is None and proc.stdin is not None:
                try:
                    self._request_id += 1
                    proc.stdin.write(
                        json.dumps({"id": self._request_id, "action": "shutdown"})
                        + "\n"
                    )
                    proc.stdin.flush()
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
            self._proc = None
            self._started = False


class LxDaemonPool:
    """Shared pool of per-script LX daemons."""

    def __init__(
        self,
        *,
        node_executable: str,
        daemon_path: Path | None = None,
        auto_update_scripts: bool = False,
        update_min_interval_hours: float = 24.0,
        default_timeout_ms: int = 20000,
        popen=None,
    ):
        self.node_executable = node_executable
        self.daemon_path = daemon_path
        self.auto_update_scripts = auto_update_scripts
        self.update_min_interval_hours = update_min_interval_hours
        self.default_timeout_ms = default_timeout_ms
        self._popen = popen
        self._daemons: dict[str, LxDaemonProcess] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, music_config=None, popen=None):
        music_config = music_config if isinstance(music_config, dict) else {}
        node_executable = _find_node_executable(music_config.get("node_executable"))
        return cls(
            node_executable=node_executable,
            auto_update_scripts=music_config.get("auto_update_scripts", False),
            update_min_interval_hours=float(
                music_config.get("update_min_interval_hours", 24)
            ),
            default_timeout_ms=max(
                int(float(music_config.get("resolve_timeout_seconds", 20)) * 1000),
                1000,
            ),
            popen=popen,
        )

    def get_daemon(self, source_id: str, script_path: Path) -> LxDaemonProcess:
        key = str(Path(script_path).resolve())
        with self._lock:
            daemon = self._daemons.get(key)
            if daemon is None:
                daemon = LxDaemonProcess(
                    source_id,
                    script_path,
                    node_executable=self.node_executable,
                    daemon_path=self.daemon_path,
                    auto_update_scripts=self.auto_update_scripts,
                    update_min_interval_hours=self.update_min_interval_hours,
                    default_timeout_ms=self.default_timeout_ms,
                    popen=self._popen,
                )
                self._daemons[key] = daemon
            return daemon

    def shutdown(self) -> None:
        with self._lock:
            for daemon in self._daemons.values():
                try:
                    daemon.close()
                except Exception as exc:
                    _logger.warning("[LX DAEMON] shutdown failed: %s", exc)
            self._daemons.clear()


_POOLS: list[LxDaemonPool] = []
_POOLS_LOCK = threading.RLock()


def register_pool(pool: LxDaemonPool) -> LxDaemonPool:
    with _POOLS_LOCK:
        if pool not in _POOLS:
            _POOLS.append(pool)
    return pool


def shutdown_all_pools() -> None:
    with _POOLS_LOCK:
        while _POOLS:
            pool = _POOLS.pop()
            pool.shutdown()


atexit.register(shutdown_all_pools)


def _find_node_executable(configured: str | None = None) -> str:
    if configured:
        configured_path = shutil.which(configured)
        if configured_path:
            return configured_path
        raise LxDaemonError("lx_daemon", f"未找到 Node 可执行文件：{configured}")
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise LxDaemonError("lx_daemon", "未找到 Node.js，请安装后确保 node 在 PATH 中")
