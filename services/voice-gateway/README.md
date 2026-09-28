# Being 云端语音服务

这里保存桌面与手机通话共用的 Python 语音服务源码。此前该服务单独部署；本次纳入仓库的 13 个运行模块与 2026-09-28 已部署版本一致，包含查询短标题与按主题提醒。它是独立服务，不随 Electron 在用户电脑上启动。

## 查询标题与报告

- 豆包调用 `ask_agent` 时同时给出完整 `message`、简短 `title` 和过渡句。标题不需要额外模型请求。
- `TaskStore` 分开保存原请求和短标题；去重继续按原查询判断，复用时不覆盖原标题。旧数据库自动添加 `request` 列，保留原问题、正文和播放位置；旧标题按原问题缩短显示。
- 正文流式返回后，报告在空闲时说明主题，例如“刚才查的项目甲发布进度，有内容返回了”。打断后问“关于项目甲发布进度，刚才还没讲完，你还要继续听吗？”
- 续播、查询失败和部分结果也带上同一主题。查询有返回不等于外部业务任务已完成。

## 运行与配置

需要 Python 3.11 或更新版本。安装运行依赖：

```sh
python -m pip install -r services/voice-gateway/requirements.txt
```

当前源码保留现网数据目录 `/data/private/wpy/minicpm-voice`：`doubao_config.py` 的 `ROOT` 控制凭据、资料与任务库路径；`voice_usage.py` 的 `PATH` 控制用量日志路径。迁移到其他目录时需同时调整这两处。

由部署环境提供以下私有文件，不要放入 Git：

- `doubao-api-key.txt`：豆包 API Key，仅服务器读取。
- `agent-url.txt`：旧默认 Being 的完整 streaming 链接，包含 token，供启动及旧资料兼容使用。每个新连接仍须提供自己的 Being 链接并通过验证。
- 运行时生成的 `agent-tasks.sqlite3`、`assistant-profile*.json` 和 `logs/`：查询结果、助手资料和用量数据，不属于源码。

准备数据目录、私有配置和日志目录后启动：

```sh
python services/voice-gateway/doubao_server.py
```

服务监听 `127.0.0.1:22601`，提供 `/health` 与 `/ws`。桌面使用 HTTPS 网关转发后的 `/pipeline/ws`；部署环境须提供 TLS、WebSocket 转发和适当的访问控制。此目录不包含现有反向代理或手机网页资产，也不会自动替换线上进程。

更新已有部署时先备份源码和 SQLite，等待通话及后台查询空闲，再重启语音服务。首次启动会迁移数据库；已有未完成任务由启动恢复逻辑处理。

## 验证

```sh
python services/voice-gateway/run_tests.py
```

九个离线脚本只使用 Python 标准库、临时数据库和模拟音频/Agent。覆盖标题与原请求分离、旧库迁移、多任务主题、重复查询、流式报告、断线续接、打断与播放游标、部分失败和空闲调度。不会读取真实密钥、录音或调用付费接口。

上线前另以合成测试问题验证过豆包能在原工具调用中返回标题并保留完整查询条件，未调用真实 Agent；现场凭据、测试录音和部署备份没有纳入仓库。
