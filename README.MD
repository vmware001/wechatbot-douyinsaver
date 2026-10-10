# GSMPS WeChat Bot

一个运行在 Windows 上、基于 WeFlow 的微信机器人框架。它将群消息接收、命令插件、自动事件、LLM 对话和统一消息发送组合在一起，可用于构建具备上下文、记忆与群聊互动能力的微信机器人。

## 核心能力

- **微信消息接入与发送**：通过 WeFlow 接收群聊和私聊消息，统一发送文字、图片、文件与语音。
- **命令插件**：`plugins/` 中的插件提供群管理、查询、游戏服务、图片与实用工具等命令能力。
- **自动插件**：`auto/` 中的事件插件可处理媒体 follow-up、消息监听和后台任务。
- **多协议 LLM API 池**：支持 OpenAI Chat Completions、OpenAI Responses 与 Anthropic Messages；可按优先级配置多路模型并在单次请求内自动回退。
- **群聊对话与主动参与**：支持前缀唤起、@ 唤起和可选的群聊上下文参与；本地门控用于控制不必要的模型请求与发言频率。
- **记忆与群聊学习**：按群维护短期上下文、长期记忆、人物信息、黑话和句式表达库，并在群聊周期结束时整理新学习材料。
- **语音与点歌**：音频通过 Windows 虚拟麦克风录入微信语音；`/song`、`/music` 由网易云搜索歌曲，再通过 Node.js 长驻守护进程执行 `music-source/` 下的洛雪自定义源脚本解析 URL（init 一次、多次复用，支持 `updateAlert` 自动更新脚本），失败时回退网易云。
- **可选服务集成**：仓库包含 Minecraft/RCON、ComfyUI、网页工具等可选能力，按配置启用即可。

## 项目结构

| 目录/文件 | 说明 |
|---|---|
| `core/` | 消息路由、队列、调度、发送与 WeFlow 接入 |
| `plugins/` | 显式命令插件 |
| `auto/` | 自动事件、媒体 follow-up 与后台监听插件 |
| `llm/` | LLM API 适配、提示词、上下文、记忆、学习与主动回复 |
| `services/` | 外部业务服务封装，例如音乐、MC、RCON、ComfyUI |
| `utils/` | 通用工具、SQLite 与数据处理模块 |
| `weflow-core/` | WeFlow 相关 Node.js 工程 |
| `assets/` | 图片、字体等静态资源 |
| `sample_config.json` | 可复制的配置模板 |

## 快速开始

### 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Node.js LTS
- 已安装并登录的微信客户端，以及可用的 WeFlow 环境

### 安装与启动

复制配置模板并填写运行所需参数：

```powershell
Copy-Item sample_config.json config.json
```

