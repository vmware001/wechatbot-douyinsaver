# music-source/

洛雪（LX Music）自定义音源脚本目录。`/song`、`/music` 会按 `source_order.txt`（若存在）或文件名排序
依次尝试这里的 `.js` 脚本解析歌曲直链（见 `services/music/`）。

**本仓库不附带第三方音源脚本**：这类脚本由第三方编写、大多没有授权声明，部分还绕过音乐平台的付费限制，
不适合随仓库分发。请自行获取有合法授权的音源脚本放进本目录。

用法：

1. 把 `*.js` 音源脚本放到本目录；
2. 如需固定尝试顺序，新建 `source_order.txt`，每行写一个文件名；
3. 没有音源可用时，点歌会回退到网易云官方接口（`music.allow_netease_fallback`）。

音源解析由 Node.js 常驻守护进程执行（`services/music/lx_daemon.mjs`），需要本机已安装 Node.js。
