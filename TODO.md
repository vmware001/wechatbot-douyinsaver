# 功能改进 Flags（待办）

（暂无待办）

## 已归档

- [2026-09-09] ~~引用消息上下文优先~~（已实现：worker `_resolve_quoted_context` 解析被引用消息 svrid/sender/content → `memory_manager.get_group_messages_with_quoted_window` 取被引用消息±25条 ∪ 最新50条并集去重 → `llm_service._group_messages_for_reply` 接入 direct 回复路径 → prompt_builder 注入「引用焦点」提示要求优先围绕被引用内容回复）
- [2026-09-09] ~~前后端分离~~（已完成，见 docs/PROTOCOL.md）
- [2026-09-09] ~~/pro 带图 404~~（原修法：带图请求自动退回默认视觉模型。2026-09-27 换 mimo-v2.6-pro 后实测该模型支持图片输入，已移除降级，见 `llm/core/llm_service.py` handle_message 里 pro 覆盖段）
