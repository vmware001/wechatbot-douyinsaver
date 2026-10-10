# 声明 / NOTICE

## 项目来源

**本项目基于 [oldicecube/GSMPSWechatBot](https://github.com/oldicecube/GSMPSWechatBot) 整理发布。**

- 原项目地址：<https://github.com/oldicecube/GSMPSWechatBot>
- 原作者：**oldicecube**
- 原项目许可：**MIT License**（Copyright (c) 2026 oldicecube），完整文本保留在 [LICENSE-MIT](./LICENSE-MIT)

## 本仓库做了什么

这是基于上述原始项目的工作副本整理出的**二次发布副本**，主要动作有三类：

1. **脱敏**：移除真实配置（API Key、微信解密密钥、wxid、Cloudflare 隧道凭据）、群聊天记录、运行日志、
   浏览器登录态、抓包产物与 mitmproxy CA 私钥；把源码里的私有群名、机器人触发词、私有域名与个人绝对路径
   替换为占位符或环境变量。逐项清单见 [PRIVACY_MANIFEST.md](./PRIVACY_MANIFEST.md)。
2. **剔除不可分发的第三方内容**：WeFlow 安装包里的预编译二进制（`weflow-core/resources/**`，被社区指为
   CC BY-NC-SA 4.0）、专有字体、未授权的音源脚本等。见 [THIRD_PARTY.md](./THIRD_PARTY.md)。
3. **补齐文档与工具**：[SETUP.md](./SETUP.md)（依赖清单与构建步骤）、`scripts/patch_wcdb_api_expiry.py`
   等 `wcdb_api.dll` 构建过期（错误码 -101 / -1000）的修补工具、`requirements.txt` 补齐漏列依赖。

对原项目的**功能与架构没有做删改**：除上述脱敏、第三方内容移除与文档补充外，源码保持原状。

## 许可

- 本项目第一方源码：**GNU GPL-3.0-or-later**，见 [LICENSE](./LICENSE)
- 原项目代码：**MIT**（Copyright (c) 2026 oldicecube）—— MIT 要求保留版权声明，故完整文本保留在
  [LICENSE-MIT](./LICENSE-MIT)
- 随仓库分发的第三方内容及各自许可：见 [THIRD_PARTY.md](./THIRD_PARTY.md)

MIT 与 GPL-3.0 兼容（MIT 允许再许可为 GPL）。本仓库的 GPL-3.0 授权**不改变**任何第三方组件自身的许可。

## 致谢

感谢原作者 **oldicecube**，以及原项目所致谢的 WeFlow、WechatRobot、ComfyUI、mcstatus、DeepSeek 等开源项目。
