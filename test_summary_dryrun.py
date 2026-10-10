# -*- coding: utf-8 -*-
"""Dry-run of daily summary: generate and print as JSON, never send to group."""
import json
import time

from llm.memory.memory_manager import MemoryManager
from core import daily_summary

mm = MemoryManager()
group = "目标群"

log_text = daily_summary._collect_log(mm, group)
t0 = time.time()
summary = daily_summary._generate_summary({}, mm, group)
elapsed = time.time() - t0

result = {
    "dry_run": True,
    "group": group,
    "log_chars": len(log_text),
    "log_lines": log_text.count("\n") + 1 if log_text else 0,
    "elapsed_seconds": round(elapsed, 1),
    "summary_chars": len(summary) if summary else 0,
    "within_limit": bool(summary) and len(summary) <= daily_summary.MAX_CHARS,
    "summary": summary,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
