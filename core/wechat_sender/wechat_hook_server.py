# -*- coding: utf-8 -*-
"""
微信消息 HTTP Hook 接口
版本：v1.0.0
创建日期：2026-03-08
功能：提供 HTTP 接口接收 POST 请求来发送微信消息
"""

import hmac
import json
import logging
import os
import re
import tempfile
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from time import sleep
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any

import pyautogui
import win32gui

from core.wechat_sender.wechat_sender_v3 import WeChatSenderV3
from utils.file_down import (
    VOICE_TEMP_DIR,
    download_file,
    download_voice_file,
    is_in_voice_temp_dir,
    safe_url_for_log,
)
from core.wechat_sender.file_copy import copy_file_to_clipboard

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 请求体大小上限（2MB）
MAX_BODY_BYTES = 2 * 1024 * 1024

# API token 缓存：sender_service 可能先于 main.py 启动，此时 config.json 尚未写入
# token（main.py 启动时自动生成并回写），按 mtime 缓存重读以消解该竞态
_TOKEN_CACHE = {"mtime": None, "token": ""}
_TOKEN_CACHE_LOCK = threading.Lock()

# =========================
# 🛡️ 出站内容安全审查（移植自旧OCR bot）
# 敏感词库: data/sensitive_words.txt (7481词, 开源全类别词库)
# 命中敏感词或AI拒答文本时返回None，调用方替换为中性兜底话术
# =========================
_SENSITIVE_LOCK = threading.Lock()
_SENSITIVE_SET = None
_REJECT_PATTERNS = (
    "rejected because it was considered high risk",
    "高风险",
    "内容违规",
    "安全审核",
    "无法显示",
    "content policy",
)


def _load_sensitive_words():
    global _SENSITIVE_SET
    with _SENSITIVE_LOCK:
        if _SENSITIVE_SET is not None:
            return _SENSITIVE_SET
        words = set()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        path = os.path.join(base_dir, "data", "sensitive_words.txt")
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    w = line.strip()
                    if w and not w.startswith("#"):
                        words.add(w.lower())
        except Exception as e:
            logger.warning(f"[安全审查] 敏感词库加载失败({e})，仅启用拒答文本拦截")
        # 忽略名单：词库中过于宽泛、误杀正常用语的词（如"信息"）
        ignore_path = os.path.join(base_dir, "data", "sensitive_words_ignore.txt")
        ignored = set()
        try:
            with open(ignore_path, "r", encoding="utf-8") as f:
                for line in f:
                    w = line.strip()
                    if w and not w.startswith("#"):
                        ignored.add(w.lower())
            if ignored:
                words -= ignored
                logger.info(f"[安全审查] 忽略名单生效: {sorted(ignored)}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[安全审查] 忽略名单加载失败({e})")
        logger.info(f"[安全审查] 敏感词库已加载: {len(words)} 个词")
        _SENSITIVE_SET = words
        return words


def _outbound_content_filter(content):
    """出站文本审查。返回 (内容或None, 命中词或None)。
    命中时内容为 None（调用方替换兜底话术），命中词用于日志与后端展示。"""
    text = str(content or "").strip()
    if not text:
        return content, None
    lowered = text.lower()
    for pat in _REJECT_PATTERNS:
        if pat in lowered:
            return None, f"拒答文本:{pat}"
    # 纯ASCII词(十六进制串/英文缩写等)必须作为独立单词出现才算命中：
    # 否则 "0x17da" 会因子串包含 "17da" 被误杀(实测事故)
    ascii_tokens = set(re.findall(r"[0-9a-z]+", lowered))
    for w in _load_sensitive_words():
        if not w:
            continue
        if w.isascii():
            if len(w) >= 3 and w in ascii_tokens:
                return None, w
        elif w in lowered:
            return None, w
    return content, None



