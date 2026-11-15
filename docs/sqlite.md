# SQLite 会议室样例数据库

## 脚本结构

- `init_db.py` 是建库入口。它开启外键检查，在一个事务中依次执行两个 SQL 文件，并将结构版本标记为 `2`。
- `schema.sql` 定义 4 个表、3 个视图、1 个显式索引和 5 个触发器。
- `seed.sql` 插入人员、会议室、预约和成员样例。样例预约的起止时间以建库时刻为基准。
- `meeting.db` 是默认生成的本地文件，包含本机的实际账号绑定，由 Git 忽略。

## 创建和使用数据库

按[首次创建环境](../README.md#首次创建环境)创建虚拟环境，然后在项目根目录打开 PowerShell。建库使用 Python 标准库 `sqlite3`，无需另外安装 SQLite 命令行工具。

```powershell
uv run python data/sqlite/init_db.py
```

默认输出到 `data/sqlite/meeting.db`。如果该库已经初始化，脚本会提示「已初始化」并跳过。需要单独建一份样例库时，用 `--database` 指定一个新文件名。目标文件的父目录须已存在。

```powershell
uv run python data/sqlite/init_db.py --database data/sqlite/meeting_fresh.db
```

可以在 SQLite 客户端打开生成的文件，使用下面的样例 SQL 查看数据。FastAPI 和企业微信程序默认读取 `meeting.db`。使用其他文件时，在两个程序各自的启动终端设置相同的绝对路径，例如 `$env:MEETING_DB_PATH = (Resolve-Path data/sqlite/meeting_fresh.db).Path`。

新库的企业微信 UserID 是样例值。需要用实际账号联调时，按[企业微信使用说明](wecom.md)重新绑定。使用 SQLite 客户端手动写入数据前，应为当前连接执行 `PRAGMA foreign_keys = ON;`。

## 表和字段

所有表均为 SQLite `STRICT` 表。「可空」字段省略时默认为 `NULL`。时刻字段统一存储为 UTC Unix 秒级时间戳。

### `people` 人员

- `person_id` · `TEXT` 主键，必填，格式为 `P` 加 6 位数字。
- `name` · `TEXT`，必填，去掉首尾空格后须非空，允许同名。
- `birthday` · `TEXT`，可空，必须是有效的 `YYYY-MM-DD` 日期。
- `phone` · `TEXT`，可空，保留区号和前导零。
- `email` · `TEXT`，可空。
- `wecom_user_id` · `TEXT`，可空，非空值须唯一，去掉首尾空格后须非空。
- `job_title` · `TEXT`，可空，填写时须非空，例如「项目经理」。
- `job_level` · `TEXT`，可空，填写时须非空，例如 `P6` 或 `M1`。

### `rooms` 会议室

- `room_id` · `TEXT` 主键，必填，格式为 `R` 加 6 位数字。
- `name` · `TEXT`，必填，去掉首尾空格后须非空。
- `address` · `TEXT`，必填，去掉首尾空格后须非空。
- `capacity` · `INTEGER`，必填，须大于 0，容量包含主持人。
- `is_active` · `INTEGER`，必填，默认 `1`。取值 `1` 表示启用，`0` 表示停用。

### `bookings` 预约

- `booking_id` · `TEXT` 主键，必填，格式为 `A` 加 6 位数字，由 API 创建预约时分配。
- `room_id` · `TEXT`，必填，外键引用 `rooms.room_id`。
- `host_id` · `TEXT`，必填，外键引用 `people.person_id`，表示主持人。
- `created_by` · `TEXT`，必填，外键引用 `people.person_id`，表示预订人。代订时可以与主持人不同。
- `starts_at` · `INTEGER`，必填，开始时间。
- `ends_at` · `INTEGER`，必填，结束时间，须晚于开始时间。
- `subject` · `TEXT`，必填，去掉首尾空格后须非空。
- `content` · `TEXT`，可空，会议内容。
- `canceled_at` · `INTEGER`，可空，记录取消时间。空值表示预约尚未取消。

### `booking_members` 其他与会人员

- `booking_id` · `TEXT`，必填，外键引用 `bookings.booking_id`。
- `person_id` · `TEXT`，必填，外键引用 `people.person_id`。

两列组成复合主键，同一个人在一场会议中只能登记一次。主持人记录在 `bookings.host_id`。

## 视图

- `booking_status` 每场会议一行，显示申请编号、主题和内容、会议室名、主持人、预订人、其他与会人员和状态。其他与会人员按人物编号排列，姓名之间用「、」分隔，无其他与会人员时为空字符串。开始、结束和取消时间统一为北京时间，格式为 `YYYY-MM-DD HH:MM:SS`。
- `booking_attendees` 每位与会人员一行，给出申请编号、主题、姓名、角色和人物编号。角色为「主持人」或「成员」。
- `booking_state` 提供 FastAPI 使用的原始预约字段和实时状态。已取消为「取消」，未开始为「等待」，开始后到结束前为「会议中」，到达结束时间为「结束」。

## 索引和触发器

部分索引 `bookings_active_room_time` 按会议室、开始时间和结束时间索引未取消的预约，用于查询空闲会议室和检查重叠。时段采用左闭右开规则，因此相邻时段可以预订。取消预约后，原时段释放。

- `bookings_check_overlap_insert` 在插入预约前检查。同一会议室的两条未取消预约时段重叠时，拒绝插入并返回 `room already booked for this time`。
- `bookings_check_overlap_update` 在修改会议室、起止时间或取消时间前检查。比较时排除当前预约，冲突时返回同样的错误。
- `booking_members_check_host_insert` 在新增成员前检查。成员是该会议主持人时，返回 `host is already an attendee`。
- `booking_members_check_host_update` 在修改成员的申请编号或人物编号前检查，防止更新后的成员与主持人重复，返回同样的错误。
- `bookings_check_host_update` 在修改主持人前检查。拟任主持人已是该会议的成员时，返回 `remove member before assigning as host`。先从成员名单移除该人员，再设置为主持人。

触发器通过 `RAISE(ABORT, ...)` 撤销违反规则的 SQL 语句。API 还会在写入前检查会议室是否启用、人数是否超出容量以及操作人的权限。

## 样例查询

```sql
SELECT name, job_title, job_level
FROM people
ORDER BY person_id;

SELECT booking_id, subject, room_name, starts_at, ends_at,
       host_name, creator_name, member_names, status
FROM booking_status
ORDER BY starts_at, booking_id;

SELECT subject, person_name, role
FROM booking_attendees
WHERE booking_id = 'A000002'
ORDER BY role = '主持人' DESC, person_id;
```

新建库含 8 人、3 间会议室、8 条预约和 13 条成员记录。`A000001` 与 `A000002` 时段相邻，`A000002` 和 `A000006` 是代订。`A000005` 取消后，`A000006` 占用同一时段。建库时有 4 条等待、1 条会议中、1 条取消和 2 条结束，状态会随时间推进。

下面的输入与 `A000001` 重叠，数据库会拒绝写入并返回 `room already booked for this time`。

```sql
INSERT INTO bookings (booking_id, room_id, host_id, created_by, starts_at, ends_at, subject)
SELECT 'A999999', room_id, host_id, created_by, starts_at + 900, ends_at, '冲突校验'
FROM bookings
WHERE booking_id = 'A000001';
```
