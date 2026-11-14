INSERT INTO people (person_id, name, birthday, phone, email, wecom_user_id, job_title, job_level) VALUES
    ('P000001', '林晓', '1991-03-18', '+12025550101', 'lin.xiao@example.com', 'sample_lin_xiao', '项目经理', 'P6'),
    ('P000002', '周宁', '1993-07-22', '+12025550102', 'zhou.ning@example.com', 'sample_zhou_ning', '产品经理', 'P5'),
    ('P000003', '陈宇', '1990-12-09', '+12025550103', 'chen.yu@example.com', NULL, '软件工程师', 'P5'),
    ('P000004', '许佳', '1995-01-16', '+12025550104', 'xu.jia@example.com', NULL, '交互设计师', 'P4'),
    ('P000005', '高远', '1988-08-30', '+12025550105', 'gao.yuan@example.com', NULL, '采购主管', 'M1'),
    ('P000006', '沈悦', '1996-11-03', '+12025550106', 'shen.yue@example.com', NULL, '培训专员', 'P4'),
    ('P000007', '罗晨', '1992-05-25', NULL, 'luo.chen@example.com', NULL, '运维工程师', 'P5'),
    ('P000008', '方敏', NULL, NULL, NULL, NULL, '行政专员', 'P4');

INSERT INTO rooms (room_id, name, address, capacity, is_active) VALUES
    ('R000001', '三楼大会议室', '示例园区 A 座 3 层 301', 12, 1),
    ('R000002', '二楼讨论室', '示例园区 A 座 2 层 205', 6, 1),
    ('R000003', '四楼会议室', '示例园区 A 座 4 层 402', 8, 0);

-- 所有偏移量均为秒，基准时间为本次初始化时的 UTC Unix 时间戳。
WITH reference_time(now) AS (VALUES (unixepoch('now'))),
sample(booking_id, room_id, host_id, created_by, starts, ends, subject, content, canceled) AS (
    VALUES
        ('A000001', 'R000001', 'P000001', 'P000001', 7200, 10800, '项目周会', '同步进度与本周安排', NULL),
        ('A000002', 'R000001', 'P000002', 'P000001', 10800, 14400, '需求评审', '评审会议室查询需求', NULL),
        ('A000003', 'R000002', 'P000003', 'P000003', -1800, 1800, '迭代站会', '同步今天的任务与阻塞', NULL),
        ('A000004', 'R000003', 'P000004', 'P000004', -93600, -90000, '设计回顾', '回顾上一版设计方案', NULL),
        ('A000005', 'R000002', 'P000005', 'P000005', 86400, 90000, '供应商沟通', '讨论交付安排', -3600),
        ('A000006', 'R000002', 'P000006', 'P000002', 86400, 90000, '培训筹备', '确认讲师与培训材料', NULL),
        ('A000007', 'R000001', 'P000007', 'P000007', -7200, -3600, '上线复盘', NULL, NULL),
        ('A000008', 'R000002', 'P000008', 'P000008', 172800, 174600, '行政例会', '同步下周安排', NULL)
)
INSERT INTO bookings (booking_id, room_id, host_id, created_by, starts_at, ends_at, subject, content, canceled_at)
SELECT booking_id, room_id, host_id, created_by, now + starts, now + ends, subject, content, now + canceled
FROM sample CROSS JOIN reference_time
ORDER BY booking_id;

INSERT INTO booking_members (booking_id, person_id) VALUES
    ('A000001', 'P000002'), ('A000001', 'P000003'), ('A000001', 'P000004'),
    ('A000002', 'P000001'), ('A000002', 'P000005'),
    ('A000003', 'P000004'), ('A000003', 'P000006'),
    ('A000004', 'P000007'),
    ('A000005', 'P000002'),
    ('A000006', 'P000002'), ('A000006', 'P000008'),
    ('A000007', 'P000001'),
    ('A000008', 'P000006');
