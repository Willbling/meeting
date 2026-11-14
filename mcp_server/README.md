# 会议室 MCP 服务

通过 `stdio` 向 MCP 客户端提供会议室工具。服务把请求转发给项目中的 FastAPI，由 API 执行权限、容量和时段冲突检查。

## 本地运行

在项目根目录使用 Conda `py313` 环境安装依赖并启动 API。

```powershell
conda activate py313
python -m pip install -r api/requirements.txt -r mcp_server/requirements.txt
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

保持 API 运行。在支持 stdio 的 MCP 客户端中，把项目根目录设为工作目录，并配置以下启动项。客户端会启动并管理 MCP 子进程。

- 命令为 `py313` 环境的 `python.exe` 绝对路径。可用 `python -c "import sys; print(sys.executable)"` 查看。
- 参数为 `-m`、`mcp_server.server`。
- 环境变量 `MEETING_PERSON_ID` 为样例人物 ID，例如 `P000001`。
- 环境变量 `MEETING_API_URL` 默认为 `http://127.0.0.1:8000`。

每个 MCP 进程使用启动时指定的本地测试身份。工具参数中的 `host_id` 可以指定同事，支持代订。接入企业微信时，由应用使用已验证的 `UserID` 确定操作者。

## 工具

- `find_people` 按 `name` 查人，返回姓名和 `person_id`。同名时返回多个候选。
- `find_available_rooms` 按 `starts_at`、`ends_at` 和可选 `min_capacity`、`name` 查空闲会议室。
- `list_my_bookings` 查操作者创建、主持或参加的预约，可按 `status`、`room_id` 和相交时段过滤。
- `get_booking` 按 `booking_id` 查预约详情和与会名单。
- `create_booking` 用 `room_id`、`host_id`、`starts_at`、`ends_at` 和 `subject` 创建预约，可选 `member_ids` 和 `content`。
- `reschedule_booking` 按 `booking_id` 修改 `starts_at`、`ends_at` 或 `room_id`，传入要改的字段。
- `cancel_booking` 按 `booking_id` 取消预约。创建人或主持人重复取消会得到同一条已取消记录。

时间参数使用带时区的整秒日期时间，例如 `2026-11-16T14:00:00+08:00`。列表结果放在 `structuredContent.items`，单条预约直接放在 `structuredContent`。业务失败时 `isError` 为 `true`，文本和结构化结果都带 API 的错误码和提示。例如 `ROOM_TIME_CONFLICT` 表示时段冲突。写入超时后先查询预约确认结果。

## 联调测试

从项目根目录运行。测试会使用回环端口、临时 SQLite 和真正的 stdio 子进程。

```powershell
python -B -m unittest api.test_bookings mcp_server.test_stdio -v
```
