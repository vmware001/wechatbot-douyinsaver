"""
/anima 命令插件
用于调用 Anima 图像生成服务
"""

import os
import sys
from core.sender import send
from utils.points_manager import get_points, try_deduct_points

COMMAND = "/anima"
POINTS_COST = 8  # 每次Anima图像生成消耗的积分

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 全局服务实例
_anima_service = None
_config = {}


def init(config):
    """初始化插件"""
    global _anima_service, _config
    
    _config = config or {}
    
    try:
        from anima.anima_service import AnimaService
        _anima_service = AnimaService(_config)
        print("[PLUGIN] /anima 初始化成功")
    except Exception as e:
        print(f"[PLUGIN] /anima 初始化失败: {e}")


def handle(content, context):
    """
    处理 /anima 命令
    
    用法: /anima <提示词>
    示例: /anima 一个穿着蓝色连衣裙的女孩在樱花树下
    """
    global _anima_service
    
    if not _anima_service:
        return "❌ Anima 服务未初始化"
    
    # 提取提示词
    user_prompt = content.strip()
    if not user_prompt:
        return "❌ 请提供提示词\n用法: /anima <提示词>"
    
    # 获取用户信息
    user_wxid = context.get("wxid", "unknown")
    nickname = context.get("user", "未知用户")
    group_name = context.get("group", "")
    
    # 检查是否可以开始生成
    can_generate, msg = _anima_service.can_start_generate(user_wxid)
    if not can_generate:
        return msg

    # 标记开始生成
    if not _anima_service.mark_generating(user_wxid):
        return "❌ 已有生成任务正在进行，请稍候"

    # 原子预扣积分：原实现是"先查余额 → 生成几十秒 → 事后扣分"，
    # 竞态窗口横跨整个生成周期，并发请求可双双通过检查透支成负分
    try:
        deducted, balance = try_deduct_points(user_wxid, POINTS_COST)
    except Exception as e:
        _anima_service.mark_done()
        print(f"[ANIMA] 积分扣减失败: {e}")
        return "❌ 积分扣减失败，请重试"
    if not deducted:
        _anima_service.mark_done()
        return f"❌ 积分不足！生成一张图像需要消耗 {POINTS_COST} 积分，你当前有 {balance:.1f} 积分。"

    try:
        # 通知用户开始处理
        send(group_name, f"🎨 {nickname} 发起图像生成，处理中...")

        # 执行生成流程
        result = _anima_service.generate(user_prompt)

        # 构建响应消息
        if result.get("success"):
            image_path = result.get("image_path")
            elapsed = result.get("elapsed_seconds", 0)

            # 发送图像文件
            try:
                send(group_name, file_path=image_path, mode="wechat_file")
                # 发送成功后删除图像
                _anima_service.delete_image(image_path)
            except Exception as e:
                print(f"[ANIMA] 发送图像失败: {e}")
                send(group_name, f"⚠️ 图像保存路径: {image_path}")
                _anima_service.delete_image(image_path)

            remaining_points = get_points(user_wxid)

            return f"✅ 生成完成，用时 {elapsed} 秒\n💎 消耗积分: -{POINTS_COST} (剩余: {remaining_points:.1f})"
        else:
            error = result.get("error", "未知错误")

            # 检查是否被拒绝
            if result.get("rejected"):
                error_msg = f"⛔ 生成请求被拒绝：{error}"
            else:
                error_msg = f"❌ 生成失败：{error}"

            # 积分已在生成前预扣（失败不退还，与原语义一致）
            remaining_points = get_points(user_wxid)
            error_msg += f"\n💎 消耗积分: -{POINTS_COST} (剩余: {remaining_points:.1f})"

            return error_msg

    except Exception as e:
        error_msg = f"❌ 处理异常: {e}"

        # 积分已在生成前预扣（异常不退还，与原语义一致）
        remaining_points = get_points(user_wxid)
        error_msg += f"\n💎 消耗积分: -{POINTS_COST} (剩余: {remaining_points:.1f})"

        return error_msg

    finally:
        # 标记生成完成
        _anima_service.mark_done()
