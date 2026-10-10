# 脱敏对照清单（发布前检查表）

> **这是什么**：本项目发布到 GitHub 时，哪些内容必须排除、哪些源码里的硬编码隐私必须替换的**完整地图**。
> 它由作者本地的 `PRIVACY_MANIFEST.md` 整理而来，供 fork 或二次发布的人照着做。
>
> **它本身不含任何真实密钥/群名/域名**：所有敏感值一律用占位符描述（`目标群`、`<域名>`、`<token>`），
> 真实值请从它指出的**原始文件**里现取。这份清单可以安全公开发布。
>
> 最后更新：2026-10-10

---

## 0. 脱敏时的操作纪律

1. 真实值**取到内存里就用**，不要打印到终端或对话里；报告一律用占位符（`目标群`、`<域名>`、`<token>`）。
2. 顺序：先排除第 1 类（实体文件）→ 再替换第 2 类（源码硬编码）→ 再查第 3 类（授权/合规）→
   跑第 5 节的扫描清单 → 编译检查 → 提交。**任何一次都不要"凭记忆"跳过扫描。**
3. 每轮发布前重新扫一遍：新出现的文件（尤其 `tools/`、`plugins/`、`config/`、`docs/`）都可能带新隐私。

---

## 1. 绝不能带走的实体文件 / 目录

| 路径 | 里面有什么 | 处理 |
|---|---|---|
| `config.json` | 内部 API token、微信 `decryptKey`（64 位 hex）、`myWxid`、`target_group`（真实群名）、`prefix`（触发词）、`llm.apis[*].api_key` + `base_url`、`group_features` | 不发布；只发 `sample_config.json` |
| `config/cloudflare.json` | `api_token`、`tunnel_token`、`hostname`/`zone`（私有域名）、`tunnel_id` | 不发布；发 `config/cloudflare.example.json` |
| `config/douyin_forwarder.json` | 内部发送 `token`（与 `config.json` 的 token 相同）、`target`/`allowed_targets`（私有群名）、`messages_file` 路径 | 不发布；发 `.example.json` |
| `config/douyin_saver.json` | `group_name`/`group_names`（私有监听群名）、`save_root`/`label_pool_dir`（个人盘路径） | 不发布；发 `.example.json` |
| `key_extract.log` | 微信数据库解密密钥的提取过程（可能含密钥片段） | 不发布 |
| `msgs.json`、`sse_test.txt` | 抓下来的真实消息样例 | 不发布 |
| `run.log`、`run_old.log`（若有） | 7 MB+ 运行日志，含群名、昵称、聊天内容、token 前缀 | 不发布 |
| `logs/`（`sender.log*`、`image_debug/`、`recover_label_media*.log`） | 发送日志与 OCR 截图，含群名/标题/聊天摘要 | 不发布 |
| `watch.log`、`wizard.log`、`@AutomationLog.txt` | 本地调试痕迹 | 不发布 |
| `data/groups/<群名>/group_messages.json` | **群聊天记录 + 群成员昵称**（最敏感） | 不发布 |
| `data/*.sqlite3`（`llm_learning` / `memory` / `bot`） | 记忆、学习到的黑话/句式/人物画像、积分 | 不发布 |
| `data/dedup_cache.json`、`data/bot_stats.json`、`data/echo.json`、`data/local_store.json` | 去重缓存、统计、回声树洞内容 | 不发布 |
| `data/_page.html`、`data/_sc.json`、`data/_ui_ocr.txt`、`data/thumb1_label.png` | 抓包页面、脚本快照、界面 OCR 文本、截图 | 不发布 |
| `data/chrome_douyin/`、`data/edge_douyin/` | 浏览器 profile（**登录态 Cookie**、历史） | 不发布 |
| `data/douyin_mitm/` | mitmproxy 抓包产物（含 `conf/` 里的 CA） | 不发布 |
| `data/douyin_saved`、`data/label_pool` | 指向个人盘（抖音库根目录，见 `config/douyin_*.json`）的软链接 | 不发布 |
| `data/pics/`、`data/emoji/`、`output/` | 自备素材与生成物 | 不发布 |
| `*datadouyin_mitmconf/`（名字被路径拼接搞坏的目录） | mitmproxy CA 证书 + **私钥**（`.pem`/`.p12`/`.cer`） | 不发布（安全风险） |
| `backup/` | 历史快照，上面这些东西的副本 | 不发布 |
| `runtime/`、`.venv/`、`__pycache__/`、`node_modules/`、`weflow-core/dist/`、`weflow-core/.weflow-data/` | 运行时/构建产物 | 不发布 |
| `weflow-core/WeFlow.exe` | 改名后的 node.exe（85 MB） | 不发布 |
| `bin/cloudflared.exe`、`installers/`、`assets/vbcable/`、`welive/` | 第三方二进制/安装包 | 不发布（见第 3 类） |

