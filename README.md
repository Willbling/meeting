# 会议室预订系统

项目使用 Conda `py313`。SQLite 存储人员、会议室和预约，FastAPI 提供业务接口，MCP 提供工具。命令行和企业微信单聊共用 Ollama Agent，企业微信群聊可查询空闲会议室。

## 本地启动

在项目根目录用 PowerShell 安装依赖并一键启动 FastAPI 和 MCP。

```powershell
conda activate py313
python -m pip install -r api/requirements.txt -r mcp_server/requirements.txt
python -X utf8 start_services.py
```

启动器在数据库文件缺失时创建样例库，等 FastAPI 和数据库通过就绪检查后启动 MCP，再通过 MCP 客户端确认工具列表。默认 API 地址为 `http://127.0.0.1:8000/docs`，MCP 地址为 `http://127.0.0.1:8001/mcp`。按 Ctrl+C 停止两个服务。

修改 [services.toml](services.toml) 可以设置 `api.port`、`api.database`、`mcp.port` 和每个服务的就绪等待时间 `startup.timeout_seconds`。数据库相对路径以配置文件所在目录为基准。用 `python -X utf8 start_services.py --config 路径.toml` 指定其他配置。

如需启动 Agent 或企业微信，再分别安装 `agent/requirements.txt` 或 `wecom/requirements.txt`，按下方对应说明启动。如果修改了端口，命令行以及企业微信单聊和群聊使用 `MEETING_MCP_URL` 指向新 MCP 地址。企业微信的 `MEETING_DB_PATH` 也要指向 API 使用的数据库。

`services.toml` 的 `testing.person_id` 供 MCP 启动探测，并作为命令行 Agent 的本地测试默认值。企业微信单聊使用当前发送者的本地绑定。

首次配置时，将 `.env.example` 复制为本地 `.env`，按需填写企业微信凭据。Git 跟踪样例配置、建表 SQL 和样例数据 SQL。本地 `.env`、SQLite 数据库文件及其中的账号绑定留在工作目录。

## 使用说明

- [SQLite 数据与初始化](data/sqlite/README.md)
- [FastAPI 接口](api/README.md)
- [MCP 服务与测试](mcp_server/README.md)
- [命令行智能体与 Ollama 联调](agent/README.md)
- [企业微信长连接、账号绑定及群聊](wecom/README.md)
