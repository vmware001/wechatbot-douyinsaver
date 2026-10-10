"""云端文生图服务：调用云端文生图模型（智谱 CogView / 百炼千问 Qwen-Image、通义万相）。

四种协议，靠 config.json 的 image_gen.protocol 切换（默认 sync）：

  protocol="sync"（默认，文档标注「推荐」）—— DashScope 原生同步，一次请求直接出图
      POST {base_url}/api/v1/services/aigc/multimodal-generation/generation
      Body: {"model": "qwen-image-3.0-pro",
             "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
             "parameters": {"size": "1024*1024", "n": 1}}
      图片 URL：output.choices[0].message.content[0].image（24 小时有效）

  protocol="async" —— DashScope 异步任务：建任务 + 轮询（通义万相 wan2.6 走这条）
      POST {base_url}/api/v1/services/aigc/image-generation/generation
           Header: X-DashScope-Async: enable（缺这个头会报
                   "current user api does not support synchronous calls"）
      GET  {base_url}/api/v1/tasks/{task_id}
      output.task_status: PENDING / RUNNING → 继续轮询；SUCCEEDED → 同样取
                          output.choices[0].message.content[0].image；
                          FAILED / CANCELED / UNKNOWN → 明确失败

  protocol="openai" —— OpenAI 兼容同步（只有 qwen-image-3.0 / 3.0-pro 等新模型支持）
      POST {base_url}/compatible-mode/v1/images/generations
      Body: {"model": ..., "prompt": ..., "size": "1024x1024", "n": 1}
            ← 参数平铺在顶层，size 用字母 x 分隔（文档专门警告过与 DashScope 的 * 不同）
      图片 URL：data[0].url；错误体是 {"error": {"message", "type", "param", "code"}}

  protocol="zhipu" —— 智谱开放平台 CogView（cogview-3-flash 免费档，最快最省）
      POST {base_url}/api/paas/v4/images/generations
      Body: {"model": "cogview-3-flash", "prompt": ..., "size": "1024x1024"}
            ← 平铺在顶层，size 用字母 x；实测可用的最小请求体就是这三个字段，不带 n
      图片 URL：data[0].url（需再下载）；错误体是 {"error": {"message": ...}}
                或顶层 code/message，两种都兜住

依据（zhipu 这条是 2026-09-26 实测跑通的请求，不是凭记忆）：
  · POST https://open.bigmodel.cn/api/paas/v4/images/generations
    Header: Authorization: Bearer <key>；Content-Type: application/json
    Body: {"model":"cogview-3-flash","prompt":"一只坐在窗台上的橘猫，午后阳光，写实照片",
           "size":"1024x1024"}
    → HTTP 200、7.9s（含下载），响应 data[0].url 下载得 88282 字节图片
  · 智谱开放平台的 key 是账号级的：同一把 GLM key 可直接调出图模型，无需另开服务。
  · cogview-3-flash 属免费用途档；同平台另有 cogview-3、cogview-4-250304 等付费档，
    model 做成可配即可切换。
  · 免费额度上限、是否要求实名等计费细则本次未核实，以智谱控制台为准。

────────────────────────────────────────────────────────────────────────
事实依据（下面百炼那部分是 2026-09-25 抓取的官方文档，不是凭记忆）：
  · 千问-文生图            https://help.aliyun.com/zh/model-studio/qwen-image-api
  · 千问-图像生成与编辑3.0  https://help.aliyun.com/zh/model-studio/qwen-image-generation-and-editing-api-reference

  1) 模型名：qwen-image-3.0-pro / qwen-image-3.0（两个页面的模型表都标「推荐」，同时支持
     文生图 T2I 与图生图 I2I）；同表还有 qwen-image-2.0-pro / qwen-image-2.0 / qwen-image-max /
     qwen-image-plus / qwen-image 等。默认取 qwen-image-3.0-pro。
  2) 同步还是异步：Qwen-Image **有同步接口**（DashScope 原生同步被文档标为「推荐」；
     3.0 还额外提供 OpenAI 兼容同步）。这与万相 wan2.6（只有异步任务轮询）最大的不同 ——
     所以默认走 sync，同时把异步那条路完整保留给万相和批量场景。
  3) 端点：DashScope 原生同步与万相同一个地址
     /api/v1/services/aigc/multimodal-generation/generation；3.0 的异步建任务地址
     /api/v1/services/aigc/image-generation/generation 也与万相同一个；轮询统一
     /api/v1/tasks/{task_id}；OpenAI 兼容在 /compatible-mode/v1/images/generations。
     （万相另一个旧端点 /services/aigc/text2image/image-synthesis 用 input.prompt，
       支持 qwen-image-plus / qwen-image，本模块的 async 路不覆盖它 —— 这两个模型改用 sync 即可。）
  4) 域名与 Key：和文本模型同一个百炼域名（现有 https://dashscope.aliyuncs.com 仍可用，或业务空间
     专属域名 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com），也用同一把百炼 API Key。
     文档强调「模型、endpoint URL 和 API Key 必须属于同一地域，跨地域调用会失败」。
  5) 请求字段：DashScope 是嵌套结构 input.messages[0].{role, content:[{text}]} +
     parameters.{size, n, negative_prompt, prompt_extend, watermark, seed}；
     OpenAI 兼容把所有字段平铺在顶层（model / prompt / n / size / prompt_extend / ...），
     没有 input、parameters 嵌套。
  6) size：DashScope 格式为 宽*高（星号），OpenAI 兼容格式为 宽x高（字母 x）。
     qwen-image-3.0 系列总像素 512*512 ~ 2048*2048、宽高比 1:8~8:1，1024*1024 合法
     —— 对比万相 wan2.6 要求总像素 ≥1280*1280，1024*1024 会被拒。
     qwen-image-max / qwen-image-plus 是固定档位（1664*928 默认 / 1472*1104 / 1328*1328 /
     1104*1472 / 928*1664），换这些模型时 size 要跟着改。
  7) n：文档默认 1（万相文档的默认值是 4），千问支持 1~6 张且按张计费 —— 这里固定 n=1。
  8) 图片 URL 24 小时过期，必须立刻下载转存；文档还建议同步调用把客户端超时设到 600 秒
     （生成通常 20~60 秒），本模块用 timeout_seconds 当总预算、单次 HTTP 再按剩余预算裁剪。
────────────────────────────────────────────────────────────────────────

可靠性设计（与第一版一致、与协议无关）：
  · 每个请求一个 request_id：只按这条请求自己的 task_id / 自己那条 URL 落地，
    绝不扫描共享目录取「最新文件」，从结构上杜绝旧 anima「超时后把别人之前生成的图发出去」的串图问题；
  · owns_image() 在发送前再核对一次归属，对不上宁可不发；
  · 失败一律明确失败（reason = not_configured / timeout / error），调用方只回自然中文，
    错误码、HTTP 状态码等内部细节只进 run.log。
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 复用项目已有的下载防护：仅 http/https 且主机不得解析到内网/回环地址（SSRF 防护）
from utils.file_down import _check_url_safety, safe_url_for_log


DEFAULT_MODEL = "qwen-image-3.0-pro"
DEFAULT_SIZE = "1024*1024"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
# 智谱 CogView 的域名与百炼完全不同：protocol=zhipu 且没写 base_url 时用这个兜底
DEFAULT_ZHIPU_BASE_URL = "https://open.bigmodel.cn"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_POLL_INTERVAL_SECONDS = 5.0

# 四种协议（config.json 的 image_gen.protocol）
PROTOCOL_SYNC = "sync"
PROTOCOL_ASYNC = "async"
PROTOCOL_OPENAI = "openai"
PROTOCOL_ZHIPU = "zhipu"
_ALL_PROTOCOLS = (PROTOCOL_SYNC, PROTOCOL_ASYNC, PROTOCOL_OPENAI, PROTOCOL_ZHIPU)
# 允许写得更随意一些，统一映射回上面四个值
_PROTOCOL_ALIASES = {
    "sync": PROTOCOL_SYNC,
    "synchronous": PROTOCOL_SYNC,
    "dashscope": PROTOCOL_SYNC,
    "dashscope-sync": PROTOCOL_SYNC,
    "async": PROTOCOL_ASYNC,
    "dashscope-async": PROTOCOL_ASYNC,
    "task": PROTOCOL_ASYNC,
    "tasks": PROTOCOL_ASYNC,
    "openai": PROTOCOL_OPENAI,
    "openai-compatible": PROTOCOL_OPENAI,
    "compatible": PROTOCOL_OPENAI,
    "compatible-mode": PROTOCOL_OPENAI,
    "zhipu": PROTOCOL_ZHIPU,
    "zhipu-ai": PROTOCOL_ZHIPU,
    "bigmodel": PROTOCOL_ZHIPU,
    "glm": PROTOCOL_ZHIPU,
    "cogview": PROTOCOL_ZHIPU,
}

# 端点路径
SYNC_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
ASYNC_CREATE_PATH = "/api/v1/services/aigc/image-generation/generation"
OPENAI_PATH = "/compatible-mode/v1/images/generations"
ZHIPU_PATH = "/api/paas/v4/images/generations"
TASK_PATH_TEMPLATE = "/api/v1/tasks/{task_id}"

# 智谱要的 size 是「宽x高」（字母 x），用它校验，避免把星号格式原样发过去
_SIZE_X_RE = re.compile(r"^\d+x\d+$")

# 单次 HTTP 请求的超时上限；整体耗时一律由 timeout_seconds 兜住，
# 同步出图按文档建议放宽到 600 秒（生成本身就在这一个请求里完成）
_SYNC_TIMEOUT_SECONDS = 600.0
_ZHIPU_TIMEOUT_SECONDS = 120.0
_CREATE_TIMEOUT_SECONDS = 30.0
_POLL_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 30.0

_STATUS_SUCCEEDED = "SUCCEEDED"
_STATUS_FAILED = ("FAILED", "CANCELED", "CANCELLED", "UNKNOWN")

_IMAGE_TEMP_DIR = os.path.join(tempfile.gettempdir(), "WechatRobot", "image_gen")

# 图片魔数表：既用来挡掉「HTTP 200 但其实是 JSON/HTML 报错页」，
# 也用来按真实格式定临时文件扩展名（实测智谱返回的 watermark URL 以 .png 结尾、
# 内容却是 JPEG，顶着 .png 后缀发出去不保险）
_IMAGE_MAGIC_EXT = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)

# 失败原因分类：给调用方判断该回哪句话，避免去猜错误文本
REASON_NOT_CONFIGURED = "not_configured"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"


class ImageGenError(RuntimeError):
    """出图过程中的内部错误：错误文案只进日志，不往群里发。"""


class ImageGenNotConfigured(ImageGenError):
    """未启用或未配置 api_key。"""


class ImageGenTimeout(ImageGenError):
    """整体耗时超过 timeout_seconds。"""


def _to_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _to_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _to_non_negative_float(value, default):
    """允许 0 的浮点配置。

    _to_float 会把 0/负数换成默认值，而 price_per_image_cny 恰恰要能配 0
    （免费档，如智谱 cogview-3-flash），所以单价单独用这个解析。
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _brief(text, limit=80):
    """日志用短文本：换行压平、超长截断。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _detect_image_ext(head):
    """按首部字节判断图片扩展名；不是已知图片返回 None。"""
    for magic, ext in _IMAGE_MAGIC_EXT:
        if head.startswith(magic):
            return ext
    return None


def _looks_like_image(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return _detect_image_ext(head) is not None


def _normalize_protocol(value, model):
    """配置里的 protocol → 四个标准值之一。

    没写（旧配置）时按模型名推断：
      · wan*        → async（万相只有异步任务验证过）
      · cogview*/glm* → zhipu（智谱的模型名一眼可辨，key 也是智谱的）
      · 其余（qwen-image* 等）→ sync（DashScope 原生同步，千问文档标推荐）
    这样老配置不改也不会崩。
    """
    protocol = _PROTOCOL_ALIASES.get(str(value or "").strip().lower())
    if protocol:
        return protocol
    model_name = str(model or "").strip().lower()
    if model_name.startswith("wan"):
        return PROTOCOL_ASYNC
    if model_name.startswith("cogview") or model_name.startswith("glm"):
        return PROTOCOL_ZHIPU
    return PROTOCOL_SYNC


class ImageGenService:
    """智谱 CogView / 千问 Qwen-Image / 通义万相 文生图服务（四种协议共用一套可靠性外壳）。

    每次调用 generate() 都带一个独立的 request_id：服务内部只用这条请求自己的 task_id /
    自己那条图片 URL，任何一步失败都直接返回失败——不会去猜、也不会去捡别的请求的产物。
    """

    def __init__(self, config=None):
        cfg = (config or {}).get("image_gen") or {}

        self.enabled = bool(cfg.get("enabled", False))
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.model = str(cfg.get("model") or "").strip() or DEFAULT_MODEL
        self.size = str(cfg.get("size") or "").strip() or DEFAULT_SIZE
        self.protocol = _normalize_protocol(cfg.get("protocol"), self.model)
        # 单张价格（元），只用于日志里的费用估算。0 = 免费档（如智谱 cogview-3-flash），
        # 所以这里必须用允许 0 的解析。官方 qwen-image-3.0-pro 1K 输出约 0.25 元/张。
        self.price_per_image_cny = _to_non_negative_float(
            cfg.get("price_per_image_cny"), 0.25
        )
        # base_url 的兜底随协议走：智谱与百炼是两个完全不同的域名
        default_base_url = (
            DEFAULT_ZHIPU_BASE_URL if self.protocol == PROTOCOL_ZHIPU else DEFAULT_BASE_URL
        )
        self.base_url = (
            str(cfg.get("base_url") or "").strip() or default_base_url
        ).rstrip("/")
        self.timeout_seconds = _to_float(cfg.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS)
        self.max_image_bytes = _to_int(cfg.get("max_image_bytes"), DEFAULT_MAX_IMAGE_BYTES)
        self.poll_interval_seconds = _to_float(
            cfg.get("poll_interval_seconds"), DEFAULT_POLL_INTERVAL_SECONDS
        )

        # request_id -> 该请求自己下载的临时文件路径。发送前用 owns_image() 再核对一次，
        # 保证「结果只按 request_id 对应」不靠调用方自觉。
        self._request_files = {}
        self._lock = threading.RLock()

    # =========================================================
    # 状态
    # =========================================================
    @property
    def ready(self):
        """enabled 且填了 api_key 才算配置好。"""
        return bool(self.enabled and self.api_key)

    def owns_image(self, request_id, image_path):
        """该路径是否正是这条 request 自己下载的文件。"""
        if not request_id or not image_path:
            return False
        with self._lock:
            registered = self._request_files.get(str(request_id))
        if not registered:
            return False
        try:
            return os.path.normcase(os.path.abspath(str(registered))) == os.path.normcase(
                os.path.abspath(str(image_path))
            )
        except Exception:
            return False

    def delete_image(self, image_path, request_id=None):
        """删除本地临时文件（发完即删）；带 request_id 时同时注销登记。"""
        if request_id:
            with self._lock:
                if self._request_files.get(str(request_id)) == image_path:
                    self._request_files.pop(str(request_id), None)
        return self._remove_file(image_path)

    # =========================================================
    # 主流程
    # =========================================================
    def generate(self, prompt, request_id=None):
        """生成一张图并下载到本地临时文件。

        返回 {"success": True, "image_path", "request_id", "protocol", "task_id"(仅异步),
              "elapsed_seconds"}
        或   {"success": False, "error"(内部原因，仅日志), "reason", "request_id", "elapsed_seconds"}
        """
        request_id = str(request_id or uuid.uuid4().hex)
        started_at = time.monotonic()
        deadline = started_at + self.timeout_seconds
        task_id = None

        if not self.ready:
            reason = "image_gen 未启用" if not self.enabled else "image_gen 未配置 api_key"
            return self._failure(request_id, started_at, reason, REASON_NOT_CONFIGURED)

        print(
            f"[IMAGE GEN] 开始生成 request={request_id} protocol={self.protocol} "
            f"model={self.model} size={self.size} prompt={_brief(prompt)}",
            flush=True,
        )

        try:
            if self.protocol == PROTOCOL_ASYNC:
                task_id = self._create_task(prompt, request_id, deadline)
                image_url = self._wait_for_image(task_id, request_id, deadline)
            elif self.protocol == PROTOCOL_OPENAI:
                image_url = self._request_openai(prompt, request_id, deadline)
            elif self.protocol == PROTOCOL_ZHIPU:
                image_url = self._request_zhipu(prompt, request_id, deadline)
            else:
                image_url = self._request_sync(prompt, request_id, deadline)
            image_path = self._download_image(image_url, request_id, deadline)
        except ImageGenNotConfigured as exc:
            return self._failure(request_id, started_at, str(exc), REASON_NOT_CONFIGURED)
        except ImageGenTimeout as exc:
            return self._failure(request_id, started_at, str(exc), REASON_TIMEOUT)
        except ImageGenError as exc:
            return self._failure(request_id, started_at, str(exc), REASON_ERROR)
        except Exception as exc:
            # 任何意外都不许把异常抛给插件/worker：一律明确失败，不发任何图
            return self._failure(
                request_id, started_at, f"{type(exc).__name__}: {exc}", REASON_ERROR
            )

        elapsed = time.monotonic() - started_at
        print(
            f"[IMAGE GEN] 完成 用时 {elapsed:.1f}s request={request_id} "
            f"protocol={self.protocol} task={task_id or '-'} "
            f"费用≈¥{self.price_per_image_cny:.4f}（{self.price_per_image_cny:.4f} 元/张 × 1 张）",
            flush=True,
        )
        return {
            "success": True,
            "image_path": image_path,
            "request_id": request_id,
            "protocol": self.protocol,
            "task_id": task_id,
            "elapsed_seconds": elapsed,
        }

    @staticmethod
    def _failure(request_id, started_at, reason, kind):
        elapsed = time.monotonic() - started_at
        print(
            f"[IMAGE GEN ERROR] request={request_id} 用时 {elapsed:.1f}s "
            f"原因({kind}): {_brief(reason, 200)}",
            flush=True,
        )
        return {
            "success": False,
            "error": str(reason),
            "reason": kind,
            "request_id": request_id,
            "elapsed_seconds": elapsed,
        }

    # =========================================================
    # 请求体：DashScope 原生（同步/异步共用）与 OpenAI 兼容
    # =========================================================
    def _dashscope_body(self, prompt):
        parameters = {"n": 1}  # n 固定 1：按张计费，文档默认值万相是 4、千问是 1
        # size 传 auto 或留空时干脆不带该字段：DashScope 会按提示词自动推荐分辨率
        if self.size and self.size.lower() != "auto":
            parameters["size"] = self.size
        return {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
            "parameters": parameters,
        }

    def _openai_body(self, prompt):
        body = {"model": self.model, "prompt": prompt, "n": 1}
        if self.size:
            # OpenAI 兼容协议用字母 x，DashScope 用星号
            body["size"] = self.size.lower().replace("*", "x")
        return body

    def _dashscope_headers(self, asynchronous=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if asynchronous:
            headers["X-DashScope-Async"] = "enable"
        return headers

    # =========================================================
    # 同步（DashScope 原生 / OpenAI 兼容）
    # =========================================================
    def _request_sync(self, prompt, request_id, deadline):
        """DashScope 原生同步：一次 POST 直接返回图片 URL（文档标注「推荐」）。"""
        url = f"{self.base_url}{SYNC_PATH}"
        print(f"[IMAGE GEN] DashScope 同步调用 request={request_id} path={SYNC_PATH}", flush=True)
        data = self._post_json(
            url,
            self._dashscope_body(prompt),
            self._dashscope_headers(),
            deadline,
            "同步出图",
            _SYNC_TIMEOUT_SECONDS,
        )
        image_url = self._extract_image_url(data, f"同步响应 request={request_id}")
        print(
            f"[IMAGE GEN] 同步出图返回图像 request={request_id} url={safe_url_for_log(image_url)}",
            flush=True,
        )
        return image_url

    def _request_openai(self, prompt, request_id, deadline):
        """OpenAI 兼容同步：参数平铺、size 用字母 x（仅 qwen-image-3.0 / 3.0-pro 等支持）。"""
        url = f"{self.base_url}{OPENAI_PATH}"
        print(f"[IMAGE GEN] OpenAI 兼容调用 request={request_id} path={OPENAI_PATH}", flush=True)
        data = self._post_json(
            url,
            self._openai_body(prompt),
            self._dashscope_headers(),
            deadline,
            "OpenAI 兼容出图",
            _SYNC_TIMEOUT_SECONDS,
        )
        image_url = self._extract_image_url(data, f"OpenAI 兼容响应 request={request_id}")
        print(
            f"[IMAGE GEN] OpenAI 兼容出图返回图像 request={request_id} "
            f"url={safe_url_for_log(image_url)}",
            flush=True,
        )
        return image_url

    # =========================================================
    # 智谱 CogView（同一套同步语义，但域名/路径/请求体都不同）
    # =========================================================
    def _zhipu_size(self):
        """size 归一成智谱要的「宽x高」（字母 x）。

        空 / auto → 返回 None，调用方干脆不带这个字段；
        配成星号（1024*1024）会自动转成 1024x1024；
        既不是 WxH 也不是 auto 的写法直接报错，避免把错格式静默发过去。
        """
        size = (self.size or "").strip()
        if not size or size.lower() == "auto":
            return None
        normalized = size.lower().replace("*", "x")
        if not _SIZE_X_RE.match(normalized):
            raise ImageGenError(
                f"size 格式不对：{self.size!r}；智谱协议要「宽x高」（如 1024x1024）"
            )
        return normalized

    def _zhipu_body(self, prompt):
        """实测跑通的最小请求体：model + prompt + size，不带 n。

        （智谱官方文档的 CogView 请求示例同样只有这几项；实测时不传 n 也能正常出图，
          所以这里不传，避免多余的参数被拒。）
        """
        body = {"model": self.model, "prompt": prompt}
        size = self._zhipu_size()
        if size:
            body["size"] = size
        return body

    @staticmethod
    def _zhipu_headers(api_key):
        """智谱用同一套 Bearer + JSON 头（与实测请求一致）。"""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _request_zhipu(self, prompt, request_id, deadline):
        """智谱 CogView 同步出图：POST /api/paas/v4/images/generations → data[0].url。"""
        url = f"{self.base_url}{ZHIPU_PATH}"
        print(
            f"[IMAGE GEN] 智谱 CogView 调用 request={request_id} path={ZHIPU_PATH} "
            f"model={self.model}",
            flush=True,
        )
        data = self._post_json(
            url,
            self._zhipu_body(prompt),
            self._zhipu_headers(self.api_key),
            deadline,
            "智谱出图",
            _ZHIPU_TIMEOUT_SECONDS,
        )
        image_url = self._extract_image_url(data, f"智谱响应 request={request_id}")
        print(
            f"[IMAGE GEN] 智谱出图返回图像 request={request_id} url={safe_url_for_log(image_url)}",
            flush=True,
        )
        return image_url

    # =========================================================
    # 异步：建任务 + 轮询
    # =========================================================
    def _create_task(self, prompt, request_id, deadline):
        url = f"{self.base_url}{ASYNC_CREATE_PATH}"
        print(f"[IMAGE GEN] 建异步任务 request={request_id} path={ASYNC_CREATE_PATH}", flush=True)
        data = self._post_json(
            url,
            self._dashscope_body(prompt),
            self._dashscope_headers(asynchronous=True),
            deadline,
            "建任务",
            _CREATE_TIMEOUT_SECONDS,
        )
        output = data.get("output") or {}
        task_id = str(output.get("task_id") or "").strip()
        if not task_id:
            raise ImageGenError(f"建任务响应缺少 task_id: {_brief(json.dumps(data, ensure_ascii=False))}")
        print(
            f"[IMAGE GEN] 已建任务 request={request_id} task={task_id} "
            f"status={output.get('task_status')}",
            flush=True,
        )
        return task_id

    def _wait_for_image(self, task_id, request_id, deadline):
        url = f"{self.base_url}{TASK_PATH_TEMPLATE.format(task_id=task_id)}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        poll_count = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImageGenTimeout(
                    f"轮询超时：{self.timeout_seconds:g}s 内任务未完成 task={task_id}"
                )

            poll_count += 1
            status_code = None
            try:
                response = requests.get(
                    url, headers=headers, timeout=self._budget(deadline, _POLL_TIMEOUT_SECONDS)
                )
                status_code = self._status_code(response)
            except Exception as exc:
                # 单次轮询的网络抖动不致命：总预算没用完就继续轮询
                print(
                    f"[IMAGE GEN] 轮询请求失败 request={request_id} "
                    f"task={task_id}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                self._sleep_before_next_poll(deadline)
                continue

            if status_code is not None and status_code >= 500:
                print(
                    f"[IMAGE GEN] 轮询返回 HTTP {status_code}，稍后重试 request={request_id} "
                    f"task={task_id}",
                    flush=True,
                )
                self._sleep_before_next_poll(deadline)
                continue

            data = self._parse_response(response, "查询任务")
            output = data.get("output") or {}
            task_status = str(output.get("task_status") or "").upper()
            print(
                f"[IMAGE GEN] 轮询 {poll_count}: request={request_id} "
                f"task={task_id} status={task_status or '?'}",
                flush=True,
            )

            if task_status == _STATUS_SUCCEEDED:
                return self._extract_image_url(data, f"任务 task={task_id}")
            if task_status in _STATUS_FAILED:
                code = output.get("code") or data.get("code") or task_status
                message = output.get("message") or data.get("message") or ""
                raise ImageGenError(
                    f"任务 {task_status} task={task_id} code={code} message={_brief(message, 120)}"
                )

            # PENDING / RUNNING / 空状态 → 继续轮询
            self._sleep_before_next_poll(deadline)

    # =========================================================
    # 下载
    # =========================================================
    def _download_image(self, image_url, request_id, deadline):
        try:
            _check_url_safety(image_url)
        except Exception as exc:
            raise ImageGenError(f"图像地址不合法: {exc}") from exc

        os.makedirs(_IMAGE_TEMP_DIR, exist_ok=True)
        # 文件名带 request_id：每个请求一个文件，跨请求不可能互相覆盖或取错
        image_path = os.path.join(
            _IMAGE_TEMP_DIR, f"img_{request_id}_{int(time.time() * 1000)}.png"
        )
        timeout = self._budget(deadline, _DOWNLOAD_TIMEOUT_SECONDS)
        total_bytes = 0

        try:
            response = requests.get(image_url, stream=True, timeout=timeout)
            try:
                status_code = self._status_code(response)
                if status_code is not None and not 200 <= status_code < 300:
                    raise ImageGenError(f"下载图像失败 HTTP {status_code}")

                with open(image_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > self.max_image_bytes:
                            raise ImageGenError(
                                f"图像超过大小上限 {self.max_image_bytes} 字节"
                            )
                        handle.write(chunk)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except ImageGenError:
            self._remove_file(image_path)
            raise
        except Exception as exc:
            self._remove_file(image_path)
            raise ImageGenError(f"下载图像失败: {type(exc).__name__}: {exc}") from exc

        if total_bytes <= 0:
            self._remove_file(image_path)
            raise ImageGenError("下载图像为空")
        if not _looks_like_image(image_path):
            self._remove_file(image_path)
            raise ImageGenError("下载内容不是图片")
        # 按真实魔数纠正扩展名（智谱的 .png URL 实际返回 JPEG）
        image_path = self._rename_by_magic(image_path)

        with self._lock:
            self._request_files[request_id] = image_path
        print(
            f"[IMAGE GEN] 已下载 request={request_id} bytes={total_bytes} "
            f"path={image_path} url={safe_url_for_log(image_url)}",
            flush=True,
        )
        return image_path

    @staticmethod
    def _rename_by_magic(image_path):
        """按真实魔数把临时文件改成对应扩展名。

        实测智谱返回的 watermark URL 以 .png 结尾、内容却是 JPEG；顶着 .png 后缀送去微信
        不保险，所以统一按首部字节纠正（认不出格式就原样返回，不动文件）。
        """
        try:
            with open(image_path, "rb") as handle:
                head = handle.read(12)
        except OSError:
            return image_path
        ext = _detect_image_ext(head)
        if not ext:
            return image_path
        base, current_ext = os.path.splitext(image_path)
        if current_ext.lower() == ext:
            return image_path
        new_path = base + ext
        try:
            os.replace(image_path, new_path)
        except OSError as exc:
            print(f"[IMAGE GEN] 按魔数纠正扩展名失败（忽略）: {exc}", flush=True)
            return image_path
        return new_path

    # =========================================================
    # 小工具
    # =========================================================
    @staticmethod
    def _extract_image_url(data, source):
        """按文档取图片 URL：DashScope 的 choices → 老版 results → OpenAI 兼容的 data[]。"""
        output = data.get("output") or {}

        # DashScope 同步 / 异步（千问 3.0 与万相 wan2.6 都是这个结构）
        for choice in output.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            for item in message.get("content") or []:
                if isinstance(item, dict) and item.get("image"):
                    return str(item["image"])

        # 兼容旧协议（万相 wan2.5 及以下、qwen-image-plus 的异步接口）的 results[].url
        for item in output.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])

        # OpenAI 兼容协议：图片在顶层 data[].url
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])

        raise ImageGenError(
            f"{source or '响应'}里没有图像地址: {_brief(json.dumps(data, ensure_ascii=False))}"
        )

    @staticmethod
    def _status_code(response):
        try:
            return int(getattr(response, "status_code", 0))
        except (TypeError, ValueError):
            return None

    def _post_json(self, url, body, headers, deadline, action, timeout_cap):
        timeout = self._budget(deadline, timeout_cap)
        try:
            response = requests.post(url, json=body, headers=headers, timeout=timeout)
        except Exception as exc:
            raise ImageGenError(f"{action}请求失败: {type(exc).__name__}: {exc}") from exc
        return self._parse_response(response, action)

    @staticmethod
    def _parse_response(response, action):
        status_code = ImageGenService._status_code(response)
        try:
            data = response.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            raise ImageGenError(f"{action}响应不是 JSON（HTTP {status_code}）")

        # OpenAI 兼容协议的错误体：{"error": {"message", "type", "param", "code"}}
        error = data.get("error")
        if isinstance(error, dict):
            code = error.get("code") or status_code
            message = error.get("message") or ""
            param = error.get("param")
            suffix = f" param={param}" if param else ""
            raise ImageGenError(
                f"{action}失败 HTTP {status_code} code={code} "
                f"message={_brief(message, 120)}{suffix}"
            )

        # DashScope 协议的错误体：顶层 code + message（如 InvalidApiKey / DataInspectionFailed）
        if data.get("code") or (status_code is not None and status_code >= 400):
            code = data.get("code") or status_code
            message = data.get("message") or ""
            raise ImageGenError(
                f"{action}失败 HTTP {status_code} code={code} message={_brief(message, 120)}"
            )
        return data

    @staticmethod
    def _budget(deadline, cap):
        """本次 HTTP 请求允许的超时：不超过剩余总预算，也不低于 1 秒。"""
        return max(1.0, min(cap, deadline - time.monotonic()))

    def _sleep_before_next_poll(self, deadline):
        time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _remove_file(image_path):
        try:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
                print(f"[IMAGE GEN] 已删除临时文件 {image_path}", flush=True)
                return True
        except Exception as exc:
            print(f"[IMAGE GEN ERROR] 删除临时文件失败 {image_path}: {exc}", flush=True)
        return False