**`data/` 里可以发布的例外**（词表，无个人数据）：
`blacklist.json`、`fortune_words.json`、`life_words.json`、`sensitive_words.txt`、`sensitive_words_ignore.txt`。

---

## 2. 源码里需要替换/检查的硬编码隐私

| 类别 | 真实值从哪来 | 出现位置（2026-10-10 扫描结果） | 替换成 |
|---|---|---|---|
| **私有微信群名** | `config.json → target_group`；`data/groups/` 下的目录名；`config/douyin_*.json → group_name(s)/target/allowed_targets` | `core/sender.py`、`core/wechat_sender/wechat_sender_v3.py`（注释与 OCR 示例多处）、`core/wechat_sender/wechat_hook_server.py`、`plugins/help.txt`、`plugins/shit.py`、`tools/douyin_saver.py`、`tools/douyin_forwarder.py`、`docs/PROTOCOL.md`、`test_summary_dryrun.py` | `目标群` / `Your Target Group`；OCR 示例同步改字符（如 `'目标群' → '目 标 群'`） |
| **机器人触发词** | `config.json → prefix` | `core/router.py`（含推导 bot 名字的逻辑）、`core/worker.py`（默认值）、`core/dispatcher.py`、`core/daily_summary.py`、`llm/prompt/prompt_builder.py`、`llm/core/llm_service.py`、`plugins/help.txt` | `@bot` / `bot` |
| **私有域名** | `config/cloudflare.json → hostname` / `zone` | `tools/douyin_console.py` 的 `PUBLIC_HOST` | `os.environ.get("DOUYIN_PUBLIC_HOST", "label.example.com")` |
| **个人盘路径** | `config/douyin_*.json → save_root` / `label_pool_dir` | `tools/bt_model.py`、`tools/douyin_console.py`、`tools/douyin_scroll_watch.py` 里硬编码的抖音库根目录 | `os.environ.get("DOUYIN_LIB_DIR", …)`（默认值可留） |
| **安装路径** | 本机目录 | 约 72 处 `*.bat` / `*.ps1` 里的 `C:\GSMPSWechatBot` | 可保留，但必须在 README 注明"克隆到别处要改" |
| **Windows 用户名** | 本机 | 任何 `C:\Users\<用户名>` 残留（发布前必须为 0） | 不要出现 |
| **真人出镜** | — | `plugins/jane.py`：以真实群成员为原型的人设 + 8 句"真实说过的话"（**2026-10-10 已改为虚构人设与虚构示例句**，发布前复查一遍）；`data/thumb1_label.png`、`data/pics/`、`logs/image_debug/` 里的截图 | 去掉人设中的真人特征，或整块删除该插件 |
| **内部 token 复用** | `config.json → token` | 与 `config/douyin_forwarder.json → token` 相同，换名字也还是密钥 | 两者在示例模板里都写 `your-weflow-token` |

---

## 3. 不是隐私、但同样不能发布（授权 / 合规）

| 内容 | 原因 |
|---|---|
| `weflow-core/resources/**` | WeFlow 安装包里的**预编译二进制**（`wcdb_api.dll`/`WCDB.dll`/`SDL2.dll`/MSVC 运行库/`key/**`/`wedecrypt/**`）。社区指其沿用 **CC BY-NC-SA 4.0（非商业）**，与 GPL-3.0 不兼容；`key/**`、`wedecrypt/**` 授权不明。**改由使用者从自己的 WeFlow 安装目录复制**（见 [SETUP.md](./SETUP.md) 第 3 步） |
| `assets/fonts/Minecraft AE.ttf` | Mojang / Microsoft 专有字体（本仓库已移除） |
| `assets/fonts/RoGSanSrfStd-Bd.otf` | ASUS 专有字体（**目前仍在仓库里**，建议一并移除） |
| `music-source/*.js` | 第三方洛雪音源脚本，无授权声明（目前只保留了 `星海音乐源`，也可整目录不发） |
| `bin/cloudflared.exe`、`installers/douyin-pc-setup.exe`、`assets/vbcable/*.exe`、`welive/` | 第三方二进制/安装包 |

---

## 4. 可以安全发布的白名单（参考）

