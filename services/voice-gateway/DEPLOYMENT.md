# Being 语音网页版：打包与部署

本目录是当前手机语音网页与豆包 / GPT-Live 网关的独立交付单元；不需要 Electron、本地大模型或 GPU。语音模型和 Being Agent 仍通过网络调用。手机界面、字体、网关、完整流程、测试及部署示例一同交付。

## 生成部署包

在仓库根目录运行（Python 3.11+，打包本身不需要额外依赖）：

```sh
python services/voice-gateway/package.py
```

也可使用 `npm run package:voice-web`。生成 `out/being-voice-web.zip`，解压得到 `being-voice-web/`。`MANIFEST.json` 记录源提交、是否含未提交改动、逐文件 SHA-256 和大小。打包只读取 `package-files.json` 的明确清单，不递归收集数据目录；ZIP 本身不提交 Git，可随时从源码重建。

## 本机启动

进入解压目录，或仓库的 `services/voice-gateway/`：

```sh
python -m venv .venv
# Linux/macOS
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python serve.py --data-dir ./data
```

Windows PowerShell 对应命令：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe serve.py --data-dir ./data
```

访问 `http://localhost:22601/mobile/`。缺少 API Key 时仍可以打开网页和验证启动；两个模式均未配置时 `/pipeline/health` 的 `ready` 为 false，`providers` 分别给出配置状态。

在**服务端**的数据目录中创建 `doubao-api-key.txt`，内容仅为你自己的豆包端到端实时语音 API Key。不要写入 HTML/JS、提交到仓库或放进 ZIP。当前配置使用模型 `1.2.6.1`，需要账户具备相应接口权限；普通文本模型 Key 不保证可用于此语音接口。

使用 GPT-Live 时，在同一服务端数据目录创建 `openai-api-key.txt`，内容仅为可调用 `gpt-live-1` 的 OpenAI 项目 Key；也可通过进程环境变量 `OPENAI_API_KEY` 提供。文件应仅服务用户可读，例如 Linux `chmod 600 openai-api-key.txt`。现网数据目录为 `/data/private/wpy/minicpm-voice`。文件每次连接时读取，不需要重启；网页刷新后在设置中选择 GPT-Live。服务器需能连接 `wss://api.openai.com/v1/live/sessions`。配置存在不等于账户权限已通过验证；默认仍使用豆包。OpenRouter Key 不能替代此官方接口的 Key。

用户首次打开页面，在设置里输入自己的 Being 链接，例如 `https://echo.beings.town/用户名/?token=你的token`。后端验证链接并派生 streaming 地址。当前只允许 `echo.beings.town`，没有泛化为任意 HTTP Agent。

### 仅通过本地电脑转接 OpenAI

云端无法连接 OpenAI、本地电脑可以连接时，在本地运行：

```sh
python openai_local_relay.py --ssh-target 用户名@服务器地址 --ssh-port SSH端口
```

需要事先配置 SSH 密钥登录及可信 `known_hosts`；程序不接收或保存 SSH 密码。默认本地监听 `127.0.0.1:22618`，用 SSH 反向转发到服务器 `127.0.0.1:22619`。SSH 连接断开后每 5 秒重试，远端端口被占用时不会抢占。仅支持 `CONNECT api.openai.com:443`，不开放其他目标；TLS 不在电脑上解密，OpenAI Key 仍在服务器。

在服务器数据目录创建 `openai-proxy-url.txt`，内容为 `http://127.0.0.1:22619`；也可使用 `OPENAI_LIVE_PROXY` 环境变量（支持 HTTP/HTTPS CONNECT 代理）。仅 Live 连接读取该配置，不改变 Being、豆包或系统全局代理。普通环境变量代理在未指定专用代理时继续适用。代理私有文件不得打包或提交。

电脑需保持开机、不休眠、运行转接进程并能访问 OpenAI；此脚本不会自动安装开机启动。结束本地程序会断开 Live 通话，用户可以切换豆包。生产环境需保持 SSH `GatewayPorts no`，并核实远端转发只监听回环地址；不要将转接端口暴露公网。

新部署**不需要**公共默认 `agent-url.txt`；每个用户自行配置链接。旧环境的该文件仍可选，用于识别和复用旧助手资料，不会绕过新连接的 token 验证。

## 手机与 HTTPS

手机访问使用 HTTPS，浏览器才能正常授权麦克风；电脑的 localhost 可用于本机测试。`serve.py` 默认只监听 `127.0.0.1:22601`。把域名指向服务器，配置 HTTPS 反向代理，完整保留路径并支持 WebSocket。

