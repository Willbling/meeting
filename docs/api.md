# 会议室预订 API

按[根目录启动说明](../README.md#本地启动)安装依赖。从项目根目录初始化 SQLite 并单独启动 API。

```powershell
conda activate py313
python data/sqlite/init_db.py
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

打开 [交互式接口文档](http://127.0.0.1:8000/docs) 调试。默认连接 `data/sqlite/meeting.db`，可以用 `MEETING_DB_PATH` 指定另一个已初始化的 SQLite 文件。

## 接口

- `GET /readyz` 检查数据库文件和关键表、视图。可用时返回 `{"status":"ready"}`，不可用时返回 HTTP `503` 和 `DATABASE_UNAVAILABLE`。
- `GET /people` 按 `name` 匹配姓名片段，返回姓名、人物 ID、职务和职级。职务或职级未登记时为 `null`。同名时返回多个候选。
- `GET /rooms` 查询启用的会议室，可按 `name`、`min_capacity` 筛选。一起传入 `starts_at` 和 `ends_at` 时，只返回整个时段内空闲的会议室。
- `GET /rooms?mode=intervals` 返回指定范围内至少有一段空闲的会议室，包含 `free_intervals` 和 `busy_intervals`。占用只提供起止时间。可以用 `min_duration_minutes` 要求至少一段连续空闲达到指定分钟数。
- `GET /bookings` 查询当前操作者创建、主持或参加的预约。支持 `room_id`、`status`、`starts_at` 和 `ends_at` 筛选，时间筛选匹配与查询范围相交的预约。状态可选「等待」「会议中」「取消」「结束」。
- `GET /bookings/{booking_id}` 查询预约详情，返回主持人、成员和会议内容。
- `POST /bookings` 创建预约。请求包含 `room_id`、`host_id`、`starts_at`、`ends_at` 和 `subject`，可选 `content` 和 `member_ids`。申请 ID 由系统生成，创建人取自操作者身份，可以与主持人不同。
- `PATCH /bookings/{booking_id}` 可一次修改 `starts_at`、`ends_at`、`room_id`、`subject`、`content` 和 `member_ids`。省略沿用原值；`content: null` 清空内容，`member_ids: []` 清空其他与会人员。成员名单替换时不包含主持人。修改整笔提交，冲突或验证失败保留原预约。
- `POST /bookings/{booking_id}/cancel` 取消预约并释放时段。创建人或主持人重复取消时，返回同一条已取消的记录。

所有预约响应均含 `room_id`、`room_name`、`room_address`、`host_id`、`host_name`、`created_by` 和 `creator_name`。详情和写入响应还提供会议内容和结构化 `attendees` 名单。

三个列表接口支持 `limit` 和 `offset`，默认每次返回最多 50 条。所有 `/bookings` 接口都要求在请求头传 `X-Person-ID`，例如 `P000001`。API 信任本机调用方提供的身份。企业微信单聊的身份来自发送者的本地账号绑定，经 MCP 转发给 API。

时间输入使用带时区的整秒 ISO 8601 日期时间。响应使用北京时间，示例为 `2026-11-13T14:00:00+08:00`。创建和改期的开始时间必须晚于当前时间。创建人和主持人可以改期或取消等待中的预约，成员可以查询自己参加的会议。容量包含主持人，启用状态和容量会在创建、改期时检查。

同一会议室允许相邻时段，时段重叠返回 HTTP `409` 和 `ROOM_TIME_CONFLICT`。业务错误的 JSON 包含 `code` 和 `message`，请求参数校验失败时还会返回 `details`。预约及成员在同一事务内写入。

## 本地验收

测试会初始化临时 SQLite 文件，覆盖代订、主持人改期、创建人取消、原时段重新预订和并发冲突。

```powershell
python -B -m unittest api.test_bookings -v
```
