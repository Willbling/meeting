# 会议室 MCP 服务

通过 Streamable HTTP 向 MCP 客户端提供会议室工具。服务独立运行，将请求转发给 FastAPI，由 API 执行权限、容量和时段冲突检查。

## 本地运行

推荐使用[一键启动脚本](../README.md#本地启动)。也可以单独启动。安装根目录依赖后，先按 [API 文档](api.md)初始化数据库，再在项目根目录启动 API。

```powershell
conda activate py313
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

保持 API 运行，在另一个 PowerShell 窗口从项目根目录启动 MCP。

```powershell
conda activate py313
$env:MEETING_API_URL = 'http://127.0.0.1:8000'
python -X utf8 -m mcp_server.server --port 8001
```

客户端连接 `http://127.0.0.1:8001/mcp`。启动时 MCP 用 `services.toml` 中的 `testing.person_id` 向 API 发起一次只读预约查询，验证 HTTP 连通和测试身份有效性。配置文件可通过 `--config` 指定。

预约及人员工具使用当次 MCP 请求的 `params._meta["meeting/person-id"]`。Agent 在程序侧填入操作者，API 负责预约权限校验。群聊使用的 `find_available_rooms` 支持无身份查询，返回房间信息和占用时段。HTTP 服务监听 `127.0.0.1`，人员编号由受信任的本机调用方传入。运行中 API 掉线时工具返回结构化错误，API 恢复后可以继续调用。本地 stdio 调试入口为 `python -X utf8 -m mcp_server.server --transport stdio`。

## 工具

- `find_people` 按 `name` 查人，返回姓名、`person_id`、职务和职级。同名时返回多个候选。
- `find_available_rooms` 按时间、人数或名称查房。默认 `mode=full` 要求全程空闲。`mode=intervals` 列出空闲段和占用段，支持 `min_duration_minutes`。
- `list_my_bookings` 查操作者创建、主持或参加的预约，可按 `status`、`room_id` 和相交时段过滤。
- `get_booking` 按 `booking_id` 查预约详情和与会名单。
- `create_booking` 用 `room_id`、`host_id`、`starts_at`、`ends_at` 和 `subject` 创建预约，可选 `member_ids` 和 `content`。
- `reschedule_booking` 按 `booking_id` 编辑时间、房间、主题、内容和其他与会人员。只传要修改的字段。用 `clear_content=true` 清空内容，用 `member_ids=[]` 清空其他与会人员。
- `cancel_booking` 按 `booking_id` 取消预约。创建人或主持人重复取消会得到同一条已取消记录。

通过 `tools/list` 可查看每个工具的名称、说明、参数说明和输出 schema。时间参数使用带时区的整秒日期时间，例如 `2026-11-16T14:00:00+08:00`。列表结果放在 `structuredContent.items`，单条预约直接放在 `structuredContent`。预约结果包含房间名称及地址、主持人和预订人姓名。业务失败时 `isError` 为 `true`，文本和结构化结果都带错误码和提示，例如 `ROOM_TIME_CONFLICT` 表示时段冲突，`API_UNAVAILABLE` 表示 API 连接失败。写入超时后先查询预约确认结果。

## 联调测试

服务运行时，用 `python -X utf8 mcp_test.py` 检查 HTTP 工具发现和 API 查询。修改过 MCP 端口时，先设置 `MEETING_MCP_URL`。

下面的回归测试使用回环端口、临时 SQLite 和 stdio 子进程。

```powershell
python -B -m unittest api.test_bookings mcp_server.test_stdio -v
```
