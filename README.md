# 会议室预订系统

项目使用 Conda `py313`。SQLite 存储人员、会议室和预约，FastAPI 提供业务接口，MCP 提供工具。命令行智能体可连接 Ollama，企业微信入口目前提供单聊查询和群聊空闲会议室查询。

## 本地启动

在项目根目录运行以下命令。初始化脚本会根据建表和样例 SQL 创建数据库。再次运行会保留已有数据。

```powershell
conda activate py313
python -m pip install -r agent/requirements.txt -r wecom/requirements.txt
python data/sqlite/init_db.py
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

首次配置时，将 `.env.example` 复制为本地 `.env`，按需填写企业微信凭据。Git 跟踪样例配置、建表 SQL 和样例数据 SQL。本地 `.env`、SQLite 数据库文件及其中的账号绑定留在工作目录。

## 使用说明

- [SQLite 数据与初始化](data/sqlite/README.md)
- [FastAPI 接口](api/README.md)
- [MCP 服务与测试](mcp_server/README.md)
- [命令行智能体与 Ollama 联调](agent/README.md)
- [企业微信长连接、账号绑定及群聊](wecom/README.md)
