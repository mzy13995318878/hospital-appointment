# 数据模型与关键决策

> 这是全项目唯一的表结构契约。R1 建表必须按此文件执行，R2–R5 只能读不能改结构（确需改，先改本文件再改代码）。

---

## 1. 表结构总览

```
users ──┐
        ├── appointments ──┐
        └── waitlist ──────┤
                           ├── schedules ── doctors
```

五张表：`users` / `doctors` / `schedules` / `appointments` / `waitlist`。

---

## 2. 字段定义

### 2.1 `users` — 模拟身份

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| name | TEXT | NOT NULL | 姓名 |
| id_card_last4 | TEXT | NOT NULL, CHECK(length=4) | 身份证后 4 位，仅用于组合唯一，不做真实核验 |
| phone | TEXT | NOT NULL | 手机号 |
| status | TEXT | NOT NULL DEFAULT 'active' | `active` / `blacklisted`（加分项用） |
| no_show_count | INTEGER | NOT NULL DEFAULT 0 | 累计爽约次数（加分项用） |
| created_at | TEXT | NOT NULL DEFAULT (datetime('now','localtime')) | |

**唯一约束**：`UNIQUE(name, id_card_last4, phone)`
→ 三要素一致即认为是同一个人，登录时命中则直接返回该用户，否则自动注册。

---

### 2.2 `doctors` — 医生

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT NOT NULL | 姓名 |
| title | TEXT NOT NULL | 职称：主任医师 / 副主任医师 / 主治医师 |
| department | TEXT NOT NULL | 科室：口腔科 / 皮肤科 / 心内科 |
| intro | TEXT | 简介 |
| specialty | TEXT | 擅长方向 |

---

### 2.3 `schedules` — 排班与号源池

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER PK | | |
| doctor_id | INTEGER | NOT NULL, FK→doctors(id) | |
| work_date | TEXT | NOT NULL | `YYYY-MM-DD` |
| period | TEXT | NOT NULL, CHECK IN ('morning','afternoon','evening') | 时段 |
| total_slots | INTEGER | NOT NULL CHECK(>0) | 号源总数 |
| booked_count | INTEGER | NOT NULL DEFAULT 0, CHECK(≥0 AND ≤total_slots) | 已占号数，**唯一计数源** |
| is_closed | INTEGER | NOT NULL DEFAULT 0 | 1 = 医生停诊 |

**唯一约束**：`UNIQUE(doctor_id, work_date, period)` —— 同医生同一天同时段只能有一条排班。

**注意**：这里**不存** `remaining` 字段，也不存 `status` 字段。原因见决策 1、2。

---

### 2.4 `appointments` — 正式预约

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER PK | | |
| user_id | INTEGER | NOT NULL, FK→users | |
| schedule_id | INTEGER | NOT NULL, FK→schedules | |
| seq_no | INTEGER | NOT NULL CHECK(≥1) | 就诊序号，1..total_slots |
| status | TEXT | NOT NULL DEFAULT 'booked' | `booked` / `pending` / `cancelled` / `completed` / `no_show` |
| source | TEXT | NOT NULL DEFAULT 'direct' | `direct` 直接约 / `promoted` 候补转正 |
| created_at | TEXT | NOT NULL | |
| cancelled_at | TEXT | | |
| confirm_deadline | TEXT | | **加分项**：`pending` 的确认截止时刻，逾期作废并顺延下一位 |
| checked_in_at | TEXT | | **加分项**：签到时刻；空且排班日期已过 → 结算为 `no_show` |

**`pending` 是 R5 新增的状态，含义是「已把号留给他，等他确认」。**

五个取值的分工：

| 状态 | 是否占号 | 何时出现 |
|---|---|---|
| `pending` | **是** | 候补者被递补选中，等待其确认（15 分钟内） |
| `booked` | 是 | 直接预约成功，或 `pending` 已确认 |
| `completed` | 是 | 已签到就诊 |
| `no_show` | 是 | 过期未签到，由系统结算 |
| `cancelled` | 否 | 用户退号，或 `pending` 超时作废 |

> **`pending` 必须算作「占着号」**，否则这 15 分钟里号源会被别人直接抢走，
> 那么"给他留着待确认"就成了空话 —— 他确认时号早没了。
> 代价是：队列在这 15 分钟里被卡住，后面的人要等。
> 这正是真实挂号系统的行为（逐个通知、逐个确认），
> 而卡的时长由 `policy.CONFIRM_TIMEOUT_MINUTES` 明确限定，不会无限期。

