from mcstatus import JavaServer
import threading

DEFAULT_GAME_HOST = "game.sie.gdutmc.com"
_game_host = DEFAULT_GAME_HOST
_game_port = None

DEFAULT_FAKE_NAME = "Anonymous Player"
_fake_name = DEFAULT_FAKE_NAME


def configure(config=None):
    global _game_host, _game_port, _fake_name

    mc_config = {}
    if isinstance(config, dict):
        mc_config = (
            config.get("mc")
            or config.get("minecraft")
            or config.get("mc_server")
            or {}
        )

    host = mc_config.get("host") or mc_config.get("server") or DEFAULT_GAME_HOST
    port = mc_config.get("port")
    fake_name = mc_config.get("fake_name", DEFAULT_FAKE_NAME)

    _game_host = str(host).strip() or DEFAULT_GAME_HOST
    _fake_name = str(fake_name).strip() or DEFAULT_FAKE_NAME

    try:
        _game_port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        _game_port = None


def _create_server(timeout=3):
    # 显式传 timeout：底层 socket 读写都有界，查询线程不会永久阻塞
    if _game_port is None:
        return JavaServer.lookup(_game_host, timeout=timeout)
    return JavaServer(_game_host, _game_port, timeout=timeout)


# single-flight + 短缓存：并发/高频调用共享同一次实际查询，
# 防止服务器宕机期间每次调用都新建一个阻塞线程（线程泄漏）
_STATUS_LOCK = threading.Lock()
_STATUS_INFLIGHT = None  # threading.Event，非 None 表示有查询进行中
_STATUS_CACHE = {"ts": 0.0, "data": None}
_CACHE_TTL = 5.0


# =========================
# MC 状态查询（核心）
# =========================
def get_status(timeout=3):
    import time

    with _STATUS_LOCK:
        now = time.time()
        if _STATUS_CACHE["data"] is not None and now - _STATUS_CACHE["ts"] < _CACHE_TTL:
            return _STATUS_CACHE["data"]
        event = _STATUS_INFLIGHT
        if event is None:
            event = threading.Event()
            _STATUS_INFLIGHT = event
            leader = True
        else:
            leader = False

    if not leader:
        # 已有查询在跑：等它结束，读共享结果（失败则为 None）
        event.wait(timeout + 1.0)
        with _STATUS_LOCK:
            return _STATUS_CACHE["data"]

    try:
        data = _query_status_once(timeout=timeout)
    except Exception:
        data = None

    with _STATUS_LOCK:
        # 失败也缓存 _CACHE_TTL 秒，避免宕机时被高频轮询打爆
        _STATUS_CACHE["ts"] = time.time()
        _STATUS_CACHE["data"] = data
        _STATUS_INFLIGHT = None
        event.set()
    return data


def _query_status_once(timeout=3):
    result = {}
    finished = threading.Event()

    def worker():
        try:
            server = _create_server(timeout=timeout)
            status = server.status()

            # =========================
            # ✔ 修复点1：正确在线人数
            # =========================
            online_players = status.players.online
            max_players = status.players.max

            # =========================
            # ✔ 修复点2：安全处理 sample
            # =========================
            players = []
            if status.players.sample:
                for p in status.players.sample:
                    if p.name != _fake_name:
                        players.append(p.name)

            result["data"] = {
                "online": True,
                "latency_ms": round(status.latency, 2),
                "online_players": online_players,   # ✔ 用真实值
                "max_players": max_players,
                "players": players
            }

        except Exception as e:
            result["error"] = str(e)

        finally:
            finished.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    if not finished.wait(timeout):
        return None

    if "error" in result:
        return None

    return result["data"]


# =========================
# 对外 API：状态（不变）
# =========================
def status():
    return get_status()


# =========================
# 对外 API：玩家列表（不变）
# =========================
def player_list():
    data = get_status()
    if not data:
        return None
    return data.get("players", [])
