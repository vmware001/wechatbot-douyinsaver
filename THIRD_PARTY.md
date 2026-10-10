# 第三方组件与许可说明

本仓库的第一方源码以 **GPL-3.0-or-later** 发布（见 [LICENSE](./LICENSE)）。

上游项目 [oldicecube/GSMPSWechatBot](https://github.com/oldicecube/GSMPSWechatBot) 以 **MIT** 许可发布，
其原始版权声明保留在 [LICENSE-MIT](./LICENSE-MIT)。MIT 与 GPL-3.0 兼容，本项目在 MIT 授权的原始代码
基础上继续以 GPL-3.0 发布；第三方组件各自遵循其原有许可，不因本仓库的 GPL 授权而改变。

---

## 一、随仓库一起分发的第三方内容

| 组件 | 位置 | 许可 | 出处 |
|---|---|---|---|
| DeepSeek 分词器数据 | `assets/deepseek_tokenizer/tokenizer.json` | 随 deepseek-ai 仓库发布 | [deepseek-ai](https://github.com/deepseek-ai) |
| 敏感词库 | `data/sensitive_words.txt` | 开源全类别词库（上游注释标注） | 随上游仓库分发 |
| Glow Sans 字体 | `assets/fonts/GlowSansSC-Normal-Bold.otf` | SIL OFL 1.1 | Glow Sans |

## 二、来源或授权需要使用者自行确认的文件

这些文件来自上游仓库，**没有附带明确的许可声明**，本仓库按上游现状保留。若要用于商业或严格合规场景，请自行核实或移除：

| 文件 | 说明 |
|---|---|
| `assets/fonts/RoGSanSrfStd-Bd.otf` | ASUS ROG 字体，版权归 ASUS |
| `assets/images/*`（含 `BlueArchive.png`、`Ba Logo.png`） | 二次元素材，版权归原权利方 |
| `music-source/星海音乐源 v2.3.11.js` | 第三方洛雪（LX Music）音源脚本，无授权声明（其余同目录脚本未分发，见 `music-source/README.md`） |
| `tools/douyin_*.py` 中的接口签名算法 | 与抖音前端行为相关，仅供学习研究 |
| `scripts/install_weflow_patch.py` | 社区补丁的安装器（`wcdb_api.dll` 过期修复，见 README「已知问题」）。文件头自述为 AGPL-3.0 代码；AGPL-3.0 与 GPL-3.0 兼容，但会把 AGPL §13 的网络使用条款引入组合作品。若不需要，删除该文件、改用 `scripts/patch_wcdb_api_expiry.py`（本仓库自有，GPL-3.0）即可 |

## 三、未随仓库分发（请自行获取）

完整依赖清单、项目地址与安装步骤见 **[SETUP.md](./SETUP.md)**；这里只列与许可直接相关的部分：

| 组件 | 获取方式 | 为什么不分发 |
|---|---|---|
| **WeFlow 安装包里的 `resources/`**：`wcdb_api.dll`、`WCDB.dll`、`SDL2.dll`、MSVC 运行库、`key/**`、`wedecrypt/**` | 装好 [hicccc77/WeFlow](https://github.com/hicccc77/WeFlow) 后，把它的 `resources\resources\` 复制到本项目的 `weflow-core\resources\`（SETUP.md 第 3 步） | WCDB 本体是腾讯的 BSD 3-Clause，但 WeFlow 打包的这几个**预编译二进制**被社区指为 **CC BY-NC-SA 4.0（非商业 + 相同方式共享）**；CC BY-NC-SA 与 GPL-3.0 不兼容，不适合随开源仓库分发。`key/**`（密钥提取）与 `wedecrypt/**`（图片解密）则来源与授权不明 |
| Node.js | [nodejs.org](https://nodejs.org/) | 上游用改名的 `node.exe`（`WeFlow.exe`）跑 `weflow-core`，运行时不该进仓库 |
| Python / npm 依赖 | `pip install -r requirements.txt`、`cd weflow-core && npm ci` | 交给包管理器，保证版本与来源 |
| cloudflared | [cloudflare/cloudflared](https://github.com/cloudflare/cloudflared)（Apache-2.0） | 上游自有发布渠道；原仓库的 `bin/cloudflared.exe` 未分发 |
| VB-CABLE 虚拟声卡 | [vb-audio.com/Cable](https://vb-audio.com/Cable/) | 第三方驱动的安装包不转分发 |
| mitmproxy、ComfyUI、Chrome、微信与抖音客户端 | 见 [SETUP.md](./SETUP.md) 第一节 | 同上 |
| 第三方洛雪音源脚本 | 自行获取后放进 `music-source/` | 无授权声明 |

## 四、公开仓库里没有的内容

下列内容不会出现在本仓库任何提交中：

**脱敏删除（隐私与安全）**

- `config.json`、`config/*.json`：真实 API Key、微信解密密钥、wxid、Cloudflare 隧道凭据。仓库只保留 `config/*.example.json` 占位模板。
- `data/`：群聊天记录、SQLite 记忆/学习库、登录态与浏览器配置、抓包产物、去重缓存、日志。
- `logs/`、`run.log`、`*.log`：运行日志，可能含聊天内容与他人昵称。
- `key_extract.log`：微信数据库解密密钥提取过程记录。
- `backup/`：按功能打包的历史快照。
- mitmproxy CA 证书与**私钥**（`.pem` / `.p12` / `.cer`）：私钥属于敏感凭据，公开会导致可被中间人攻击。
- 真实群名与机器人触发词：源码中的私有群名已替换为 `目标群` / `Your Target Group`，触发词默认值改为 `@bot`。
- 私有域名（Cloudflare 隧道主机名）与个人绝对路径：分别改为 `DOUYIN_PUBLIC_HOST` 环境变量和 `DOUYIN_LIB_DIR` 环境变量，默认值见对应文件。

**许可原因移除（第三方内容）**

- `weflow-core/resources/**`：来自 WeFlow 安装包的二进制，见第三节；请自行复制，复制后仍不建议提交（已被 `.gitignore` 忽略）。
- `assets/fonts/Minecraft AE.ttf`：Mojang / Microsoft 的专有字体。代码不按文件名引用字体，移除不影响运行。
- `music-source/` 里另外三个音源脚本（`独家音源-v5.js`、`聚合音源 特供版.js`、`长青SVIP音源(二改修复版) v1.2.0.js`）：无授权声明。
- `bin/cloudflared.exe`、`installers/`、`assets/vbcable/` 里的第三方安装包。
