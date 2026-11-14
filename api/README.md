# 会议室预订 API

在项目根目录使用 Conda 的 `py313` 环境启动。

```powershell
conda activate py313
python -m pip install -r api/requirements.txt
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

打开 [交互式接口文档](http://127.0.0.1:8000/docs) 调试。默认连接 `data/sqlite/meeting.db`，可以用 `MEETING_DB_PATH` 指定另一个已初始化的 SQLite 文件。

## 接口

- `GET /people` 按 `name` 模糊查询，只返回姓名和人物 ID。同名时返回多个候选。
- `GET /rooms` 查询启用的会议室，可按 `name`、`min_capacity` 筛选。一起传入 `starts_at` 和 `ends_at` 时，只返回整个时段内空闲的会议室。
- `GET /bookings` 查询当前操作者创建、主持或参加的预约。支持 `room_id`、`status`、`starts_at` 和 `ends_at` 筛选，时间筛选匹配与查询范围相交的预约。状态可选「等待」「会议中」「取消」「结束」。
- `GET /bookings/{booking_id}` 查询预约详情，返回主持人、成员和会议内容。
- `POST /bookings` 创建预约。请求包含 `room_id`、`host_id`、`starts_at`、`ends_at` 和 `subject`，可选 `content` 和 `member_ids`。申请 ID 由系统生成，创建人取自操作者身份，可以与主持人不同。
- `PATCH /bookings/{booking_id}` 修改 `starts_at`、`ends_at` 或 `room_id`。只传需要修改的字段。
- `POST /bookings/{booking_id}/cancel` 取消预约并释放时段。创建人或主持人重复取消时，返回同一条已取消的记录。

三个列表接口支持 `limit` 和 `offset`，默认每次返回最多 50 条。所有 `/bookings` 接口都要求在请求头传 `X-Person-ID`，例如 `P000001`。该请求头可由调用方填写，仅供本机测试。接入企业微信时，应从已验证的用户身份获取操作者。

时间输入使用带时区的整秒 ISO 8601 日期时间。响应使用北京时间，示例为 `2026-11-13T14:00:00+08:00`。创建和改期的开始时间必须晚于当前时间。创建人和主持人可以改期或取消等待中的预约，成员可以查询自己参加的会议。容量包含主持人，启用状态和容量会在创建、改期时检查。

同一会议室允许相邻时段，时段重叠返回 HTTP `409` 和 `ROOM_TIME_CONFLICT`。业务错误的 JSON 包含 `code` 和 `message`，请求参数校验失败时还会返回 `details`。预约及成员在同一事务内写入。

## 本地验收

测试会初始化临时 SQLite 文件，覆盖代订、主持人改期、创建人取消、原时段重新预订和并发冲突。

```powershell
python -m pip install httpx
python -B -m unittest api.test_bookings -v
```
