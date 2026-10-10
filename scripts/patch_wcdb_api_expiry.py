# -*- coding: utf-8 -*-
"""就地修补 wcdb_api.dll 的内置构建过期开关（只改 6 个字节）。

背景：WeFlow 5.0.0 随包的预编译 wcdb_api.dll 里有**两个硬编码的构建过期开关**，
都指向 2026-09-30 23:59:59。过期后 `InitProtection()` 返回 -101、`wcdb_init()`
返回 -1000，`initialize()` 直接失败，数据库永远打不开（界面上就是"错误码 -101"）。

修补方式与社区结论一致：把两处"条件跳到成功出口"改成"无条件跳到同一出口"，
执行路径与过期前逐字节一致，不跳过初始化、不引入新行为：

  gate 1  cmp rax, 0x6ABDA27F / 7E jle  ->  EB jmp      （InitProtection 的 -101）
  gate 2  cmp rax, rbx / 0F 8E jle rel32 ->  E9 jmp rel32 + 90 nop（wcdb_init 的 -1000）

两处都按**字节签名**定位（不依赖固定文件偏移），并要求签名在全文件中唯一命中；
gate 2 还会校验跳转目标确实是 `33 C0`（xor eax, eax）这个成功出口，否则拒绝写入。

对上游预编译二进制的最小改动：本脚本改完只比原文件少 6 个字节的内容
（gate1 1 字节 + gate2 5 字节），文件大小、导出表、其余代码全部不变。
不下载任何第三方二进制。

用法：
    python scripts/patch_wcdb_api_expiry.py --check                 # 只检查，不写入
    python scripts/patch_wcdb_api_expiry.py --dry-run --out new.dll # 写到副本，供对比
    python scripts/patch_wcdb_api_expiry.py                         # 就地修补（自动备份）
"""
from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DLL = BASE_DIR / "weflow-core" / "resources" / "wcdb" / "win32" / "x64" / "wcdb_api.dll"

# gate 1：cmp rax, 0x6ABDA27F(2026-09-30 23:59:59 UTC) / jle +10 / mov eax, -101
GATE1_OLD = bytes([0x48, 0x3D, 0x7F, 0xA2, 0xBD, 0x6A, 0x7E, 0x0A, 0xB8, 0x9B, 0xFF, 0xFF, 0xFF])
GATE1_NEW = bytes([0x48, 0x3D, 0x7F, 0xA2, 0xBD, 0x6A, 0xEB, 0x0A, 0xB8, 0x9B, 0xFF, 0xFF, 0xFF])

# gate 2 锚点：struct tm 初始化（2026-09-30 23:59:59 本地时间）
GATE2_TM = bytes([
    0xC7, 0x44, 0x24, 0x6C, 0x7E, 0x00, 0x00, 0x00,
    0xC7, 0x44, 0x24, 0x68, 0x08, 0x00, 0x00, 0x00,
    0xC7, 0x44, 0x24, 0x64, 0x1E, 0x00, 0x00, 0x00,
    0xC7, 0x44, 0x24, 0x60, 0x17, 0x00, 0x00, 0x00,
    0xC7, 0x44, 0x24, 0x5C, 0x3B, 0x00, 0x00, 0x00,
    0xC7, 0x44, 0x24, 0x58, 0x3B, 0x00, 0x00, 0x00,
])
GATE2_OLD = bytes([0x48, 0x3B, 0xC3, 0x0F, 0x8E])   # cmp rax, rbx / jle rel32
GATE2_NEW = bytes([0x48, 0x3B, 0xC3, 0xE9])         # cmp rax, rbx / jmp rel32
SUCCESS_EXIT = bytes([0x33, 0xC0])                  # xor eax, eax


def find_all(haystack: bytes, needle: bytes) -> list[int]:
    hits = []
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return hits
        hits.append(i)
        start = i + 1