**唯一约束**（两个都要，缺一不可）：

```sql
-- 同一个号不可能被两个人同时持有
CREATE UNIQUE INDEX ux_appt_slot ON appointments(schedule_id, seq_no)
  WHERE status IN ('booked','pending','completed','no_show');

-- 同一个人在同一排班下不可能有两条有效预约
CREATE UNIQUE INDEX ux_appt_user_slot ON appointments(user_id, schedule_id)
  WHERE status IN ('booked','pending','completed','no_show');
```

> **加 `pending` 时必须同步改这两个索引**，与 `guards.ACTIVE_STATUS_CLAUSE` 三处一起改。
> 漏掉索引的后果最隐蔽：应用层会拦住重复预约，看起来一切正常，
> 但库级那道兜底悄悄失效了 —— 而它存在的唯一理由就是"应用层被绕过时还能拦住"。
> 兜底失效时系统照样跑，直到有人绕过应用层写库才发现，那时候已经晚了。

第二条使用了 SQLite 的**部分索引**（partial index）：只对"有效"记录去重，已取消的历史记录可以共存多行。这是防黄牛的库级兜底——即使应用层校验被绕开，插入也会失败。

---

### 2.5 `waitlist` — 候补队列

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER PK | | |
| user_id | INTEGER | NOT NULL, FK→users | |
| schedule_id | INTEGER | NOT NULL, FK→schedules | |
| position | INTEGER | NOT NULL | 排队序号，从 1 开始 |
| status | TEXT | NOT NULL DEFAULT 'waiting' | `waiting` / `promoted` / `cancelled` / `expired` |
| joined_at | TEXT | NOT NULL | 入队时间，排序依据 |
| resolved_at | TEXT | | 转正/失效时间 |

**唯一约束**：

```sql
CREATE UNIQUE INDEX ux_wait_slot_pos ON waitlist(schedule_id, position)
  WHERE status = 'waiting';
CREATE UNIQUE INDEX ux_wait_user ON waitlist(schedule_id, user_id)
  WHERE status = 'waiting';
```

排队顺序 = `ORDER BY position ASC`（或 `joined_at ASC, id ASC`，两者一致）。

---

## 3. 六个关键决策（README 里要原样展开）

### 决策 1：剩余号不落库，实时派生

`remaining = total_slots - booked_count`。

**为什么不存**：两个字段同时维护必然出现"剩余号显示 3，实际只有 2 人能约"的不一致。派生值没有不一致的可能。
**代价**：列表页每次都要算，但只是减法，忽略不计。

### 决策 2：号源状态也是派生的，只有"停诊"落库

题目要求四种状态：可约 / 已满 / 候补中 / 已停诊。计算规则：

| 状态 | 条件 |
|---|---|
| 已停诊 | `is_closed = 1` |
| 可约 | `is_closed = 0` 且 `booked_count < total_slots` |
| 候补中 | 已满，且 `waiting` 状态的候补数 > 0 |
| 已满 | 已满，且无人在候补 |

**为什么**：「已满」和「候补中」的差别只在于有没有人排队，它是瞬时事实而非持久状态。如果把它存成字段，就必须在每次有人入队/离队时同步改它，多一处可能忘记的地方。

### 决策 3：就诊序号取「最小可用号」，不用 MAX+1

场景：总数 3，1/2/3 号都已被占；1 号取消 → 空出 1 号 → 新人预约。
若用 `MAX+1` 得到 4，**超出 total_slots 上限**，违反 CHECK 约束。

正确做法：在 1..total 里找最小的未被占用序号。

```python
used = {r["seq_no"] for r in conn.execute(
    "SELECT seq_no FROM appointments WHERE schedule_id=? AND status != 'cancelled'", (sid,))}
seq = next(n for n in range(1, total + 1) if n not in used)
```

事务内已持有写锁，`SELECT` 与 `INSERT` 之间不会被插入，所以不会算重。

### 决策 4：取消 = 软删除

取消时 `status='cancelled'` + `cancelled_at`，记录保留。

**理由**：加分项（爽约统计、违规拉黑、退号冷却期）全部依赖历史轨迹；硬删除等于把这些功能的路堵死。同时部分唯一索引让软删除不影响防重逻辑。

