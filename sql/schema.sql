-- ============================================================
-- 医院门诊号源预约与候补系统 · 数据库结构
-- ------------------------------------------------------------
-- 本文件是全项目唯一的建表来源。
-- 修改表结构必须先改本文件，再改代码；禁止在别处写 CREATE TABLE。
--
-- 关于外键：
--   SQLite 的外键约束是「每个连接」级别的开关，默认关闭。
--   真正生效要靠 app/db.py 在每次建连接时执行 PRAGMA foreign_keys = ON。
--   这里刻意不写它，避免给人「执行了脚本就等于开了外键」的错觉。
--
-- 执行顺序：本文件只建空表，数据由 sql/seed.sql 灌入。
-- ============================================================

-- ------------------------------------------------------------
-- 1. users：模拟身份
--    题目要求模拟注册登录，不对接公安身份核验，因此只存三要素。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    id_card_last4 TEXT    NOT NULL,
    phone         TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active',
    no_show_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),

    -- 身份证后 4 位只限长度，不强制为数字：
    -- 真实身份证最后一位可能是字母 X，强制数字会误伤真实数据
    CONSTRAINT chk_id_card_len CHECK (length(id_card_last4) = 4),
    CONSTRAINT chk_phone_len  CHECK (length(phone) = 11),
    CONSTRAINT chk_user_status CHECK (status IN ('active', 'blacklisted')),

    -- 三要素一致即视为同一个人，这是「模拟实名」的唯一性依据
    UNIQUE (name, id_card_last4, phone)
);

-- ------------------------------------------------------------
-- 2. doctors：医生固定信息
--    单独成表是为了避免在多条排班里重复抄写职称、科室等不变信息。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doctors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    title      TEXT NOT NULL,
    department TEXT NOT NULL,
    intro      TEXT NOT NULL DEFAULT '',
    specialty  TEXT NOT NULL DEFAULT '',

    CONSTRAINT chk_title CHECK (title IN ('主任医师', '副主任医师', '主治医师'))
);

-- ------------------------------------------------------------
-- 3. schedules：排班，即一个号源池
--    一行 = 某医生某天某个时段放出的号源。
--
--    注意：本表刻意不存 remaining（剩余号）与 status（号源状态）。
--    它们都是派生值，一律由 app/services/slot_status.py 现算。
--    库里只保留 is_closed —— 因为「医生停诊」是人为设定，不是算出来的。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id    INTEGER NOT NULL,
    work_date    TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    total_slots  INTEGER NOT NULL,
    booked_count INTEGER NOT NULL DEFAULT 0,
    is_closed    INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT fk_schedules_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctors (id),
    CONSTRAINT chk_period
        CHECK (period IN ('morning', 'afternoon', 'evening')),
    CONSTRAINT chk_work_date
        CHECK (work_date LIKE '____-__-__'),
    CONSTRAINT chk_total_slots
        CHECK (total_slots > 0),
    CONSTRAINT chk_booked_not_negative
        CHECK (booked_count >= 0),
    CONSTRAINT chk_is_closed
        CHECK (is_closed IN (0, 1)),

    -- 第四层防护：即使条件 UPDATE 写错导致超卖，这里也会让整条语句失败回滚
    CONSTRAINT chk_booked_not_exceed
        CHECK (booked_count <= total_slots),

    -- 同一医生同一天同一时段只能有一条排班
    UNIQUE (doctor_id, work_date, period)
);

