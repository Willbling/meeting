提取会议室预订条件。当前北京时间 {now}。只填原话明确给出的值，未知留空。先提取主办人和其他与会人员。「我和陈宇的」表示 host=我、members=[陈宇]。这句话只描述人员，subject 留空。只有原话明确给出会议议题或标题才填 subject。把今天、明天等原词填入 date_hint，明确时长换算成 duration_minutes。只有具体起止时刻才填带 +08:00 的 ISO 8601 时间。下午是时段提示，并未给出具体几点。

一句话给出多个条件时全部提取。遇到「预订明天下午三点到到五点的二楼会议室，我和甲、乙、丙」，把 date_hint 填「明天」，start_clock 填「15:00:00」，end_clock 填「17:00:00」，room_name 填「二楼」，host 填「我」，members 分别填甲、乙、丙，subject 留空。起止时刻可优先用 start_clock 和 end_clock，程序会与日期合并。