class WeChatHookHandler(BaseHTTPRequestHandler):
    """微信 Hook HTTP 请求处理器"""
    
    # 类级别的发送器实例（延迟初始化）
    _sender = None
    _sender_lock = threading.Lock()
    _sender_config = {}
    # GUI 自动化（切窗口、键盘注入、Shift 长按等）不能并发，发送区段全局串行
    _send_op_lock = threading.Lock()
    
    def log_message(self, format, *args):
        """访问日志：客户端 IP + 请求行（屏蔽 ?token= 参数值，防止 token 写入日志）"""
        try:
            line = re.sub(r'([?&]token=)[^&\s]*', r'\1***', self.requestline)
            logger.info(f"[HTTP] {self.client_address[0]} {line}")
        except Exception:
            pass

    def _get_sender(self) -> WeChatSenderV3:
        """获取或创建微信发送器实例"""
        if WeChatHookHandler._sender is None:
            with WeChatHookHandler._sender_lock:
                if WeChatHookHandler._sender is None:
                    WeChatHookHandler._sender = WeChatSenderV3(WeChatHookHandler._sender_config)
        return WeChatHookHandler._sender

    def _get_expected_token(self) -> str:
        """获取服务端期望的 token：优先取启动配置；为空时按 mtime 缓存重读
        项目根 config.json（sender_service 可能先于 main.py 生成 token 之前启动）。"""
        token = str(WeChatHookHandler._sender_config.get("token") or "").strip()
        if token:
            return token
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        config_path = os.path.join(base_dir, "config.json")
        with _TOKEN_CACHE_LOCK:
            try:
                mtime = os.path.getmtime(config_path)
                if _TOKEN_CACHE["mtime"] != mtime:
                    with open(config_path, "r", encoding="utf-8") as f:
                        _TOKEN_CACHE["token"] = str(
                            json.load(f).get("token") or "").strip()
                    _TOKEN_CACHE["mtime"] = mtime
            except Exception as e:
                logger.warning(f"[鉴权] config.json token 读取失败：{e}")
            return _TOKEN_CACHE["token"]

    def _check_auth(self) -> bool:
        """校验请求 token（X-Token 请求头 / Authorization: Bearer / ?token= 参数）。
        通过返回 True；失败时已自行回复错误响应，返回 False。"""
        expected = self._get_expected_token()
        if not expected:
            self._send_error_response(
                "服务端未配置 token",
                "服务端未配置 token，请在 config.json 顶层设置 \"token\" 后重启服务",
                status_code=503,
            )
            return False
        client_token = str(self.headers.get('X-Token') or "").strip()
        if not client_token:
            auth = str(self.headers.get('Authorization') or "")
            if auth.startswith('Bearer '):
                client_token = auth[len('Bearer '):].strip()
        if not client_token:
            client_token = (parse_qs(urlparse(self.path).query).get('token') or [""])[0].strip()
        if not client_token or not hmac.compare_digest(
                client_token.encode('utf-8'), expected.encode('utf-8')):
            self._send_error_response(
                "鉴权失败",
                "缺失或无效的 token（可用 X-Token 请求头、Authorization: Bearer 或 ?token= 参数传入）",
                status_code=401,
            )
            return False
        return True

    @staticmethod
    def _allowed_send_roots():
        """本地文件发送白名单根目录（normcase + abspath 归一化后）。"""
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        roots = [
            os.path.join(base_dir, "output"),
            # auto/picture.py 对称图输出目录（相对项目根）
            os.path.join(base_dir, "data", "pics"),
            # utils/file_down.py 统一语音临时目录及其上级下载目录
            VOICE_TEMP_DIR,
            os.path.join(tempfile.gettempdir(), "WechatRobot"),
        ]
        cfg = WeChatHookHandler._sender_config or {}
        # plugins/anima.py 生成的图像来自 ComfyUI 输出目录
        comfy_out = (cfg.get("comfy") or {}).get("output_dir")
        if comfy_out:
            roots.append(str(comfy_out))
        # 表情包目录（core/worker.py、core/dispatcher.py 的 animation 发送）
        emoji_dir = (cfg.get("llm") or {}).get("emoji_dir")
        if emoji_dir:
            emoji_dir = str(emoji_dir)
            if not os.path.isabs(emoji_dir):
                emoji_dir = os.path.join(base_dir, emoji_dir)
            roots.append(emoji_dir)
        # config.json 中显式追加的允许目录
        for item in (cfg.get("security") or {}).get("allowed_send_dirs") or []:
            if item:
                roots.append(str(item))
        result = []
        for root in roots:
            try:
                result.append(os.path.normcase(os.path.abspath(os.path.normpath(root))))
            except Exception:
                continue
        return result

    @staticmethod
    def _allowed_send_files():
        """白名单单个文件（不在任何允许目录之下的合法发送来源）。"""
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        files = [
            # plugins/prompt.py 的 PROMPT_FILE 直接位于项目根
            os.path.join(base_dir, "prompt.txt"),
        ]
        result = []
        for path in files:
            try:
                result.append(os.path.normcase(os.path.abspath(os.path.normpath(path))))
            except Exception:
                continue
        return result

    def _is_allowed_local_path(self, path) -> bool:
        """本地路径白名单校验：归一化后必须位于允许根目录之一，或为允许的单个文件。
        相对路径按本进程工作目录解析（生产上前端/后端均从项目根启动）。"""
        try:
            real = os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
        except Exception:
            return False
        for root in self._allowed_send_roots():
            if real == root or real.startswith(root + os.sep):
                return True
        return real in self._allowed_send_files()
    
    def do_POST(self):
        """处理 POST 请求"""
        try:
            # 解析 URL 路径
            parsed_path = urlparse(self.path)
            req_path = parsed_path.path.strip('/')

            # 验证路径格式：/wxSend 或 /wxPrepare
            if req_path not in ('wxSend', 'wxPrepare'):
                self._send_error_response("无效的路径格式", "路径应为：/wxSend 或 /wxPrepare")
                return

            # 鉴权：缺失/错误 token 一律 401（在读取请求体之前拒绝）
            if not self._check_auth():
                return

            # 读取请求体（限制大小，防止声明超大 Content-Length 耗尽内存）
            try:
                content_length = int(self.headers.get('Content-Length', 0))
            except (TypeError, ValueError):
                self._send_error_response("无效的请求头", "Content-Length 非法")
                return
            if content_length <= 0:
                self._send_error_response("缺少请求体", "请求体不能为空")
                return
            if content_length > MAX_BODY_BYTES:
                self._send_error_response(
                    "请求体过大",
                    f"请求体超过上限（{MAX_BODY_BYTES // (1024 * 1024)}MB）",
                    status_code=413,
                )
                return
            
            request_body = self.rfile.read(content_length).decode('utf-8')
            
            # 解析 JSON
            try:
                data = json.loads(request_body)
            except json.JSONDecodeError as e:
                self._send_error_response(f"JSON 解析失败", str(e))
                return
            
            # 验证必需的字段
            if 'target' not in data:
                self._send_error_response("缺少必需字段", "请求体必须包含'target'字段（聊天对象名）")
                return
            
            chat_target = data['target']
            
            # 验证聊天对象名
            if not chat_target or not isinstance(chat_target, str):
                self._send_error_response("无效的聊天对象名", "target 必须是非空字符串")
                return

            # /wxPrepare：LLM 生成期间的预切群请求（仅预热缓存，不发送任何内容）。
            # 与发送共用同一把 _send_op_lock，保证不会和粘贴/按键交错。
            #
            # 响应必须"诚实"：老问题（2026-09-26 23:09）是日志打出"已搜索并进入个人微信群聊:
            # 目标群"、窗口却还停在私聊 root，后端把预热当成功，随后的发送命中缓存直接跳过
            # 搜索，消息就发进了 root。现在 prepare_group 只有"读标题确认过"才返回 True，
            # 这里把标题校验的结论（实际标题/阶段/是否搜一搜面板）一并回给调用方。
            if req_path == 'wxPrepare':
                logger.info(f"收到预切群请求：{chat_target}")
                sender = self._get_sender()
                with WeChatHookHandler._send_op_lock:
                    ok = sender.prepare_group(chat_target)
                    info = dict(getattr(sender, "last_title_verify", None) or {})
                if ok:
                    logger.info(f"预切群成功：目标={chat_target} 实际标题={info.get('actual')!r} "
                                f"阶段={info.get('stage')!r} 标题校验={'启用' if info.get('verified') else '未启用'}")
                    self._send_success_response({
                        "status": "success",
                        "message": "预切群完成",
                        "target": chat_target,
                        "verified": bool(info.get("verified")),
                        "actual_title": info.get("actual"),
                    })
                else:
                    logger.warning(f"预切群失败：目标={chat_target} 实际标题={info.get('actual')!r} "
                                   f"阶段={info.get('stage')!r} 搜一搜面板={info.get('panel')} "
                                   f"已尝试恢复={info.get('recovered')}")
                    self._send_error_response(
                        "预切群失败",
                        f"没有真正切到目标会话：目标={chat_target} "
                        f"实际标题={info.get('actual')!r} 阶段={info.get('stage')!r}"
                        + ("（检测到“搜一搜”网页面板）" if info.get("panel") else ""),
                        status_code=500,
                    )
                return

            # 获取 content / file / voice / duration 字段
            message_content = data.get('content')
            file_path = data.get('file')
            voice_path = data.get('voice')
            duration = data.get('duration')
            voice_start = data.get('start')
            
            # 验证 content / file / voice 只能有一个有值
            has_content = message_content is not None and message_content != ''
            has_file = file_path is not None and file_path != ''
            has_voice = voice_path is not None and str(voice_path).strip() != ''

            if not has_content and not has_file and not has_voice:
                self._send_error_response(
                    "缺少消息内容",
                    "请求体必须包含'content'（消息内容）、'file'（文件路径）或'voice'（语音文件路径）之一"
                )
                return

            if (has_content and has_file) or (has_content and has_voice) or (has_file and has_voice):
                self._send_error_response(
                    "参数冲突",
                    "'content'、'file'和'voice'字段不能同时有值，请只使用其中一个"
                )
                return
            
            sender = self._get_sender()
            
            # 处理语音发送
            if has_voice:
                # 校验语音时长（1~30 秒，支持小数）
                try:
                    duration = float(duration) if duration is not None else None
                except (TypeError, ValueError):
                    self._send_error_response("无效的语音时长", "duration 必须是 1~30 之间的数字（支持小数）")
                    return
                if duration is not None and (duration < 1 or duration > 30):
                    self._send_error_response("语音时长超出限制", "duration 必须在 1~30 秒之间")
                    return
                # 校验起始秒（非负，支持小数；非法按 0 处理）
                try:
                    voice_start = float(voice_start) if voice_start is not None else None
                except (TypeError, ValueError):
                    self._send_error_response("无效的起始秒", "start 必须是非负数字（支持小数）")
                    return
                if voice_start is not None and voice_start < 0:
                    voice_start = 0.0

                local_voice_path = None
                try:
                    # 判断是否为远程文件
                    if str(voice_path).startswith('http://') or str(voice_path).startswith('https://'):
                        logger.info(f"检测到远程语音文件，开始下载：{safe_url_for_log(voice_path)}")
                        try:
                            local_voice_path = download_voice_file(voice_path)
                            logger.info(f"语音文件下载成功：{local_voice_path}")
                        except Exception as e:
                            logger.error(f"语音文件下载失败：{e}", exc_info=True)
                            self._send_error_response("语音文件下载失败", "远程语音文件下载失败，详情见服务端日志", status_code=500)
                            return
                    else:
                        # 本地文件：必须在白名单目录内，防止任意本地文件外发
                        if not self._is_allowed_local_path(voice_path):
                            logger.warning(f"[安全] 拒绝白名单外的本地语音路径：{voice_path}")
                            self._send_error_response("路径不允许", "本地语音路径不在允许的目录内", status_code=403)
                            return
                        if not os.path.exists(voice_path):
                            self._send_error_response("语音文件不存在", f"本地语音文件不存在：{voice_path}")
                            return
                        local_voice_path = voice_path

                    logger.info(f"收到语音发送请求：目标={chat_target}, 语音={local_voice_path}, 时长={duration}, 起始={voice_start}")

                    # 发送语音（GUI 自动化全局串行）
                    with WeChatHookHandler._send_op_lock:
                        success = sender.send_voice(local_voice_path, chat_target, duration, voice_start)

                    if success:
                        logger.info(f"语音发送成功：{chat_target}")
                        self._send_success_response({
                            "status": "success",
                            "message": "语音发送成功"
                        })
                    else:
                        logger.error(f"语音发送失败：{chat_target}")
                        self._send_error_response(
                            "语音发送失败",
                            "微信语音发送失败，请检查微信是否正常运行、是否已安装 VB-CABLE 并选中 CABLE Output 作为麦克风",
                            status_code=500
                        )
                finally:
                    # 发送完成后，由发送组件删除统一语音临时目录中的音频文件
                    if local_voice_path and is_in_voice_temp_dir(local_voice_path):
                        try:
                            os.remove(local_voice_path)
                            logger.info(f"已删除语音临时文件：{local_voice_path}")
                        except OSError as e:
                            logger.warning(f"删除语音临时文件失败：{local_voice_path}，原因：{e}")
                sleep(0.1)
                print("=" * 60)
                return

            # 处理文件发送
            if has_file:
                # 判断是否为远程文件
                if file_path.startswith('http://') or file_path.startswith('https://'):
                    logger.info(f"检测到远程文件，开始下载：{safe_url_for_log(file_path)}")
                    try:
                        local_file_path = download_file(file_path)
                        logger.info(f"文件下载成功：{local_file_path}")
                    except Exception as e:
                        logger.error(f"文件下载失败：{e}", exc_info=True)
                        self._send_error_response("文件下载失败", "远程文件下载失败，详情见服务端日志", status_code=500)
                        return
                else:
                    # 本地文件：必须在白名单目录内，防止任意本地文件外发
                    if not self._is_allowed_local_path(file_path):
                        logger.warning(f"[安全] 拒绝白名单外的本地文件路径：{file_path}")
                        self._send_error_response("路径不允许", "本地文件路径不在允许的目录内", status_code=403)
                        return
                    if not os.path.exists(file_path):
                        self._send_error_response("文件不存在", f"本地文件不存在：{file_path}")
                        return
                    local_file_path = file_path

                logger.info(f"收到文件发送请求：目标={chat_target}, 文件={local_file_path}")

                # 发送文件（GUI 自动化全局串行）
                with WeChatHookHandler._send_op_lock:
                    success = sender.send_file(local_file_path, chat_target)
                
                if success:
                    logger.info(f"文件发送成功：{chat_target}")
                    self._send_success_response({
                        "status": "success",
                        "message": "文件发送成功"
                    })
                else:
                    logger.error(f"文件发送失败：{chat_target}")
                    self._send_error_response(
                        "文件发送失败",
                        "微信文件发送失败，请检查微信是否正常运行",
                        status_code=500
                    )
                sleep(0.1)
                print("=" * 60)
                return
            
            # 处理文本消息发送
            if not isinstance(message_content, str):
                self._send_error_response("无效的消息内容", "content 必须是字符串")
                return
            
            logger.info(f"收到发送请求：目标={chat_target}, 消息长度={len(message_content)}")

            # ===== 出站内容安全审查（单群部署移植自旧OCR bot） =====
            filtered, matched_word = _outbound_content_filter(message_content)
            filter_info = None
            if filtered is None:
                logger.warning(f"[安全审查] 内容命中敏感词/拒答文本，已拦截: 命中={matched_word!r} "
                               f"目标={chat_target} 内容前30字={str(message_content)[:30]!r}")
                if data.get("silent_on_filter"):
                    # 被动场景（随机接话）：宁可这条不发，也不用兜底话术往群里占屏。
                    # 不发任何内容，回报 sent=False 让后端放弃并回滚接话冷却。
                    logger.warning(f"[安全审查] silent_on_filter=True，已放弃发送（目标={chat_target}）")
                    self._send_success_response({
                        "status": "filtered",
                        "sent": False,
                        "filtered": {"match": matched_word},
                    })
                    sleep(0.1)
                    print("=" * 60)
                    return
                message_content = "这个话题咱就聊到这吧，懂的都懂"
                filter_info = {"match": matched_word}
            else:
                message_content = filtered

            # 发送微信消息（GUI 自动化全局串行）
            with WeChatHookHandler._send_op_lock:
                success = sender.send_text(message_content, chat_target)

            if success:
                logger.info(f"消息发送成功：{chat_target}")
                resp = {
                    "status": "success",
                    "message": "消息发送成功"
                }
                if filter_info:
                    resp["filtered"] = filter_info
                self._send_success_response(resp)
            else:
                logger.error(f"消息发送失败：{chat_target}")
                self._send_error_response(
                    "消息发送失败",
                    "微信消息发送失败，请检查微信是否正常运行",
                    status_code=500
                )
            sleep(0.1)
            print("=" * 60)
        
        except Exception as e:
            logger.error(f"处理请求失败：{e}", exc_info=True)
            self._send_error_response("服务器内部错误", "服务器内部错误，详情见服务端日志", status_code=500)
    
    def do_GET(self):
        """处理 GET 请求（用于测试）"""
        try:
            parsed_path = urlparse(self.path)

            # /health 路径：轻量健康检查（不触碰微信窗口，免鉴权）
            if parsed_path.path.strip('/') == 'health':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({
                        "status": "ok",
                        "service": "sender",
                        "protocol_version": 1,
                    }, ensure_ascii=False).encode('utf-8'))
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
                    logger.warning(f"健康检查响应写入失败（客户端已断开）：{e}")
                return

            # /voice/rescan 路径：强制重新枚举音频设备（装驱动/热插拔后免重启）
            if parsed_path.path.strip('/') == 'voice/rescan':
                if not self._check_auth():
                    return
                from core.wechat_sender.virtual_mic import VirtualMicInjector
                VirtualMicInjector.rescan_devices()
                sender = self._get_sender()
                injector = VirtualMicInjector(
                    playback_device=sender.voice_playback_device,
                    capture_device=sender.voice_capture_device,
                )
                result = {
                    "service": "WeChat Voice Rescan API",
                    "rescanned": True,
                    "virtual_mic": injector.describe(),
                }
                self.send_response(200 if injector.is_available() else 503)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
                    logger.warning(f"响应写入失败（客户端已断开）：{e}")
                return

            # /voice/status 路径：虚拟麦克风注入器状态
            if parsed_path.path.strip('/') == 'voice/status':
                if not self._check_auth():
                    return
                from core.wechat_sender.virtual_mic import VirtualMicInjector
                sender = self._get_sender()
                injector = VirtualMicInjector(
                    playback_device=sender.voice_playback_device,
                    capture_device=sender.voice_capture_device,
                )
                result = {
                    "service": "WeChat Voice Status API",
                    "dry_run": os.environ.get('WECHAT_SENDER_DRY_RUN', '0') == '1',
                    "virtual_mic": injector.describe(),
                }
                self.send_response(200 if injector.is_available() else 503)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                return

            # /test 路径：测试微信窗口状态
            if parsed_path.path.strip('/') == 'test':
                if not self._check_auth():
                    return
                logger.info("收到测试请求")

                # 退到桌面
                pyautogui.hotkey('win', 'd')
                
                # 创建发送器实例并执行测试
                sender = self._get_sender()
                
                result = {
                    "service": "WeChat Test API",
                    "tests": []
                }
                
                # 测试 1：查找微信进程
                try:
                    if sender.find_target_process():
                        result["tests"].append({
                            "name": "微信进程查找",
                            "status": "success",
                            "message": "✅ 个人微信进程查找成功",
                            "pid": sender.wechat_pid
                        })
                    else:
                        result["tests"].append({
                            "name": "微信进程查找",
                            "status": "failed",
                            "message": "❌ 个人微信进程查找失败"
                        })
                except Exception as e:
                    result["tests"].append({
                        "name": "微信进程查找",
                        "status": "error",
                        "message": f"❌ 个人微信进程查找异常：{e}"
                    })
                
                # 测试 2：查找微信窗口
                try:
                    if sender._find_wechat_windows():
                        result["tests"].append({
                            "name": "微信窗口查找",
                            "status": "success",
                            "message": "✅ 个人微信窗口查找成功",
                            "window_hwnd": sender.main_window_hwnd,
                            "window_title": win32gui.GetWindowText(sender.main_window_hwnd) if sender.main_window_hwnd and win32gui.IsWindow(sender.main_window_hwnd) else None
                        })
                    else:
                        result["tests"].append({
                            "name": "微信窗口查找",
                            "status": "failed",
                            "message": "❌ 个人微信窗口查找失败"
                        })
                except Exception as e:
                    result["tests"].append({
                        "name": "微信窗口查找",
                        "status": "error",
                        "message": f"❌ 个人微信窗口查找异常：{e}"
                    })
                
                # 测试 3：激活窗口
                try:
                    if sender.activate_application():
                        result["tests"].append({
                            "name": "微信窗口激活",
                            "status": "success",
                            "message": "✅ 窗口激活成功"
                        })
                    else:
                        result["tests"].append({
                            "name": "微信窗口激活",
                            "status": "failed",
                            "message": "❌ 窗口激活失败"
                        })
                except Exception as e:
                    result["tests"].append({
                        "name": "微信窗口激活",
                        "status": "error",
                        "message": f"❌ 窗口激活异常：{e}"
                    })
                
                # 清理资源
                sender.cleanup()
                sleep(0.1)
                print("=" * 60)
                
                # 判断总体状态
                all_success = all(test["status"] == "success" for test in result["tests"])
                result["overall_status"] = "success" if all_success else "failed"
                
                self.send_response(200 if all_success else 500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                return
            
            # 其他路径返回 404
            self._send_error_response("未找到请求的资源", f"未知路径：{parsed_path.path}")
        
        except Exception as e:
            logger.error(f"处理 GET 请求失败：{e}", exc_info=True)
            self._send_error_response("服务器内部错误", "服务器内部错误，详情见服务端日志", status_code=500)
    
    def _send_success_response(self, data: Dict[str, Any]):
        """发送成功的 JSON 响应"""
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({**data}, ensure_ascii=False).encode('utf-8'))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            # 客户端提前断开（如超时放弃），消息本身已发出，仅降级为警告
            logger.warning(f"响应写入失败（客户端已断开）：{e}")

    def _send_error_response(self, error: str, details: str = None, status_code: int = 400):
        """发送错误的 JSON 响应"""
        try:
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()

            response = {
                "code": 1,
                "error": error
            }
            if details:
                response["details"] = details

            self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(f"错误响应写入失败（客户端已断开）：{e}")