- 第一方源码：`core/`、`plugins/`、`auto/`、`llm/`、`services/`、`utils/`、`anima/`、`scripts/`（含两个 wcdb 补丁脚本）、`tools/`（脱敏后）、`weflow-core/src|package.json|esbuild*.mjs|tsconfig.json`
- 文档：`README.MD`、`DEV_GUIDE.md`、`config_guide.md`、`SETUP.md`、`THIRD_PARTY.md`、`TODO.md`、`admin_commands.txt`、`docs/`
- 模板：`sample_config.json`、`config/*.example.json`、`config/README.md`、`music-source/README.md`
- 词表：`data/` 的 5 个白名单文件
- 脚本：启动/停止用的 `*.bat` / `*.ps1`（路径说明见上）
- 授权文件：`LICENSE`(GPL-3.0)、`LICENSE-MIT`（上游 MIT 声明）、`THIRD_PARTY.md`
- 需要保留判断的素材：`assets/images/*`、`assets/fonts/GlowSansSC-Normal-Bold.otf`(OFL)、
  `assets/deepseek_tokenizer/tokenizer.json`、`music-source/星海音乐源 v2.3.11.js`（已在 THIRD_PARTY 声明）

---

## 5. 发布前扫描清单（照着跑，别跳）

**先从原始文件取真实值到内存（不要打印）**：`config.json → target_group / prefix`、
`data/groups/` 目录名、`config/cloudflare.json → hostname / zone`、两个 `config/douyin_*.json` 里的群名。

**关键词扫描**（对发布副本，`--glob '!data/sensitive_words*'`）

- 上面的所有群名、触发词、域名
- `C:\Users\`（应为 0 命中）、个人盘路径、`wxid_`（只允许 `wxid_xxx` 这类占位符）

**正则扫描**

| 目标 | 正则 |
|---|---|
| API Key | `sk-[A-Za-z0-9_\-]{16,}`、`AIza[0-9A-Za-z_\-]{30,}`、`ghp_[A-Za-z0-9]{30,}` |
| 64 位 hex（微信密钥） | `\b[0-9a-fA-F]{64}\b` |
| token 字段 | `"token"\s*:\s*"[^"]{16,}"`、`api[_-]?key"?\s*[:=]\s*"[^"]{12,}"` |
| 私钥 | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |
| 手机号/邮箱 | `\b1[3-9]\d{9}\b`、`[\w\.\-]+@[\w\-]+\.[a-z]{2,}` |

**文件类型扫描**：`*.pem`、`*.p12`、`*.cer`、`*.log`、`*.sqlite3`、`*.jsonl`、`config.json`、`*.bak-expiry`、
`__pycache__`、`node_modules`、`*.exe` —— 发布副本里都不该有（白名单除外）。

**二进制抽查**：>1 MB 的文件核对 magic bytes（`ttf/otf` 是真字体、`MZ` 是 PE、ELF/Mach-O 是原生库），
确认没有伪装成素材的压缩包/数据库；顺手确认二进制里没有用户名等字符串
（注意：`sqlite3BtreeSetPageSize` 这类符号名里**偶然**出现的、与用户名重合的子串是巧合，不是泄漏）。

**收尾**：`python -m compileall` 全过；`git ls-tree` 文件数与预期一致；工作区干净。

---

## 6. 历史记录

**2026-10-10 第一次公开发布** → <https://github.com/vmware001/wechatbot-douyinsaver>（GPL-3.0，已署名原作者 oldicecube）

- 已排除：`config.json`、`config/*.json`、`data/`（除 5 个词表）、`logs/`、`run.log`、`key_extract.log`、
  `msgs.json`、`sse_test.txt`、`backup/`、mitmproxy CA 私钥目录、`welive/`、`bin/`、`installers/`、
  `assets/vbcable/`、`.venv`/`node_modules`/`dist` 等
- 已替换：私有群名 → `目标群`、触发词 → `@bot`、私有域名 → `DOUYIN_PUBLIC_HOST`、个人盘路径 → `DOUYIN_LIB_DIR`
- 已清理真人信息：`plugins/jane.py` 的人设与"真实说过的话"改为虚构
- 已按授权移除：`weflow-core/resources/**`、`assets/fonts/Minecraft AE.ttf`、3 个未授权音源脚本
- 新增：`SETUP.md`、两个 `wcdb_api.dll` 过期修补脚本、`THIRD_PARTY.md` 说明；`requirements.txt` 补齐 6 个漏列依赖
- **下次要复查的**：`plugins/jane.py` 是否又出现真人特征；`tools/` 与 `docs/` 里是否有新的群名/昵称；
  `RoGSanSrfStd-Bd.otf` 是否仍保留
