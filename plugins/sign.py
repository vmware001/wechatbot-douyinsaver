import random
from datetime import date

from utils.points_manager import (
    try_sign_today,
    add_points,
)

COMMAND = "/sign"


def handle(content, context):
    """处理签到命令"""
    wxid = context.get("wxid") or context.get("raw", {}).get("wxid")

    if not wxid:
        return "无法获取用户信息"

    wxid = str(wxid).strip()

    # 原子签到标记：并发重复签到只有第一个请求能成功（消除读-写 TOCTOU）
    if not try_sign_today(wxid):
        return "你今日已签到过了，请明天再来"

    # 随机获取5-10积分
    points_gained = random.randint(5, 10)

    # 增加积分
    try:
        total_points = add_points(wxid, points_gained)
    except Exception as e:
        print(f"[SIGN ERROR] 签到加积分失败: {e}")
        return "❌ 签到积分发放失败，请重试"

    return (
        f"✅ 签到成功！\n"
        f"获得积分: +{points_gained}\n"
        f"当前积分: {total_points:.1f}\n"
        f"-\n"
        f"💡 小贴士：你也可以通过 /player fortune 抽取今日运势获得5-10积分哦"
    )