def pe_sections(b: bytes):
    lfanew = struct.unpack_from("<I", b, 0x3C)[0]
    nsec = struct.unpack_from("<H", b, lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", b, lfanew + 20)[0]
    sec_off = lfanew + 24 + opt_size
    sections = []
    for i in range(nsec):
        o = sec_off + i * 40
        sections.append({
            "name": b[o:o + 8].rstrip(b"\0").decode("latin1"),
            "va": struct.unpack_from("<I", b, o + 12)[0],
            "raw_size": struct.unpack_from("<I", b, o + 16)[0],
            "raw_ptr": struct.unpack_from("<I", b, o + 20)[0],
        })
    return sections


def offset_to_rva(b: bytes, off: int) -> int:
    for s in pe_sections(b):
        if s["raw_ptr"] <= off < s["raw_ptr"] + s["raw_size"]:
            return s["va"] + (off - s["raw_ptr"])
    raise ValueError(f"offset 0x{off:X} is not inside any section")


def rva_to_offset(b: bytes, rva: int) -> int:
    for s in pe_sections(b):
        if s["va"] <= rva < s["va"] + s["raw_size"]:
            return s["raw_ptr"] + (rva - s["va"])
    raise ValueError(f"rva 0x{rva:X} is not inside any section")


def patch_bytes(data: bytes) -> tuple[bytes, list[str], list[str]]:
    """Return (patched, applied_notes, already_notes). Raises on anything unexpected."""
    out = bytearray(data)
    applied, already = [], []

    a1, b1 = find_all(data, GATE1_OLD), find_all(data, GATE1_NEW)
    if len(a1) == 1 and not b1:
        out[a1[0] + 6] = 0xEB
        applied.append(f"gate1 InitProtection(-101) @ 文件偏移 0x{a1[0]:X}: 7E jle -> EB jmp")
    elif len(b1) == 1 and not a1:
        already.append(f"gate1 @ 0x{b1[0]:X} 已是修补状态")
    else:
        raise ValueError(f"gate1 签名命中数异常（原版={len(a1)}，已修补={len(b1)}），拒绝写入")

    tm = find_all(data, GATE2_TM)
    if len(tm) != 1:
        raise ValueError(f"gate2 tm 初始化锚点应恰好命中 1 次，实际 {len(tm)} 次，拒绝写入")

    a2, b2 = find_all(data, GATE2_OLD), find_all(data, GATE2_NEW)
    if len(a2) == 1 and not b2:
        branch = a2[0] + 3
        rel = struct.unpack_from("<i", data, branch + 2)[0]
        branch_rva = offset_to_rva(data, branch)
        target_rva = branch_rva + 6 + rel
        target_off = rva_to_offset(data, target_rva)
        if data[target_off:target_off + 2] != SUCCESS_EXIT:
            raise ValueError(
                f"gate2 跳转目标 RVA 0x{target_rva:X} 不是预期的 xor eax,eax 成功出口，拒绝写入")
        new_rel = target_rva - (branch_rva + 5)          # E9 rel32 是 5 字节，后面跟 1 字节 NOP
        out[branch:branch + 5] = b"\xE9" + struct.pack("<i", new_rel)
        out[branch + 5] = 0x90
        applied.append(
            f"gate2 wcdb_init(-1000) @ 文件偏移 0x{branch:X} (RVA 0x{branch_rva:X}): "
            f"0F 8E jle -> E9 jmp + NOP (-> RVA 0x{target_rva:X})")
    elif len(b2) == 1 and not a2:
        already.append(f"gate2 @ 0x{b2[0]:X} 已是修补状态")
    else:
        raise ValueError(f"gate2 签名命中数异常（原版={len(a2)}，已修补={len(b2)}），拒绝写入")

    return bytes(out), applied, already


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    ap.add_argument("--check", action="store_true", help="只检查是否已修补，不写文件")
    ap.add_argument("--dry-run", action="store_true", help="结果写到 --out，不改原文件")
    ap.add_argument("--out", type=Path, help="--dry-run 的输出路径")
    args = ap.parse_args()

    target = args.dll.resolve()
    if not target.is_file():
        print(f"ERROR: 找不到 {target}", file=sys.stderr)
        return 1
    data = target.read_bytes()
    try:
        patched, applied, already = patch_bytes(data)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not applied:
        print(f"{target}\n结果：已是修补状态，无需改动。")
        for n in already:
            print(f"  {n}")
        return 0
    if args.check:
        print(f"{target}\n结果：**未修补**（两份构建过期开关仍在）。")
        for a in applied:
            print(f"  待修补 {a}")
        return 1

    diffs = [i for i in range(len(data)) if data[i] != patched[i]]
    print(f"目标     : {target}")
    print(f"文件大小 : {len(data)} bytes（修补后 {len(patched)} bytes，应一致）")
    if len(patched) != len(data):
        print("ERROR: 大小发生变化，拒绝继续", file=sys.stderr)
        return 1
    print(f"差异字节 : {len(diffs)} 处 {[hex(i) for i in diffs]}")
    for a in applied:
        print(f"修补     : {a}")

    if args.dry_run:
        out = args.out or target.with_name(target.stem + ".patched.dll")
        out.write_bytes(patched)
        print(f"输出     : {out}（原文件未改动）")
        return 0

    backup = target.with_name(target.name + ".bak-expiry")
    if backup.exists():
        from datetime import datetime
        backup = target.with_name(target.name + ".bak-expiry-" +
                                  datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(target, backup)
    target.write_bytes(patched)
    print(f"备份     : {backup}")
    print(f"输出     : {target}")
    print("结果     : 已修补。请重启 weflow-core 后再看 wcdb.log。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
