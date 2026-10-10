# 第三方组件与许可说明

本仓库的第一方源码以 **GPL-3.0-or-later** 发布（见 [LICENSE](./LICENSE)）。

上游项目 [oldicecube/GSMPSWechatBot](https://github.com/oldicecube/GSMPSWechatBot) 以 **MIT** 许可发布，
其原始版权声明保留在 [LICENSE-MIT](./LICENSE-MIT)。MIT 与 GPL-3.0 兼容，本项目在 MIT 授权的原始代码
基础上继续以 GPL-3.0 发布；第三方组件各自遵循其原有许可，不因本仓库的 GPL 授权而改变。

---

## 一、随仓库一起分发的第三方组件

| 组件 | 位置 | 许可 | 出处 |
|---|---|---|---|
| Tencent WCDB（微信数据库框架，含 SQLCipher） | `weflow-core/resources/wcdb/**`（`WCDB.dll` / `libWCDB.dylib` / `wcdb_api.dll` / `libwcdb_api.so` / `libwcdb_api.dylib`） | BSD 3-Clause，Copyright (C) 2017 THL A29 Limited, a Tencent company | [Tencent/wcdb](https://github.com/Tencent/wcdb) |
| SDL2 | `weflow-core/resources/wcdb/win32/x64/SDL2.dll` | zlib | [libsdl-org/SDL](https://github.com/libsdl-org/SDL) |
| MSVC 运行库（`msvcp140*`、`vcruntime140*`） | `weflow-core/resources/{wcdb,runtime}/win32/` | Microsoft Visual C++ 可再发行组件条款 | Microsoft |
| DeepSeek 分词器数据 | `assets/deepseek_tokenizer/tokenizer.json` | 随 deepseek-ai 仓库发布 | [deepseek-ai](https://github.com/deepseek-ai) |
| 敏感词库 | `data/sensitive_words.txt` | 开源全类别词库（上游注释标注） | 随上游仓库分发 |

> `WCDB.dll` / `wcdb_api.dll` 等二进制由上游项目自行编译打包，本仓库按原样保留，仅用于读取微信本地数据库。

## 二、来源或授权需要使用者自行确认的文件

这些文件来自上游仓库，**没有附带明确的许可声明**，本仓库按上游现状保留。若要用于商业或严格合规场景，请自行核实或移除：

| 文件 | 说明 |
|---|---|
| `weflow-core/resources/key/**`（`wx_key.dll`、`libwx_key.dylib`、`xkey_helper*`、`image_scan_helper*`） | 微信数据库密钥提取辅助模块，来源与授权不明 |
| `weflow-core/resources/wedecrypt/**`（`weflow-image-native-*.node`） | 图片解密原生模块，来源与授权不明 |
| `assets/fonts/Minecraft AE.ttf` | Minecraft 字体，商标与版权归 Mojang / Microsoft |
| `assets/fonts/RoGSanSrfStd-Bd.otf` | ASUS ROG 字体，版权归 ASUS |
| `assets/fonts/GlowSansSC-Normal-Bold.otf` | 思源柔黑（Glow Sans），SIL OFL 1.1 |
| `assets/images/*`（含 `BlueArchive.png`、`Ba Logo.png`） | 二次元素材，版权归原权利方 |
| `music-source/星海音乐源 v2.3.11.js` | 第三方洛雪（LX Music）音源脚本，无授权声明（其余同目录脚本未分发，见 `music-source/README.md`） |
| `tools/douyin_*.py` 中的接口签名算法 | 与抖音前端行为相关，仅供学习研究 |
| `scripts/install_weflow_patch.py` | 社区补丁的安装器（`wcdb_api.dll` 过期修复，见 README「已知问题」）。文件头自述为 AGPL-3.0 代码；AGPL-3.0 与 GPL-3.0 兼容，但会把 AGPL §13 的网络使用条款引入组合作品。若不需要，删除该文件、改用 `scripts/patch_wcdb_api_expiry.py`（本仓库自有，GPL-3.0）即可 |

> **关于 `weflow-core/resources/wcdb/**` 的许可**：其中的 WCDB 本体来自腾讯
> [Tencent/wcdb](https://github.com/Tencent/wcdb)（BSD 3-Clause）；但社区补丁项目称，WeFlow
> 随包分发的这几个**预编译包装二进制**沿用 **CC BY-NC-SA 4.0（非商业 + 相同方式共享）**。
> CC BY-NC-SA 与 GPL-3.0 不兼容（NC 限制），本仓库按上游现状原样保留以供运行。若用于商业场景，
> 请自行向上游确认授权，或改为不随仓库分发、让使用者从自己的 WeFlow 安装目录复制这些文件。

## 三、未随仓库分发（请自行获取）

| 组件 | 获取方式 | 说明 |
|---|---|---|
| Node.js | [nodejs.org](https://nodejs.org/) | 上游用改名的 `node.exe`（`WeFlow.exe`）运行 `weflow-core`，本仓库不附带运行时 |
| Python 依赖 | `pip install -r requirements.txt` | |
| weflow-core 依赖 | `cd weflow-core && npm ci` | `node_modules/` 与构建产物 `dist/` 未提交 |
| cloudflared | [cloudflare/cloudflared](https://github.com/cloudflare/cloudflared)（Apache-2.0） | 原仓库 `bin/cloudflared.exe` 未分发 |
| VB-CABLE 虚拟声卡 | [vb-audio.com/Cable](https://vb-audio.com/Cable/) | 语音发送依赖，安装包未分发 |
| 抖音桌面客户端 / 微信客户端 | 官方渠道 | 本项目只做本地数据读取与界面自动化，不修改客户端 |

## 四、已从工作副本中删除的内容

出于隐私与安全考虑，发布版本删除了以下内容（不会出现在本仓库任何提交中）：

- `config.json`、`config/*.json`：真实 API Key、微信解密密钥、wxid、Cloudflare 隧道凭据。仓库只保留 `config/*.example.json` 占位模板。
- `data/`：群聊天记录、SQLite 记忆/学习库、登录态与浏览器配置、抓包产物、去重缓存、日志。
- `logs/`、`run.log`、`*.log`：运行日志，可能含聊天内容与他人昵称。
- `key_extract.log`：微信数据库解密密钥提取过程记录。
- `backup/`：按功能打包的历史快照。
- mitmproxy CA 证书与**私钥**（`.pem` / `.p12` / `.cer`）：私钥属于敏感凭据，公开会导致可被中间人攻击。
- 真实群名与机器人触发词：源码中的私有群名已替换为 `目标群` / `Your Target Group`，触发词默认值改为 `@bot`。
- 私有域名（Cloudflare 隧道主机名）与个人绝对路径：分别改为 `DOUYIN_PUBLIC_HOST` 环境变量和 `DOUYIN_LIB_DIR` 环境变量，默认值见对应文件。