### 决策 5：候补转正「继承」退号者的序号

1 号取消 → 候补第 1 位拿到的就是 1 号，而不是"重新排到队尾"。

**理由**：序号语义是「第几个就诊」，空号本来就该由递补者填补；这样序号在 1..total 内始终连续，不会出现 3、7、12 这种带洞的序列。同时 `source='promoted'` 留下痕迹，便于前端标注"候补转正"。

### 决策 6：防黄牛三条规则分两层实现

| 规则 | 实现层 | 方式 |
|---|---|---|
| 同一用户同一出诊时段不可重复预约 | 库 + 应用 | 部分唯一索引 `ux_appt_user_slot` + 事务内预查 |
| 同一用户同一天同一科室最多 1 个号 | 应用层 | 需跨 `appointments`+`schedules`+`doctors` 三表查询，索引无法表达 |
| 数据持久化，重启不丢 | 存储层 | SQLite 文件落盘 |

第二条的 SQL：

```sql
SELECT COUNT(*) FROM appointments a
JOIN schedules s ON s.id = a.schedule_id
JOIN doctors d ON d.id = s.doctor_id
WHERE a.user_id = ? AND s.work_date = ? AND d.department = ?
  AND a.status IN ('booked','completed','no_show');
```

**边界说明**：该限制只算正式预约，候补不计入。理由——候补尚未占有任何号源，若也限制，患者会因"同科室排了两个医生的候补"而失去机会，与便民目标相反。这个取舍要主动在 README 里写出来。

---

## 4. 加分项的结构改动（R5 已全部落地）

当初预留时的设想与最终做法的对照，以及**为什么改了主意**：

| 功能 | R0 的预留设想 | R5 实际做法 | 为什么改 |
|---|---|---|---|
| 转正确认超时 | `waitlist` 加 `confirm_deadline`，`status` 增 `confirming` | `appointments` 加 `confirm_deadline`，`appointments.status` 增 `pending` | 待确认的人**已经拿到号了**，就该记在 appointments 里。记在 waitlist 上意味着"他还算在排队"，可他实际上已经出队，两条表各记一半才会不一致 |
| 退号冷却期 | 新增 `cooldowns(user_id, schedule_id, until)` 表 | 查 `appointments.cancelled_at` | 冷却期的起点就是"上一次退号的时刻"，`cancelled_at` 已经在记了。再开一张表存同样的信息，是给"两处数据可能对不上"主动创造机会 |
| 就诊签到 | `appointments` 加 `checked_in_at` | 同设想 | 与设想一致 |
| 违规拉黑 | `users.status` 已预留，加 `violations` 表记原因 | `users.status` + `no_show_count`，爽约原因可由 `appointments.status='no_show'` 反查 | 唯一会触发拉黑的原因是爽约，而爽约记录本身就是那张 `violations` 表。为了"将来可能有别的原因"先建一张空表，是拿现在确定要维护的成本去赌一个不确定的将来 |

以上都**不改动已有字段语义**，只做加法。

### 加分项共用的两条基础设施

五个加分项里有四个（转正确认超时、退号冷却期、取消时限、爽约结算）都依赖"现在几点"，
而项目原本的时间来源只有一个：**SQL 的 `date('now','localtime')`**，只到日期粒度（决策见 `PROJECT_STATE.md` 决策 10）。

它们需要的是**时刻**级别，而且测试不能真等 15 分钟。于是 R5 统一了两件事：

1. **`app/services/policy.py` —— 业务参数中心**。所有阈值与时段映射集中在一处
   （确认超时 15 分钟、退号冷却 10 分钟、取消时限 2 小时、爽约 2 次拉黑、
   `morning → 08:00` 这类时段起点）。
   散落在各处的魔法数字迟早会出现"两个地方都写着 15，改了一个忘了另一个"。
2. **`app/services/clock.py` —— 可注入时钟**。业务代码一律取 `clock.now_sql()` /
   `clock.today_sql()`，不再手写 `datetime('now','localtime')`；
   测试里 `clock.freeze(...)` 就能把整个系统的"现在"拨到任意时刻。

不先定这两件事，每个加分项都会各自造一套时间来源，随后又得回来统一 ——
与"状态算两遍"是同一类欠账，只是它更难被发现，因为两份时间来源在绝大多数时候给出的是同一个答案。