安装 Python 与 WeFlow 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Push-Location weflow-core
npm ci
npm run build
Pop-Location
```

完成 `config.json` 后启动（前后端分离：先前端两进程，再后端大脑）：

```powershell
.\start_all.bat        # 一键：前端 weflow-core(:5031) + sender-service(:9999) + 后端
```

也可分开启动（调试时很有用）：

```powershell
.\start_frontend.bat   # 只起前端（解密DB/SSE推送/取图 + UIA发送）
.\start_backend.bat    # 只起后端（大脑，自动等前端健康就绪）
.\stop_all.bat         # 全部停止
```

进程间只走 JSON/HTTP，协议见 [docs/PROTOCOL.md](./docs/PROTOCOL.md)。旧版单体启动方式 `.\start.bat`（含首次构建 weflow-core）仍保留。

首次使用至少需要填写目标群、机器人前缀、WeFlow 账号数据和 `llm.apis` 中的模型配置。完整字段说明见 [config_guide.md](./config_guide.md)。

## 基本使用

- **前缀与 @**：在 `prefix_mode: "strict"` 下，以配置的前缀或 @ 机器人触发 LLM；`"mixed"` 还允许机器人在合适的群聊上下文中自主参与。
- **普通命令**：使用 `/<命令> <参数>` 调用 `plugins/` 中的功能。可用命令以实际启用的插件为准。
- **图片 follow-up**：例如先发送 `/对称 左右`，再发送一张图片，机器人会处理后续图片。
- **点歌语音**：`/song <歌曲名>` 或 `/music <歌曲名>` 发送歌曲前 30 秒；可换行指定片段：

  ```text
  /song 歌曲名
  12.5 30
  ```

  只填写起始秒时，终止位置默认为起始秒后 30 秒；单段语音最长不超过 30 秒。

  音源脚本放在 `music-source/`；顺序由 `source_order.txt` 或文件名排序决定（见 [config_guide.md](./config_guide.md)），无需在代码里写死。

## LLM、上下文与学习

LLM 请求会根据 API 优先级选择可用端点，并支持同一请求内依次尝试后备端点。机器人将稳定提示词、群记忆和历史上下文按适合缓存的顺序组织；不同协议的缓存参数由 API 配置控制。

群聊学习围绕完整消息单元进行：短期上下文、记忆、黑话、句式和风格数据按群保存，未完成的学习材料会留待后续周期处理。表达库可以为回复提供群聊中常用的自然句式，但不会替代任务要求或正常回答。

有关 API 协议、缓存、提示词结构、主动参与门控和学习链路的开发说明见 [DEV_GUIDE.md](./DEV_GUIDE.md)。

## 文档索引

- [环境准备与构建](./SETUP.md)：还差哪些依赖、各自的项目地址、从零构建与验证清单。
- [配置参考](./config_guide.md)：`config.json` 的字段、取值和示例。
- [开发指南](./DEV_GUIDE.md)：架构、插件开发、自动插件、LLM 调用链、发送接口与调试约定。
- [管理命令](./admin_commands.txt)：已提供的管理类命令说明。
- [配置模板](./sample_config.json)：可直接复制并填写的最小配置示例。

## 上游项目与致谢

本项目基于 [oldicecube/GSMPSWechatBot](https://github.com/oldicecube/GSMPSWechatBot)（作者 **oldicecube**）开发，
另感谢 WeFlow、WechatRobot、ComfyUI、mcstatus、DeepSeek 等开源项目。

本版本是原项目的**脱敏副本**：删除了真实配置（API Key、微信解密密钥、wxid、隧道凭据）、群聊天记录、
运行日志、浏览器登录态与抓包产物，并把源码里的私有群名、机器人触发词、私有域名和个人绝对路径
替换为占位符或环境变量。第三方组件的许可与来源见 [THIRD_PARTY.md](./THIRD_PARTY.md)。

## 许可证

- 本仓库第一方源码：**GNU GPL-3.0-or-later**，见 [LICENSE](./LICENSE)
- 原上游项目：**MIT**，见 [LICENSE-MIT](./LICENSE-MIT)（版权归 oldicecube，声明依 MIT 要求保留）

MIT 与 GPL-3.0 兼容；随仓库分发的第三方二进制与素材各自遵循其原许可，详见 [THIRD_PARTY.md](./THIRD_PARTY.md)。

## 使用须知

- 微信侧能力依赖读取本机微信客户端的加密数据库，并用 UI 自动化驱动微信窗口发送消息；
  请只在**自己拥有或已获授权**的账号与设备上使用，并遵守微信、抖音的服务条款。仓库不附带任何凭据。
- 脚本默认以 `C:\GSMPSWechatBot` 作为安装路径，克隆到其他位置时请相应调整 `*.bat` / `*.ps1`。
- 抖音搬运的视频库根目录由环境变量 `DOUYIN_LIB_DIR` 指定（默认 `D:\douyin_lib`）；
  公网标注页主机名由 `DOUYIN_PUBLIC_HOST` 指定（默认 `label.example.com`）。
- `tools/` 是抖音采集/搬运/标注流水线（`start_douyin_*.bat` 拉起），配置模板见 `config/*.example.json`。

## 已知问题

### `wcdb_api.dll` 构建过期（错误码 -101 / -1000）

`weflow-core/resources/wcdb/win32/x64/wcdb_api.dll` 是你从 WeFlow 安装包复制进来的（这份二进制
**不随本仓库分发**，见 [SETUP.md](./SETUP.md) 第 3 步）。它是**预编译**原生库，
里面有两个硬编码的构建过期开关，都指向 **2026-09-30 23:59:59**（`InitProtection()` 超时返回
`-101`，`wcdb_init()` 超时返回 `-1000`）。过期后 `initialize()` 直接失败 —— 日志里是
`[bootstrap] InitProtection rc=-101`、`InitProtection failed finalCode=-101`，数据库永远打不开。
与微信版本、解密密钥、资源路径都无关，`npm run build` 也修不了，因为问题在**分发出来的二进制**里。

两条修复路线，任选其一，都要先完全停掉 weflow-core：

1. **就地改 6 字节（推荐）**：`scripts/patch_wcdb_api_expiry.py` 按字节签名定位这两处判断，
   把"条件跳到成功出口"改成"无条件跳到同一出口"，只改 6 个字节，文件大小、导出表和其他代码不变：

   ```powershell
   .\stop_all.bat
   .\.venv\Scripts\python.exe scripts\patch_wcdb_api_expiry.py --check   # 只检查，不写文件
   .\.venv\Scripts\python.exe scripts\patch_wcdb_api_expiry.py           # 就地修补，自动备份
   ```

   备份是 DLL 同目录的 `wcdb_api.dll.bak-expiry`，回滚即拷回。原理与逐字节分析来自社区项目
   [AnonymousUser443/WeFlow-Community-Patch](https://github.com/AnonymousUser443/WeFlow-Community-Patch)
   的 `docs/NATIVE-EXPIRY-GATE.md`；本脚本是同一结论的独立实现，不下载任何二进制。

2. **换用社区预编译版**：`scripts/install_weflow_patch.py` 从上述仓库下载已修补好的
   `wcdb_api.dll`（SHA-256 固定校验）并替换。⚠️ 它对应的是 **2026-07-07 构建**；如果你从
   WeFlow 复制进来的是另一个构建（例如 2026-05-07），两者的**导出表并不相同** —— 那个新构建
   没有 `wcdb_open_message_cursor_lite`（`weflow-core/src/services/wcdbCore.ts:984` 有绑定，
   缺失时会静默降级），另外多出 6 个符号。只想解决过期问题就别换构建，用路线 1。

> 重装、修复安装或升级 WeFlow 都会把该 DLL 覆盖回过期版本，届时重跑一次即可。
> 同目录的 `welive.exe`（本仓库未分发）另有**独立**的过期开关（FILETIME `2026-07-31 16:00 UTC`），
> 且带自校验，不要修改它的 `.text`。
