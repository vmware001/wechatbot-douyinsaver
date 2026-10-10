# config/ 配置目录

本目录只提交 `*.example.json` 模板；真实配置（含密钥、群名、隧道凭据）已被 `.gitignore` 排除。

使用前把模板复制成不带 `.example` 的文件并填写真实值：

```powershell
Copy-Item config\cloudflare.example.json          config\cloudflare.json
Copy-Item config\douyin_forwarder.example.json    config\douyin_forwarder.json
Copy-Item config\douyin_saver.example.json        config\douyin_saver.json
```

| 文件 | 用途 |
|---|---|
| `cloudflare.json` | 公网标注页的 Cloudflare Tunnel 配置（`tools/cloudflared_run.py`） |
| `douyin_forwarder.json` | 抖音合集搬运：发送目标群、合并门槛、免打扰时段、发送服务地址 |
| `douyin_saver.json` | 抖音采集：监听群、下载阈值、标注池目录、调试端口 |
| `../sample_config.json` | 微信机器人主体配置模板（复制为仓库根目录 `config.json`） |

注意：`douyin_forwarder.json` 里的 `token` 必须与主体 `config.json` 的 `token` 一致（内部发送接口鉴权）。
