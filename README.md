# 会议室智能助手

这是一个支持企业微信和命令行的会议室预约助手。用自然语言就能查询会议安排和空闲会议室，也可以预订、改期或取消预约。助手会在多轮对话中补齐时间、会议室和参会人员等信息，并在你确认后提交。企业微信群里可以直接 @ 助手查房。

命令行和企业微信共用同一套 Agent。主 Agent 判断话题，会议子 Agent 解析会议请求，由预约流程调用 MCP 工具。MCP 将查询和写入转给 FastAPI，业务数据保存在 SQLite。企业微信还负责单聊身份绑定、群聊范围和会话记忆。

## 首次创建环境

首次使用时，安装 uv，然后在项目根目录打开 PowerShell，执行以下两条命令。环境和依赖准备好后，日常启动可以直接使用下一节的命令。

```powershell
uv venv --python 3.13 --managed-python .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

## 本地启动

以下命令都从项目根目录运行。`uv run` 会自动使用项目的 `.venv`。

### 1. 在第一个终端启动 API 和 MCP

```powershell
uv run python -X utf8 start_services.py
```

看到「服务已就绪」后，保持这个终端运行。脚本会在数据库文件缺失时初始化样例库，并依次启动 FastAPI 和 MCP。API 调试页为 [127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)，MCP 默认地址为 `http://127.0.0.1:8001/mcp`。

### 2. 确认模型服务在运行

启动 Ollama，用 `ollama list` 检查是否已安装默认模型 `qwen3.5:4b`。首次下载可运行 `ollama pull qwen3.5:4b`。使用其他兼容 Chat Completions 的服务时，按 [Agent 文档](docs/agent.md)配置模型地址。

### 3. 在另一个终端启动助手

**在企业微信使用**。首次配置时，若项目根目录还没有 `.env`，把 [.env.example](.env.example) 复制为 `.env`。确认 `.env` 中已填写 `WECOM_BOT_ID` 和 `WECOM_BOT_SECRET`，再执行下面的命令。

```powershell
uv run python -X utf8 -m wecom.main --reset-memory true
```

看到「企业微信已认证」后，保持终端运行，再到企业微信单聊「会议室助手」。首次使用可发送「我的账号」，按[企业微信使用说明](docs/wecom.md)完成身份绑定。上述命令保留现有会话记忆。省略 `--reset-memory` 时，启动会清空旧记忆。

**在命令行使用**。在另一个终端执行下面的命令。

```powershell
uv run python -X utf8 -m agent.main
```

停止时，在助手所在终端按 Ctrl+C，再在第一个终端按 Ctrl+C 停止 API 和 MCP。

### 自定义端口和数据库

修改 [services.toml](services.toml) 可设置 API 和 MCP 端口、数据库路径、测试人员及就绪超时，数据库相对路径以该配置文件所在目录为基准。使用 `uv run python -X utf8 start_services.py --config 路径.toml` 可指定其他配置。改了 MCP 端口时，为 CLI 和企业微信设置 `MEETING_MCP_URL`。改了数据库路径时，为企业微信设置 `MEETING_DB_PATH`，供机器人和绑定命令使用。

`testing.person_id` 用于 MCP 启动探测，也是命令行 Agent 的默认测试身份。企业微信单聊使用当前发送者绑定的人员编号。

## 项目结构

```text
meeting/
├── agent/                       # 命令行和企业微信共用的 Agent
│   ├── main.py                  # 命令行入口和参数
│   ├── factory.py               # 读取模型配置、创建 Agent、管理共享连接
│   ├── mcp_connection.py        # MCP 客户端的连接生命周期
│   ├── model.py                 # Chat Completions 客户端和结构化输出
│   ├── conversation.py          # 主 Agent：分流、聊天、协调、回复
│   ├── meeting.py               # 会议子 Agent：意图解析和工具调用
│   ├── workflow.py              # 查房、查预约、预约草稿、确认提交和核实
│   ├── workflow_models.py       # 会议意图、草稿、候选和流程状态
│   ├── memory.py                # 会话快照和历史消息格式
│   ├── prompt_loader.py         # 按文件名加载提示词
│   ├── prompts/                 # 按调用场景组织的提示词模板
│   ├── test_qwen.py             # 模型联调入口
│   └── test_workflow.py         # 多轮对话和预约流程回归测试
├── api/                         # FastAPI 业务接口
│   ├── main.py                  # 创建应用、注册路由和异常处理
│   ├── routes.py                # 就绪、人员、会议室和预约接口
│   ├── dependencies.py          # 数据库连接、操作者身份和时间参数校验
│   ├── models.py                # HTTP 请求和响应数据模型
│   ├── store.py                 # SQLite 查询、事务和预约规则
│   └── test_bookings.py         # 预约权限、并发和生命周期测试
├── mcp_server/                  # 将会议工具请求转为 FastAPI 调用
│   ├── server.py                # 创建服务、注册工具、启动 HTTP 或 stdio
│   ├── client.py                # API 客户端、响应校验和错误转换
│   ├── tools/
│   │   ├── directory.py        # 人员查询和空闲会议室查询
│   │   ├── bookings.py         # 预约列表、详情、新建、改期和取消
│   │   └── common.py           # 共享参数类型和工具响应结构
│   └── test_stdio.py            # MCP stdio 和 API 联调测试
├── wecom/                       # 企业微信渠道
│   ├── main.py                  # 启动配置和账号绑定命令
│   ├── bot.py                   # 长连接、消息去重、会话串行和流式回复
│   ├── messages.py              # 单聊、群聊和引用消息的处理
│   ├── identity.py              # 查询和更新企业微信账号绑定
│   ├── memory.py                # 会话记忆文件的加载、保存和清空
│   └── test_identity.py         # 身份、记忆和多轮交互测试
├── data/
│   ├── sqlite/
│   │   ├── init_db.py          # 初始化 SQLite
│   │   ├── schema.sql          # 表、视图、索引和触发器
│   │   └── seed.sql            # 样例人员、会议室和预约
│   └── wecom_memory/           # 机器人运行后生成的本地对话记忆
├── docs/                        # 模块说明、操作指南和对话设计
├── .env.example                 # 模型和机器人配置样例
├── requirements.txt             # Python 依赖及所属模块
├── services.toml                # 本地端口、数据库路径和测试身份
├── service_config.py            # 测试身份配置读取与校验
├── start_services.py            # 初始化数据库并启动 API 和 MCP
├── mcp_test.py                  # MCP HTTP 联调入口
└── README.md
```

## 文档

- [命令行 Agent、模型配置与提示词](docs/agent.md)
- [企业微信启动、绑定、群聊与记忆](docs/wecom.md)
- [对话设计、会议业务流程和主子 Agent 的交接](docs/conversation-design.md)
- [FastAPI 接口及业务约束](docs/api.md)
- [MCP 工具与联调](docs/mcp.md)
- [SQLite 初始化、表结构与样例查询](docs/sqlite.md)

## 验证

在项目根目录运行回归测试。测试使用临时数据库和模拟模型响应。

```powershell
uv run python -X utf8 -m unittest discover -v
```

模型和 HTTP 工具可分别联调。第二条命令需要先启动 API 和 MCP。

```powershell
uv run python -X utf8 -m agent.test_qwen --ping
uv run python -X utf8 mcp_test.py
```