def run_server(host: str = 'localhost', port: int = 9999, config: Dict[str, Any] = None):
    """运行 HTTP 服务器"""
    WeChatHookHandler._sender_config = config if isinstance(config, dict) else {}
    server_address = (host, port)
    logger.info(f"正在启动微信 Hook HTTP 服务器...")
    # 多线程服务器：单个慢连接不再阻塞整个发送管道；
    # GUI 发送操作由 WeChatHookHandler._send_op_lock 保证串行
    httpd = ThreadingHTTPServer(server_address, WeChatHookHandler)
    httpd.daemon_threads = True

    if WeChatHookHandler._sender_config.get("token"):
        logger.info("API token 鉴权已启用（/wxSend、/test、/voice/* 均需鉴权，/health 免鉴权）")
    else:
        logger.warning("未在启动配置中发现 token，将按 config.json 的 mtime 变化动态读取")

    logger.info("服务启动成功")
    sleep(0.1)
    print()
    print("=" * 60)
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭服务器...")
        httpd.shutdown()
        logger.info("服务器已关闭")


def main():
    """主程序入口"""
    import sys
    
    # 默认配置：仅监听本机回环。确需局域网访问时显式传参指定网卡 IP，
    # 并务必确保 config.json 已配置 token（所有非 /health 端点均需鉴权）
    default_host = '127.0.0.1'
    default_port = 9999
    
    # 解析命令行参数
    host = default_host
    port = default_port
    
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"无效的端口号：{sys.argv[1]}")
            sys.exit(1)
    
    if len(sys.argv) > 2:
        host = sys.argv[2]
    
    # 启动服务器
    run_server(host, port)


if __name__ == "__main__":
    main()
