# SQLite 会议室样例数据库

`meeting.db` 已包含样例数据。`schema.sql` 定义表、视图和约束，`seed.sql` 提供样例记录。在项目根目录使用 Conda 的 `py313` 环境运行。

```powershell
conda activate py313
python data/sqlite/init_db.py
```

初始化在一个事务中完成。对已初始化的数据库重复执行会保留已有数据。如果需要一份以当前时间为基准的样例，用新文件名初始化。

```powershell
python data/sqlite/init_db.py --database data/sqlite/meeting_fresh.db
```

## 数据结构

- `people` 保存姓名、生日、手机、邮箱和可选且唯一的企业微信 `UserID`。人物 ID 用 `re.fullmatch(r"P[0-9]{6}", value)` 校验，数据库也校验该格式。手机号和邮箱均为虚构示例。
- `rooms` 保存会议室名称、地址、容量和启用标记。会议室 ID 格式为 `R` 加 6 位数字。
- `bookings` 保存预约，申请 ID 格式为 `A` 加 6 位数字。`host_id` 是主持人，`created_by` 是发起预订的员工，两者可以不同。开始、结束和取消时间均为 UTC Unix 秒级时间戳，聊天展示时转换为 `Asia/Shanghai`。
- `booking_members` 保存成员，申请 ID 与人物 ID 的组合唯一。查 `booking_attendees` 可以一次取得主持人、成员及其角色。
- `booking_status` 按查询时刻计算状态。已取消显示「取消」，未开始的已预约会议显示「等待」，从开始到结束前显示「会议中」，到达结束时间后显示「结束」。

后续 API 打开每个 SQLite 连接时，应先执行 `PRAGMA foreign_keys = ON`。数据库触发器在写入时检查同一会议室的时段重叠，相邻时段可以预订，已取消的预约释放原时段。

## 样例查询

```sql
SELECT booking_id, room_id, subject, host_id, created_by, status
FROM booking_status
ORDER BY booking_id;

SELECT a.role, p.person_id, p.name
FROM booking_attendees AS a
JOIN people AS p USING (person_id)
WHERE a.booking_id = 'A000002'
ORDER BY a.role = '主持人' DESC, p.person_id;
```

数据库含 8 人、3 间会议室、8 条预约和 13 条成员记录。`A000001` 与 `A000002` 时段相邻，`A000002` 和 `A000006` 是代订。`A000005` 取消后，`A000006` 占用同一时段。初始化时有 4 条等待、1 条会议中、1 条取消和 2 条结束，状态会随时间推进。

下面的输入与 `A000001` 重叠，数据库会拒绝写入并返回 `room already booked for this time`。

```sql
INSERT INTO bookings (booking_id, room_id, host_id, created_by, starts_at, ends_at, subject)
SELECT 'A999999', room_id, host_id, created_by, starts_at + 900, ends_at, '冲突校验'
FROM bookings
WHERE booking_id = 'A000001';
```