-- ------------------------------------------------------------
-- 4. appointments：正式预约
--    一行 = 一个持有就诊序号的人。
--
--    取消采用软删除：status 改为 'cancelled'，记录保留。
--    这样加分项（爽约统计、违规拉黑、退号冷却期）才有历史可查。
--
--    R5 新增 status = 'pending'：候补者被递补选中、但还没确认的那 15 分钟。
--    它**照样占着号**（否则这 15 分钟里号会被别人抢走，"给他留着"就成了空话），
--    所以它必须出现在下面两个唯一索引的 WHERE 里，也必须出现在
--    app/services/guards.py 的 ACTIVE_STATUS_CLAUSE 里。
--    三处必须一起改，漏掉索引最隐蔽 —— 应用层还会拦着，看起来一切正常，
--    只有"绕过应用层直接写库"这条路径会悄悄失效。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS appointments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    schedule_id  INTEGER NOT NULL,
    seq_no       INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'booked',
    source       TEXT    NOT NULL DEFAULT 'direct',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    cancelled_at TEXT,

    -- R5 加分项 2：pending 的确认截止时刻，由 clock.shift_sql() 算出。
    -- 只有 pending 记录有值，其余为 NULL —— 给一列无意义的默认值，
    -- 不如让 NULL 老老实实表达"这条不适用于确认"。
    confirm_deadline TEXT,

    -- R5 加分项 3：签到时刻。
    -- 空着 + 排班日期已过 ⇒ 结算为 no_show（见 services/booking.settle_schedule）
    checked_in_at TEXT,

    CONSTRAINT fk_appointments_user
        FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_appointments_schedule
        FOREIGN KEY (schedule_id) REFERENCES schedules (id),
    CONSTRAINT chk_seq_no
        CHECK (seq_no >= 1),
    CONSTRAINT chk_appointment_status
        CHECK (status IN ('booked', 'pending', 'cancelled',
                          'completed', 'no_show')),
    CONSTRAINT chk_appointment_source
        CHECK (source IN ('direct', 'promoted'))
);

-- 号源互斥索引：同一个就诊序号不可能被两条有效预约同时持有。
-- 加了 WHERE 的部分索引只约束「有效」记录，已取消的历史记录不受限，
-- 因此用户退号后该序号可以被下一个人（通常是候补首位）接手。
--
-- 注意：SQLite 的 CREATE UNIQUE INDEX IF NOT EXISTS 只在"同名索引不存在"时建，
-- 索引**已存在但定义变了**它不会管。所以改了下面的 WHERE 之后，
-- 已有的库必须由 app/db.py 的 _migrate() 删掉重建，否则新状态不生效。
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_slot
    ON appointments (schedule_id, seq_no)
 WHERE status IN ('booked', 'pending', 'completed', 'no_show');

-- 防黄牛索引：同一用户对同一排班不可能有两条有效预约。
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_user_slot
    ON appointments (user_id, schedule_id)
 WHERE status IN ('booked', 'pending', 'completed', 'no_show');

-- R5 加分项：结算扫描要按「状态 + 截止时刻」捞 pending 记录。
-- 没有它，每次结算都要全表扫 appointments；有了它，
-- 扫描的代价与"当前有多少条待确认"成正比，而不是与历史总记录数成正比。
CREATE INDEX IF NOT EXISTS ix_appointments_pending
    ON appointments (status, confirm_deadline);

-- R5 加分项：爽约扫描要找「日期已过但状态仍是 booked」的记录，
-- 这需要按排班去 appointments 里捞，schedule_id 上的普通索引能兜住。
CREATE INDEX IF NOT EXISTS ix_appointments_schedule
    ON appointments (schedule_id);

-- ------------------------------------------------------------
-- 5. waitlist：候补队列
--    一行 = 一个排队等退号的人。
--
--    position 是排队位次，与 joined_at 保持一致；
--    有人退号时取 position 最小的那一位转正。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    schedule_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'waiting',
    joined_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at TEXT,

    CONSTRAINT fk_waitlist_user
        FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_waitlist_schedule
        FOREIGN KEY (schedule_id) REFERENCES schedules (id),
    CONSTRAINT chk_position
        CHECK (position >= 1),
    CONSTRAINT chk_waitlist_status
        CHECK (status IN ('waiting', 'promoted', 'cancelled', 'expired'))
);

-- 同一排班里排队位次不可重复
CREATE UNIQUE INDEX IF NOT EXISTS ux_wait_slot_pos
    ON waitlist (schedule_id, position)
 WHERE status = 'waiting';

-- 同一用户不能在同一排班里排两次队
CREATE UNIQUE INDEX IF NOT EXISTS ux_wait_user
    ON waitlist (schedule_id, user_id)
 WHERE status = 'waiting';

-- ------------------------------------------------------------
-- 6. 常用查询路径的普通索引（非唯一，只提速）
-- ------------------------------------------------------------

-- 「我的预约」按用户查
CREATE INDEX IF NOT EXISTS ix_appointments_user
    ON appointments (user_id);

-- 排班列表按日期筛选
CREATE INDEX IF NOT EXISTS ix_schedules_date
    ON schedules (work_date);

-- 候补队列按排班取队首
CREATE INDEX IF NOT EXISTS ix_waitlist_schedule
    ON waitlist (schedule_id);
