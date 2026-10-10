# -*- coding: utf-8 -*-
"""开机自启辅助:等待微信登录成功(主窗口出现)后,自动拉起 start_bot.bat。
登录窗只有296x388,主窗口是1342x971,用窗口尺寸区分是否已登录。"""
import os
import subprocess
import time

import win32gui

BASE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 30 * 60          # 最多等30分钟,超时放弃(下次开机再试)
MAIN_W, MAIN_H = 800, 600  # 主窗口最小尺寸


def main_window():
    h = win32gui.FindWindow('Qt51514QWindowIcon', '微信')
    if not h:
        return None
    if win32gui.IsIconic(h):
        return h  # 最小化的主窗口也算已登录,尺寸判断交给bot自己恢复
    l, t, r, b = win32gui.GetWindowRect(h)
    return h if (r - l) > MAIN_W and (b - t) > MAIN_H else None


def main():
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        if main_window():
            break
        time.sleep(10)
    else:
        return
    time.sleep(15)  # 等微信界面完全就绪再拉起bot
    subprocess.Popen(['cmd', '/c', 'start_bot_vm.bat'], cwd=BASE,
                     creationflags=subprocess.CREATE_NEW_CONSOLE)


if __name__ == '__main__':
    main()
