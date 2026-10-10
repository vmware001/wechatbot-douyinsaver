"""/image 命令插件：调用云端文生图（智谱 CogView / 百炼千问 Qwen-Image、通义万相）并把图片发到当前群。

    /image <提示词>

协议/模型/尺寸全在 config.json 的 image_gen 里配（protocol 可选 zhipu / sync / async / openai，
默认模型见 DEFAULT_MODEL），详见 services/image_gen.py 顶部的文档依据与实测依据。

不扣积分、不限流；但每条请求都带自己的 request_id：服务端只按这条请求自己的
task_id / 自己那条图片 URL 落地，只下载到以 request_id 命名的临时文件，发送前再用
owns_image() 核对一次。超时/失败一律明确失败、只回一句自然语言，绝不发任何图——
旧 anima「超时后把别人之前生成的图当结果发到群里」的情况在这里结构上不会出现。

image_gen.enabled 为 false 或没填 api_key 时，只回提示，不抛异常。
"""

import threading
import uuid

from core.sender import send
from services.image_gen import (
    REASON_NOT_CONFIGURED,
    REASON_TIMEOUT,
    ImageGenService,
)

COMMAND = "/image"

# 提示词长度：千问 3.0 文档是「推荐不超过 4500 Token」，qwen-image-2.0 系列 1300 Token、
# 更老的模型 800 Token（超出由服务端自动截断，不报错）。这里按字符数做个宽松的前置截断。
MAX_PROMPT_CHARS = 2000

# 取目标群的兜底写法与 plugins/song.py 保持一致
DEFAULT_TARGET = "文件传输助手"

_service = None
_service_lock = threading.RLock()


def init(config):
    """初始化服务实例（dispatcher 加载插件后调用）。"""
    global _service
    with _service_lock:
        try:
            service = ImageGenService(config or {})
            _service = service
            if service.ready:
                state = "已启用"
            elif service.enabled:
                state = "已启用但未配置 api_key"
            else:
                state = "未启用（image_gen.enabled = false）"
            print(
                f"[PLUGIN] /image 初始化完成：{state}，"
                f"protocol={service.protocol} model={service.model} size={service.size} "
                f"单价≈¥{service.price_per_image_cny:.4f}/张",
                flush=True,
            )
        except Exception as exc:
            _service = None
            print(f"[PLUGIN] /image 初始化失败: {exc}", flush=True)


def _get_service():
    with _service_lock:
        return _service


def _get_target(context):
    if not context:
        return DEFAULT_TARGET
    return context.get("group") or context.get("user") or DEFAULT_TARGET


def _failure_message(result):
    """失败回复只说人话：错误码、HTTP 状态码、堆栈等内部细节只留在 run.log。"""
    reason = result.get("reason")
    if reason == REASON_NOT_CONFIGURED:
        return "出图功能还没配置好，先找管理员开一下"
    if reason == REASON_TIMEOUT:
        return "这次出图等太久了，换个描述再试试"
    return "出图失败了，换个描述再试试"


def handle(content, context):
    """处理 /image 命令：content 即提示词（命令后面的全部文本）。"""
    prompt = str(content or "").strip()
    if not prompt:
        return "在 /image 后面写上想画的内容，比如：/image 一只坐在窗台上的橘猫"

    prompt = prompt[:MAX_PROMPT_CHARS]

    service = _get_service()
    if service is None or not service.ready:
        print("[IMAGE GEN] 未配置或未启用，拒绝请求", flush=True)
        return "出图功能还没配置好，先找管理员开一下"

    request_id = uuid.uuid4().hex
    image_path = None

    try:
        result = service.generate(prompt, request_id=request_id)

        if not result.get("success"):
            return _failure_message(result)

        # 防串图最后一道闸：request_id 与登记文件必须都对得上，否则宁可不发
        image_path = result.get("image_path")
        if result.get("request_id") != request_id or not service.owns_image(request_id, image_path):
            image_path = None  # 不是本请求的文件：既不发也不删，交给它的归属方处理
            print(
                f"[IMAGE GEN ERROR] request={request_id} 结果与本请求不匹配，已丢弃未发送",
                flush=True,
            )
            return "出图失败了，换个描述再试试"

        target = _get_target(context)
        ok, reason = send(target, file_path=image_path, mode="wechat_file")
        if not ok:
            print(f"[IMAGE GEN ERROR] request={request_id} 发送图片失败: {reason}", flush=True)
            return "图出来了但没发出去，稍后再试试"

        print(f"[IMAGE GEN] 已发送 request={request_id} target={target}", flush=True)
        # 图片本身就是回复，不再另发文本（与 plugins/song.py 的语音一致）
        return None

    except Exception as exc:
        print(
            f"[IMAGE GEN ERROR] request={request_id} 处理异常: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return "出图失败了，换个描述再试试"

    finally:
        # 发完即删：成功、失败、异常都不在本地留下临时文件
        if image_path:
            service.delete_image(image_path, request_id=request_id)
