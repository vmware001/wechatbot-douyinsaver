# 环境准备与构建指南

本仓库是脱敏后的**源码快照**：不含任何运行时二进制（微信/抖音客户端不算），也不含密钥、聊天记录与日志。
本文说明三件事：**还差哪些东西、这些东西的项目地址在哪、怎样从零把它构建并跑起来**。
（配置字段的完整说明见 [config_guide.md](./config_guide.md)，架构与开发约定见 [DEV_GUIDE.md](./DEV_GUIDE.md)。）

Windows 10/11 全流程；命令默认在仓库根目录执行，脚本默认装到 `C:\GSMPSWechatBot`。

---

## 一、还差什么

### 1. 必装

| 缺的东西 | 项目地址 / 获取方式 | 用途 |
|---|---|---|
| **Python 3.10+**（本项目按 3.11 开发） | <https://www.python.org/downloads/> | 后端大脑、插件、自动任务 |
| **Python 依赖** | `pip install -r requirements.txt` | Pillow / requests / openai / pywin32 / sounddevice / pycaw 等 |
| **Node.js 18 LTS+** | <https://nodejs.org/> | `weflow-core`（解密数据库、SSE 推消息、取媒体）与洛雪音源守护进程 |
| **weflow-core 的 npm 依赖** | `cd weflow-core && npm ci`（`exceljs` / `koffi` / `fzstd` / `ffmpeg-static`） | `koffi` 是加载 `wcdb_api.dll` 的 FFI 层 |
| **WeFlow 桌面客户端** | <https://github.com/hicccc77/WeFlow>（Release 里下成品安装） | **它随包提供 `resources/`**（`wcdb_api.dll`、`WCDB.dll`、`SDL2.dll`、MSVC 运行库、密钥/解密辅助模块）。这些二进制本仓库不随包分发，必须从你自己的 WeFlow 安装目录复制 |
| **微信 PC 客户端 4.0+** | 官方渠道 | 数据源（`db_storage`）与被自动化的界面 |
| **微信数据库解密密钥**（64 位 hex） | WeFlow 自带的密钥提取流程 | 填进 `config.json → weflow.decryptKey` |

> `ffmpeg` 不需要单独安装：抖音合集拼接用的 ffmpeg 来自 Python 包 `imageio-ffmpeg`（已写进 requirements.txt）。

### 2. 按功能选装

| 功能 | 需要的东西 | 项目地址 |
|---|---|---|
| 语音发送（`/voice`、点歌语音） | VB-CABLE 虚拟声卡 | <https://vb-audio.com/Cable/> |
| 抖音采集/搬运（`tools/`、`start_douyin_*`） | 抖音 PC 客户端 | <https://www.douyin.com/download/pc> |
| 抖音抓包（MITM） | mitmproxy（提供 `mitmdump`，`start_mitm.bat` 调的是 `.venv\Scripts\mitmdump.exe`） | <https://github.com/mitmproxy/mitmproxy> ／ `pip install mitmproxy` |
| 公网标注页（`tools/cloudflared_run.py`） | cloudflared | <https://github.com/cloudflare/cloudflared> |
| 采集驱动 | Chrome（或 Edge） | <https://www.google.com/chrome/>（脚本用调试端口 `9223` + 独立 profile） |
| 点歌 | 洛雪（LX Music）自定义音源脚本，放进 `music-source/` | 自行获取，本仓库不附带第三方音源 |
| `/anima` 图像生成 | ComfyUI | <https://github.com/comfyanonymous/ComfyUI> |
| `/image` 云端画图 | 智谱 CogView 等 OpenAI 兼容图片 API | <https://open.bigmodel.cn/> |
| MC 查询与锦标赛 | Minecraft 服务器 + RCON | <https://www.minecraft.net/> |
| 表情整活（`animation` 字段） | `data/emoji/` 表情素材（自备，见 `llm.emoji_dir`） | — |
| TTS | 小米 MiMo TTS 或其它兼容 API（`services/tts.py`） | — |
| 大模型对话 | 任意 OpenAI Chat Completions / Responses / Anthropic Messages 兼容服务 + API Key | 自填 `config.json → llm.apis` |

### 3. 本仓库特意没有的内容（要自己补）

| 位置 | 说明 |
|---|---|
| `weflow-core/resources/**` | 来自 WeFlow 安装包的二进制（含 `wcdb_api.dll`）。**不随仓库分发**，按下面第 3 步复制 |
| `assets/fonts/Minecraft AE.ttf` | Mojang/Microsoft 的专有字体，已移除；代码不按文件名引用字体，不影响运行 |
| `data/emoji/`、`data/groups/`、`data/*.sqlite3` | 表情素材、群历史、记忆与学习库等运行时数据 |
| `music-source/*.js` | 第三方音源脚本（无授权声明，未分发） |
| `config.json`、`config/*.json` | 真实密钥与群名，仓库只提供 `sample_config.json` 与 `config/*.example.json` |
| `backup/`、`logs/`、`output/` | 备份、日志与生成物 |

---

## 二、从零构建

### 0. 前置

- Git、Python 3.10+、Node.js 18+
- 已安装并**登录**微信 4.x（本项目读它本地的加密数据库，并用 UI 自动化驱动它的窗口）

### 1. 拉代码 + Python 环境

