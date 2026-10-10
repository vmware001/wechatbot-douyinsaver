"""把 Windows 默认录音设备设为指定名称（部分匹配，如 "CABLE Output"）。

用法: python scripts/set_default_mic.py "CABLE Output"
通过 Core Audio IPolicyConfig::SetDefaultEndpoint 实现，
Console / Multimedia / Communications 三个角色全部设置。不需要管理员。
"""
import sys

import comtypes
from comtypes import CLSCTX_ALL

from pycaw.constants import CLSID_CPolicyConfigClient
from pycaw.api.policyconfig import IPolicyConfig
from pycaw.utils import AudioUtilities

E_ROLE_CONSOLE = 0
E_ROLE_MULTIMEDIA = 1
E_ROLE_COMMUNICATIONS = 2


def main():
    keyword = (sys.argv[1] if len(sys.argv) > 1 else "CABLE Output").lower()

    target_id = None
    target_name = None
    for d in AudioUtilities.GetAllDevices():
        try:
            name = d.FriendlyName
        except Exception:
            continue
        # 设备 id 形如 {0.0.1.xxxx}.{guid}，第三段 1 = 录音(capture) 端点
        is_capture = d.id.startswith("{0.0.1.")
        print(f"  {'capture' if is_capture else 'render '} endpoint: {name}")
        if is_capture and keyword in str(name).lower():
            target_id = d.id
            target_name = name

    if not target_id:
        print(f"[FAIL] no active capture device matching: {keyword}")
        sys.exit(1)

    policy = comtypes.CoCreateInstance(
        CLSID_CPolicyConfigClient, IPolicyConfig, CLSCTX_ALL
    )
    for role in (E_ROLE_CONSOLE, E_ROLE_MULTIMEDIA, E_ROLE_COMMUNICATIONS):
        hr = policy.SetDefaultEndpoint(target_id, role)
        if hr != 0:
            print(f"[FAIL] SetDefaultEndpoint role={role} hr=0x{hr & 0xFFFFFFFF:08x}")
            sys.exit(1)

    print(f"[OK] default recording device -> {target_name}")


if __name__ == "__main__":
    main()
