# GSMPSWechatBot 前后端通信协议 v1

前后端分离架构下的全部进程边界与 JSON 契约。任何一侧改动协议时必须同步更新本文档并递增 `protocol_version`。

## 进程拓扑

```
前端（WeChat-facing，两个独立常驻进程）
  ├─ weflow-core      Node    127.0.0.1:5031   解密微信DB / SSE推送新消息 / 取图取媒体
  └─ sender-service   Python  127.0.0.1:9999   UIA 驱动微信窗口发消息（sender_service.py）

后端（纯大脑，单 Python 进程，main.py）
  SSE client → 队列 → worker → router → dispatcher/plugins → LLM → sender(HTTP client)
```

原则：后端不直接触碰微信进程/窗口/数据库；前端不含任何回复逻辑。去重、已答记录、限流全部在后端。

---

## 一、FE → BE：新消息推送（SSE）

`GET {weflow_url}/api/v1/push/messages?access_token={token}`

- 持续连接，`data: {...}\n\n` 逐条推送。
- 消费方：`core/weflow_client.py`（`WeFlowClient.start` / `_normalize_msg`）。
- 事件 JSON 主要字段（后端实际消费的）：
  | 字段 | 说明 |
  |---|---|
  | `talker` / `sessionId` | 会话 id（群为 `xxx@chatroom`） |
  | `senderWxid` | 发送者 wxid（群消息） |
  | `content` | 文本内容（XML/引用消息为原始结构，由后端解析） |
  | `localType` / `type` | 消息类型（1=文本，3=图片，49=引用/复合…） |
  | `localId` / `serverId`(svrid) | 消息定位 id，用于 `/messages/original` 查询与取图 |
  | `createTime` 等 | 时间戳（`extract_timestamp` 兼容多个键名） |

## 二、BE → FE：消息/媒体查询（weflow-core :5031）

认证：`Authorization: Bearer {token}` 或 `?access_token={token}`。

| 端点 | 用途 | 消费方 |
|---|---|---|
| `GET /health` | 健康检查，返回 `{"service":"weflow-core",...}` | `core/weflow_headless.py` |
| `GET /api/v1/messages?talker=&limit=&media=1&image=1` | 拉取会话最近消息（含 mediaUrl） | `core/weflow_media.py` |
| `GET /api/v1/messages/original?talker=&localId=&serverId=` | 取单条原始消息（引用解析/上下文补水） | `core/weflow_client.py`、`core/worker.py` |
| `GET /api/v1/media/<path>` | 下载图片/媒体二进制 | `core/weflow_media.py` |
| `GET /api/v1/group-members?chatroomId=` | 群成员列表 | plugins |
| `GET /api/v1/sessions?keyword=` | 会话搜索 | 调试 |

服务地址可在 `config.json` 的 `frontend.weflow_url` 覆盖（默认 `http://127.0.0.1:5031`）。

## 三、BE → FE：发送消息（sender-service :9999）

`POST {sender_url}/wxSend`，`Content-Type: application/json`

请求体（`content` / `file` / `voice` 三者有且仅有一个）：

```json
{
  "target": "目标群",
  "content": "文本内容",
  "file": "C:\\path\\to\\file.zip",
  "voice": "C:\\path\\to\\voice.mp3",
  "duration": 59.5,
  "start": 0.0
}
```

- `target`：聊天对象名（群名或备注名），必填。
- `voice` 可为 http(s) URL（服务端先下载）或本地路径；`duration` 1~60 秒；`start` ≥0。
- 成功响应 `200`：`{"status":"success","message":"..."}`
- 失败响应 `4xx/5xx`：`{"code":1,"error":"...","details":"..."}`
- 服务端另有 `GET /health` → `{"status":"ok","service":"sender","protocol_version":1}`、`GET /test`（UIA 自检，会操作微信窗口，勿在群里乱调）、`GET /voice/status`（虚拟麦克风状态）、`GET /voice/rescan`（装驱动/热插拔后强制重新枚举音频设备，免重启）。

消费方：`core/sender.py`（`_post_wxsend`，连接失败重试 2 次指数退避）。
服务地址可在 `config.json` 的 `frontend.sender_url` 覆盖（默认 `http://127.0.0.1:9999`）。

**超时约定**：文本发送可能分段粘贴耗时 >10s，客户端 timeout=60s；语音 timeout=90s。服务端在客户端断开后写响应失败只记 warning，不影响已发出的消息。

## 四、config.json 相关配置

```json
"frontend": {
  "weflow_url": "http://127.0.0.1:5031",
  "sender_url": "http://127.0.0.1:9999"
}
```

## 五、调试速查

```bash
# 前端存活检查
curl http://127.0.0.1:5031/health
curl http://127.0.0.1:9999/health

# 独立测试发送链路（不经过后端/LLM）
curl -X POST http://127.0.0.1:9999/wxSend -H "Content-Type: application/json" \
  -d '{"target":"文件传输助手","content":"frontend send test"}'

# 独立测试消息拉取
curl "http://127.0.0.1:5031/api/v1/messages?talker=<wxid>&limit=5&access_token=<token>"
```

故障定位：5031 挂 = 收不到消息；9999 挂 = 发不出去；后端挂 = 不回复但前端收发正常。
