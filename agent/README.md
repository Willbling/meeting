# 会议室命令行智能体

程序连接现有的 MCP 服务，通过支持函数调用的 Chat Completions 模型完成多轮预约对话。模型服务会收到对话内容和用于回答的会议工具结果。命令行退出后，本次对话历史随进程清除。

在项目根目录打开 PowerShell，使用 Conda 的 py313 环境启动 FastAPI。

```powershell
conda activate py313
python -m pip install -r api/requirements.txt -r agent/requirements.txt
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

保持 FastAPI 运行，在另一个 PowerShell 窗口设置模型和本地测试身份。模型地址填基础地址，例如 `https://api.openai.com/v1`。程序会拼接 `chat/completions`。换用其他兼容服务时，填该服务的地址、模型和密钥。

```powershell
conda activate py313
$env:MEETING_LLM_BASE_URL = 'https://api.openai.com/v1'
$env:MEETING_MODEL = '替换为支持工具调用的模型名'
$env:MEETING_PERSON_ID = 'P000001'
$env:MEETING_LLM_API_KEY = Read-Host '输入模型 API 密钥' -MaskInput
python -X utf8 -m agent.main
```

智能体会自动启动 stdio MCP 子进程。API 默认位于 `http://127.0.0.1:8000`，也可以通过 `MEETING_API_URL` 指定。兼容的本地模型服务如果允许匿名请求，可以省略密钥。输入 `/exit` 结束对话。

可以先输入「帮我查一下明天有哪些会议」，再试「帮周宁订明天下午两点到三点的会议室，任选一间，主题是需求评审」。后一句会向当前 FastAPI 所用的数据库写入预约。系统提示词和示例在 `prompts.md`。

## 用 Ollama 测试 DeepSeek-R1

测试脚本读取项目根目录 `.env` 中的 `OLLAMA_API_BASE` 和 `OLLAMA_MODEL`，默认使用 `http://localhost:11434` 和 `deepseek-r1:1.5b`。脚本自动补上 Ollama 兼容接口的 `/v1`，本地 Ollama 无需 API 密钥。保持 Ollama 运行，在项目根目录用 `py313` 执行 `python -X utf8 -m agent.test_deepseek --ping`，验证模型能否应答。

测试预约功能时，先启动上面的 FastAPI，再在另一个 PowerShell 窗口运行以下命令。脚本默认用样例人物 `P000001`，并打印模型实际调用的工具和结果状态。

```powershell
# 默认查询周宁，预期编号为 P000002
python -X utf8 -m agent.test_deepseek

# 只读测试
python -X utf8 -m agent.test_deepseek "查一下我有哪些预约，列出申请编号和状态"
python -X utf8 -m agent.test_deepseek "查明天下午两点到三点能坐 8 人的空闲会议室"

# 多轮测试
python -X utf8 -m agent.test_deepseek --chat
```

单次测试以至少一个工具成功返回为通过条件。只有「模型原始回复」且没有成功的工具轨迹时，表示模型没有取得查询结果。本机的 `deepseek-r1:1.5b` 实测可以应答，但在查人样例中没有发出工具调用。单独检查 MCP 时，可在 FastAPI 运行后执行 `python -X utf8 mcp_test.py`。

多轮模式可以先说「帮周宁订明天下午两点到三点的会议室，任选一间，主题是联调测试」，成功后用实际返回的申请编号继续说「把这场改到明天下午三点到四点」，再说「取消这场会议」。这些操作会修改当前 FastAPI 所连接的数据库。