```powershell
git clone <本仓库地址> C:\GSMPSWechatBot
cd C:\GSMPSWechatBot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. weflow-core（前端：解密数据库 + SSE 推送 + 取媒体）

```powershell
cd weflow-core
npm ci
npm run build                # 也可以用 node esbuild.config.mjs
# 该目录需要一个叫 WeFlow.exe 的 Node 运行时：上游按进程名绕过 DLL 的进程校验
copy "$env:ProgramFiles\nodejs\node.exe" .\WeFlow.exe
cd ..
```

> 首次运行 `start.bat` 也会自动帮你做这几步（`npm install` → 构建 → 用 `where node` 复制出 `WeFlow.exe`），
> 但正式部署建议按上面命令手动执行一遍，便于看到报错。

### 3. 把 WeFlow 的 `resources` 复制进来

```powershell
# 装 WeFlow 时的目录结构是两层 resources，本项目的 resourcesPath 指向 weflow-core\resources
robocopy "$env:ProgramFiles\WeFlow\resources\resources" .\weflow-core\resources /E
```

不想复制也可以直接指过去：`config.json → weflow.resourcesPath` 写成
`C:\Program Files\WeFlow\resources\resources`，或设环境变量 `WCDB_RESOURCES_PATH` / `WCDB_DLL_PATH`。

> 复制完应能看到 `weflow-core\resources\wcdb\win32\x64\wcdb_api.dll`。
> 缺文件的报错是 `WCDB数据服务不存在: <路径>` 或错误码 `-2301`。

### 4. 给 `wcdb_api.dll` 打过期补丁（必做）

上游那份 `wcdb_api.dll` 内置两个硬编码的构建过期开关（都指向 **2026-09-30 23:59:59**），
过期后 `InitProtection()` 返回 `-101`、数据库永远打不开。先检查再打补丁：

```powershell
.\.venv\Scripts\python.exe scripts\patch_wcdb_api_expiry.py --check   # 只检查，不写文件
.\.venv\Scripts\python.exe scripts\patch_wcdb_api_expiry.py           # 就地改 6 字节，自动备份
```

原理、两条修复路线的取舍（就地修补 vs 社区预编译版及其导出表差异）见
[README「已知问题」](./README.MD#已知问题) 与
[AnonymousUser443/WeFlow-Community-Patch](https://github.com/AnonymousUser443/WeFlow-Community-Patch)。
注意：重装/升级 WeFlow 会把它覆盖回过期版本，届时重跑一次即可。

### 5. 配置

```powershell
Copy-Item sample_config.json config.json
```

至少要填：`target_group`（目标群名）、`prefix`（唤起词）、`weflow.dbPath`（微信数据目录）、
`weflow.decryptKey`（64 位 hex 密钥）、`weflow.myWxid`、`llm.apis[*]`（模型服务与 Key）。
字段含义见 [config_guide.md](./config_guide.md)；抖音链路另需：

```powershell
Copy-Item config\cloudflare.example.json       config\cloudflare.json
Copy-Item config\douyin_forwarder.example.json config\douyin_forwarder.json
Copy-Item config\douyin_saver.example.json     config\douyin_saver.json
```

### 6. 启动

```powershell
.\start_all.bat          # 一键：前端 weflow-core(:5031) + sender-service(:9999) + 后端
.\start_frontend.bat     # 只起前端（调试用）
.\start_backend.bat      # 只起后端（会自动等前端健康就绪）
.\stop_all.bat           # 全部停止
```

- 生产环境建议用 `start_bot_vm.bat`：崩了自动重启，并把各服务放进一个多标签窗口；
  它还会**自提权**（微信以管理员运行，UIPI 会挡住普通权限的窗口控制）。
- 抖音链路另起：`start_douyin_autostart.bat`（mitm 抓包 + 采集 + 转发 + 控制台 + 隧道）。
- 前后端只通过本地 HTTP 通信，协议见 [docs/PROTOCOL.md](./docs/PROTOCOL.md)。

### 7. 语音（可选）

见 [DEV_GUIDE.md](./DEV_GUIDE.md)「语音发送」：

1. 管理员运行 `assets/vbcable` 的安装包装 VB-CABLE（本仓库未分发安装包，去 <https://vb-audio.com/Cable/>）；
2. 重启 sender-service（`restart_sender.bat`）；
3. `python scripts\set_default_mic.py "CABLE Output"`；
4. 验证：`curl http://127.0.0.1:9999/voice/status` 应返回 `"available": true`。

---

## 三、验证清单

| 检查 | 期望 |
|---|---|
| `curl http://127.0.0.1:5031/health` | `{"status":"ok","service":"weflow-core",...}` |
| `curl http://127.0.0.1:9999/health` | `{"status":"ok","service":"sender",...}` |
| `%USERPROFILE%\.weflow-core\logs\wcdb.log` | 出现 `open sessionDb=...` 与 `open ok handle=1`，且**没有** `InitProtection rc=-101` |
| 群里发 `<prefix> /help`、`<prefix> /ping` | 有回复；`/ping` 返回处理延迟 |
| 音源可用 | `<prefix> /song 歌名` 能回语音（需本机 Node + 至少一个音源脚本） |

## 四、常见坑

- **`-101` / `-1000`**：`wcdb_api.dll` 构建过期 → 回到第 4 步打补丁。
- **`-2301` / `WCDB数据服务不存在`**：`weflow-core\resources` 没复制全。
- **`-2302`（DLL 版本不匹配）**：`resources` 与 `weflow-core` 不是同一套，重新复制整个 `resources`。
- **`-3001` / `-3002`**：`weflow.dbPath` 指错，应指向包含 `wxid_*` 子目录的微信数据目录。
- **发不出去消息**：微信以管理员运行而 bot 没有 → 用 `start_bot_vm.bat`（自提权）。
- **收不到消息**：`:5031` 没起来，或 `weflow.messagePushEnabled` 为 false。
- **语音没声音**：默认录音设备不是 `CABLE Output`，或 sender-service 没重启。