[deploy/Caddyfile.example](deploy/Caddyfile.example) 提供同域代理示例。修改示例域名后由 Caddy 提供 HTTPS，再访问 `https://你的域名/mobile/`。本包无需原 GPU 平台端口映射；也不包含证书或私人代理配置。

| 路径 | 作用 |
| --- | --- |
| `/`、`/mobile` | 跳转手机通话页面 |
| `/mobile/`、`/pipeline/` | 当前通话页面 |
| `/call-assets/call.js`、两个 `.woff2` | 脚本与字体 |
| `/pipeline/ws` | 链接验证、资料、任务和通话 WebSocket |
| `/pipeline/health` | 配置就绪状态，不代表上游可达或额度充足 |
| `/ws`、`/health` | 兼容原网关路由 |

数据目录不通过 HTTP 提供。只有明确允许的静态文件可以下载。

## 长期运行

[deploy/being-voice-web.service](deploy/being-voice-web.service) 是 Linux systemd 示例：应用目录 `/opt/being-voice-web`，专用用户 `beingvoice`，数据目录 `/var/lib/being-voice-web`。先创建该用户、解压源码并安装虚拟环境，再把 Key 放进数据目录并授权该用户读取，最后安装并启用服务单元。其他路径需同步修改示例。

`serve.py --data-dir` 优先于 `VOICE_DATA_DIR`；两者都未提供时使用源码目录下的 `data/`。直接运行旧入口 `doubao_server.py` 仍默认使用原 `/data/private/wpy/minicpm-voice`，可用 `VOICE_DATA_DIR` 覆盖。

**运行单个 worker。** 当前在途去重、后台查询和通话调度保存在单进程内，SQLite 保存任务正文与游标。不能直接增加 Uvicorn workers 来扩容；多实例需要另行设计共享任务执行与路由。

## 数据与升级

| 位置 | 内容 |
| --- | --- |
| 浏览器 localStorage | 用户 Being 链接及偏好，含该用户 token |
| 浏览器 IndexedDB | 本机完整文字记录；豆包近期窗口最多约 24000 字，Live 注入最多 60 条 / 6500 UTF-8 字节 |
| 服务端数据目录 | 私有 Key、按用户 scope 保存的助手资料、任务 SQLite、数值用量日志 |

备份完整数据目录，等待通话和后台查询结束，再替换代码并重启。网页与网关同步升级，更新 HTML 的脚本版本。更换域名不会自动迁移浏览器本地缓存；轮换 Being token 会改变 scope，不会自动把旧 scope 的任务迁给新 token。

服务重启可恢复已保存的正文和游标；当时仍在执行的请求会标记 `interrupted`，不会自动重提交可能有副作用的操作。已有在线环境不必为了打包切换入口，本次整理不要求重启线上服务。

## 检查与限制

```sh
python run_tests.py
python test_web_bundle.py
python test_connection_startup.py
```

第一项是只使用标准库的网关回归；后两项需要先安装运行依赖，验证独立页面、路由、隔离数据目录、WebSocket 控制流程、打包清单及大量旧任务恢复时的首句连接，不请求真实豆包或 Agent。

完整桌面/手机音频模拟测试在仓库根目录运行：

```sh
npx vitest run tests/voice-playback.test.ts tests/voice-mobile.test.ts tests/voice-call.test.ts tests/voice-service.test.ts tests/voice-capture.test.ts
```

手机后台、锁屏、回声环境和供应商延迟仍需真机测试。当前网页在隐藏时结束通话，不承诺后台常驻。应用内已有“测试录音”入口，但交付包不包含现场录音；需要时自行放置获授权的 `mobile/self-test.wav`，或直接使用麦克风验证。

完整行为与状态说明见 [FLOW.md](FLOW.md)。


### 排查“有进度但没声音”

网关的 `logs/doubao.err.log` 增加不含正文/PCM的链路事件：`progress_queued`、`progress_synthesis`、`progress_forwarded`、`progress_finished`、`task_tool_result`、`task_natural_audio`、`task_natural_fallback`。按任务编号关联；`progress_forwarded` 只代表成功发送，不能当成用户已听到。

连接编号关联 `voice_transition`、`audio_forwarded`、`client_playback` 与 `client_audio`。Mac 的 `client_audio` 记录 scheduled、drained、paused-confirmed、paused-local、cleared、resumed，以及音频上下文状态、暂停/排队毫秒数、帧数和 generation。scheduled 表示进入音频节点，drained 表示队列耗尽，都不证明扬声器音量或用户实际听见。诊断不采集录音、转写、结果正文或凭据。

先判断是否根本没有收到后台正文，再区分调度等待、合成失败、发出后被暂停/清除、AudioContext 挂起。对照 generation 排除旧队列回执；无需用户贴出完整私密任务内容。
