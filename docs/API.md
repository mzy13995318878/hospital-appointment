# 接口契约

> R2–R5 都按此文件实现。前端 `static/js/api.js` 与后端路由必须与此表一一对应，不得擅自增删字段。
>
> 下文示例中的具体日期（如 `2026-11-02`）只是格式示意，不是真实数据。
> 库里的排班日期由 `sql/seed.sql` 用 `date('now','localtime','+N day')` 动态生成，每次重建都会变。

---

## 1. 鉴权（简化）

不做 JWT、不做密码、不做短信。登录成功返回 `user_id`，前端存 `localStorage`，之后每个请求带：

```
X-User-Id: 3
```

后端用一个依赖 `current_user()` 解析该请求头。缺失或无效 → `401`。
**这是刻意的简化，README 里主动说明：题目只要求"模拟注册登录"。**

---

## 2. 统一响应格式

成功：

```json
{ "ok": true, "data": { ... } }
```

失败：

```json
{ "ok": false, "error": { "code": "SLOT_FULL", "message": "该时段号源已满" } }
```

HTTP 状态码与业务码分工：状态码表达"哪一类问题"，`code` 表达"具体是哪一条规则"。

> **这个形状是唯一的，没有第二种。** 框架自己抛出的请求体验证错误（FastAPI 默认返回 `422` +
> `{"detail": [{"type": "int_parsing", ...}]}`）也会被处理器改成上面这个形状，降级为 `400 INVALID_PARAM`。
> 理由：前端 `api.js` 只需要写一套错误处理 —— 判断 `ok` 的真假，取 `data` 或取 `error`。
> 如果字段类型错误走一套结构、业务规则错误走另一套，前端就得准备两套解析，
> 而"对外只有一种错误形状"正是契约存在的意义。实测过：不统一时 `schedule_id="abc"`
> 拿到的是 `detail`，前端连错误码都取不到。

---

## 3. 业务错误码

| code | HTTP | 触发场景 | 前端表现 |
|---|---|---|---|
| `UNAUTHORIZED` | 401 | 未登录 / user_id 不存在 | 跳转登录页 |
| `INVALID_IDENTITY` | 400 | 身份证后 4 位长度不为 4，或手机号长度不为 11 | 表单红字提示 |
| `INVALID_PARAM` | 400 | 枚举型 query 参数取值非法（如 `status=opne`） | 前端控制台报错 |
| `SCHEDULE_NOT_FOUND` | 404 | 排班不存在 | 提示并返回列表 |
| `DOCTOR_NOT_FOUND` | 404 | 医生不存在 | 提示并返回列表 |
| `SCHEDULE_CLOSED` | 409 | 医生停诊 | 按钮置灰 + 提示 |
| `SCHEDULE_EXPIRED` | 409 | 该排班日期已过去（不能约昨天的号） | 按钮置灰 + 提示 |
| `ALREADY_BOOKED_THIS_SLOT` | 409 | 同一时段已有有效预约 | 提示"你已约过该时段" |
| `DAILY_DEPT_LIMIT` | 409 | 同天同科室已有 1 个号 | 提示"同一天同一科室限约 1 个号" |
| `ALREADY_WAITING` | 409 | 已在候补队列中 | 提示"你已在候补第 N 位" |
| `APPOINTMENT_NOT_FOUND` | 404 | 预约 id 不存在或不属于当前用户 | 提示 |
| `WAITLIST_NOT_FOUND` | 404 | 候补 id 不存在或不属于当前用户 | 提示 |
| `ALREADY_CANCELLED` | 409 | 重复取消 | 提示并刷新 |
| `WAITLIST_ALREADY_LEFT` | 409 | 该候补已放弃 / 已转正 | 提示并刷新 |
| `CANCEL_WINDOW_CLOSED` | 409 | 加分项：临近开诊不可取消 | 提示 |
| `COOLDOWN_ACTIVE` | 409 | 加分项：刚退号，冷却期内不可再约同一排班 | 提示"请 10 分钟后再试" |
| `CONFIRM_TIMEOUT` | 409 | 加分项：候补转正后超时未确认，号已顺延 | 提示并刷新 |
| `NOT_PENDING` | 409 | 加分项：该预约不处于待确认状态 | 提示并刷新 |
| `CHECKIN_NOT_OPEN` | 409 | 加分项：还没到签到时间（就诊当天才开放） | 提示 |
| `CHECKIN_WINDOW_CLOSED` | 409 | 加分项：排班日期已过，不能再签到 | 提示 |
| `ALREADY_CHECKED_IN` | 409 | 加分项：已签到，无需重复操作 | 提示 |
| `USER_BLACKLISTED` | 403 | 加分项：被拉黑（爽约累计达 2 次自动触发） | 提示联系门诊办 |

> **注意**：`SLOT_FULL` **不是错误**。有余号就约，没余号就入候补，两者都是成功业务流，返回 `200`。只有"已满且无法排队"才异常。

---

## 4. 接口清单

### 4.1 身份

#### `POST /api/auth/login`

请求：

```json
{ "name": "张三", "id_card_last4": "1234", "phone": "13800000001" }
```

响应 `200`：

```json
{ "ok": true, "data": { "user_id": 3, "name": "张三", "is_new": false } }
```

行为：三要素命中 → 返回已有用户（`is_new=false`）；未命中 → 自动注册（`is_new=true`）。
校验：`id_card_last4` 长度必须为 4，`phone` 长度必须为 11。

**注意只校验长度，不强制为数字**：真实身份证最后一位可能是字母 X，要求纯数字会误伤真实数据。数据库侧同步只加了 `CHECK (length = 4)`，理由一致。

#### `GET /api/auth/me`

响应 `200`：`{ "user_id": 3, "name": "张三", "status": "active", "no_show_count": 0 }`

---

### 4.2 排班与医生

> 本节三个接口都是**公开浏览**，不要求登录 —— 真实挂号系统里，看看今天有哪些号本来就不该先逼用户登录。
> 从 4.3 的预约接口起才需要 `X-User-Id`。

#### `GET /api/departments`

科室下拉框的选项来源。

响应 `200`：

```json
{
  "ok": true,
  "data": [
    { "department": "儿科", "doctor_count": 1 },
    { "department": "口腔科", "doctor_count": 1 }
  ]
}
```

**只返回有排班的科室**。某位医生存在但一条排班都没有时，他所属的科室**不出现**在选项里 ——
否则用户选了它，列表页会是一片空白，看起来像系统坏了。
实现上用 `JOIN schedules` 内连接，天然排除空科室。

#### `GET /api/schedules?department=口腔科&date=2026-11-02&status=open&only_future=1`

四个 query 均可选，用于筛选。

| 参数 | 取值 | 默认值 | 非法值时 |
|---|---|---|---|
| `department` | 科室名 | 全部 | 空列表（自由文本，没匹配就是没数据） |
| `date` | `YYYY-MM-DD` | 全部 | 空列表（同上） |
| `status` | `open` / `full` / `waiting` / `closed` | 全部 | **`400 INVALID_PARAM`** |
| `only_future` | `1` / `0` | **`1`（只给今天及以后）** | `400 INVALID_PARAM` |

#### 为什么默认只返回未来的排班

过去日期的排班在库里并不删除 —— 删了就没有历史了，
将来要做「这位医生上个月出诊几天」这类统计时无从下手。
但它们**不出现在默认的列表里**，理由是：

> 它们约不了。R3+ 后端已经会挡住（`SCHEDULE_EXPIRED`），
> 那么在列表里显示出来，就等于让用户点了才知道不行。
> 把必然失败的操作提前藏掉，比事后给一句错误提示体面得多。

藏着不让人看是另一回事，所以留了 `only_future=0` 这个显式开关：

> **默认隐藏的信息，必须能被人显式取回来。**
> 否则哪天要做历史查询，会发现数据在库里却没接口能拿到，
> 只能再改一次契约 —— 那就把"当时的便利"变成了"以后的欠账"。

「今天」由 SQL 的 `date('now','localtime')` 给出，
与预约校验（R3+ 决策 7）用的是**同一个时钟**。

> 为什么同样是"查不到"，`status` 要报错而另两个返回空列表：
> 科室名叫「火星科」是真的没有，日期写错也是真的没排班，**空列表就是正确答案**；
> 但 `status=opne` 这种拼写错误，返回空列表会让前端以为「今天一个号都没有」，
> **把参数错误伪装成了业务事实**。枚举参数必须校验，否则 typo 会变成空列表事故。

> `status` 是派生值，库里没这一列。筛选必须在 SQL 里重算一遍状态，
> 因此 `slot_status.py` 里刻意把状态表达式抽成常量 `STATUS_SQL_EXPR`，
> 与 `compute_status()` 一一对应。两者由 R5 的一致性测试锁住 —— 详见第 6 节。

响应 `200`：

```json
{
  "ok": true,
  "data": [
    {
      "schedule_id": 1,
      "doctor_id": 1,
      "doctor_name": "李建国",
      "title": "主任医师",
      "department": "口腔科",
      "work_date": "2026-11-02",
      "period": "morning",
      "period_label": "上午",
      "total_slots": 3,
      "booked_count": 3,
      "remaining": 0,
      "waiting_count": 2,
      "status": "waiting",
      "status_label": "候补中"
    }
  ]
}
```

**`status` 取值固定四种**：`open` / `full` / `waiting` / `closed`。
`status_label` 由后端给出，前端不允许自己拼中文——避免两边文案不一致。

#### `GET /api/doctors/{doctor_id}`

响应 `200`：

```json
{
  "ok": true,
  "data": {
    "id": 1, "name": "李建国", "title": "主任医师", "department": "口腔科",
    "intro": "从事口腔颌面外科临床工作 28 年……",
    "specialty": "复杂牙拔除、种植修复",
    "schedules": [ { 同 4.2 列表项结构 } ]
  }
}
```

内嵌的 `schedules` 同样默认只给今天及以后的排班，开关也是 `only_future`。
两处保持一致是刻意的：否则列表页看不到昨天的号、点进医生详情却看得到，
用户就会认为这两个页面在互相矛盾。

---

### 4.3 预约与候补

#### `POST /api/appointments`

请求：

```json
{ "schedule_id": 1 }
```

响应 `200`（两种形态，靠 `type` 区分）：

```json
{ "ok": true, "data": { "type": "booked", "appointment_id": 12, "seq_no": 1, "message": "预约成功，就诊序号 1" } }
```

```json
{ "ok": true, "data": { "type": "waitlisted", "waitlist_id": 5, "position": 2, "message": "号源已满，你已进入候补队列第 2 位" } }
```

**两种都在同一个事务里完成**：先校验三条防黄牛规则 → 试占号（`booked_count < total_slots` 条件更新成功与否）→ 占不到则插入候补并算 position。

**抢到号就等于自动出队**：若该用户此前正排着这个排班的队，本次预约成功会顺手把那条候补记录标为 `cancelled`。
不做这一步，队列里就会留下一张"死条目"——将来有人退号、轮询到它时，会给一个已经持证的人再发一张，
直接撞上 `ux_appt_user_slot` 唯一索引变成 500。

#### 候补 position 的两个含义（容易踩的坑）

对外返回的 `position` 是**名次**（排在第几位），但库里那个同名字段只是**排序键**，两者常常不相等。

原因：有人转正出队之后，我们**不会**把后面所有人的排序键减一。

| 做法 | 后果 |
|---|---|
| 出队时压缩（把 position 全部 -1） | 需要一条 UPDATE 同时改多行。SQLite 逐行检查唯一约束，更新顺序一旦是「先改大的」，就会临时撞上还没改小的那条记录 —— 一个只在特定数据下才复现的并发 bug |
| **不压缩（采用）** | 排序键只增不减，相对顺序永远正确，永远不存在撞键。代价是会留下空洞（队列里可能存着 2、3、7） |

代价由展示层承担：对外一律现算名次 —— 数一数排在他前面还有几个人。
换算函数在 `services/booking.py` 的 `rank_in_waitlist()`。

#### `DELETE /api/appointments/{appointment_id}`

取消自己的预约。响应 `200`：

```json
{
  "ok": true,
  "data": {
    "released_seq_no": 1,
    "promoted": {
      "user_name": "赵六", "seq_no": 1, "waitlist_id": 1, "appointment_id": 69,
      "pending": true, "confirm_deadline": "2026-10-25 22:39:04"
    },
    "message": "已退号，赵六自动转正，接过 1 号，请在 22:39 前确认"
  }
}
```

`promoted` 为 `null` 表示队列里没人（或整个队列都不合格），号源回到可约池。
`promoted` 里的人**不一定是原来的第 1 位** —— 队首不合规时会被顺延，见下。

**R5 之后 `promoted` 多了两个字段**，是「转正确认超时」带来的：

| 字段 | 说明 |
|---|---|
| `pending` | 恒为 `true`。递补出来的号是**待确认**状态，不再直接是"已预约" |
| `confirm_deadline` | 确认截止时刻，逾期不确认则作废并顺延给下一位 |

> 为什么不用 `status: "pending"`：这个对象只在"刚刚被递补"这一刻产生，
> 它的状态**必然**是待确认，用一个布尔常量比用一个只有唯一取值的枚举更诚实——
> 后者会让人以为它还可能返回别的值。真正的 `status` 字段在
> `GET /api/my/appointments` 里，那里的取值才是完整的五种。
>
> 另外 `promoted` 里**没有 `user_id`**，只有 `user_name`。
> 这是刻意的：退号者不该拿到递补者的用户标识，
> 而 `user_name` 只是为了让提示语能说清"这个号给了谁"。

**这个接口是全场最难的一段**：释放号 + 取候补首位 + 转正建预约 + 序号继承 + 计数增减，五件事必须在同一个 `BEGIN IMMEDIATE` 事务里。

**递补者拿到的是被释放的那个号**，不是"重新排队到队尾"（决策 5）。
代码里并没有直接沿用 `released_seq_no`，而是重跑一遍最小空号算法：
正常情况下两者必然相等（队列非空 ⇒ 退号前排班是满的 ⇒ 此前无任何空号），
但万一数据被手工改乱了，通用算法也不会发出一个撞号的序号。

#### 递补前必须重跑一遍规则（顺延机制）

入队时只校验"你没在排这条队"，**刻意不校验**每日限 1 号（理由见 `guards.py`）。
于是存在一条规规矩矩的操作路径能绕开规则二：

> 在 A 医生这儿排队 → 同一天同科室的 B 医生有号 → 直接约走 B 的号 → 数日后 A 这儿有人退号 →
> **A 的队首被递补 → 此人手里同时握着同天同科室的两个号**

**入队时不知道未来，出队时才知道。** 队列是一个延迟求值的规则检查点，
所以递补时必须重新跑一遍三条规则，不合格的队首当场作废、顺延下一位：

| 队首的情形 | 处理 |
|---|---|
| 同天同科室已有号 / 同时段已有号 / 已被拉黑 | 该条候补标记 `expired`，继续看下一位 |
| 队首就是退号者本人 | 同样标记 `expired` 并顺延：否则他先排队再退号，就能反复「退 → 递补给自己」把这个号永久锁在手里 |
| 排班本身已停诊（或日期已过） | 立即停止递补，谁的条目都不动（这是排班的问题，不是某个人的问题） |

被顺延掉的人不吃亏：他拿到的是 `expired` 而不是 `cancelled`，
`assert_waitable` 只认 `status='waiting'`，所以他可以重新排队，代价是排到队尾。
这条路径与加分项"转正确认超时 → 顺延下一位"共用同一段代码。

#### `DELETE /api/waitlist/{id}`

主动放弃候补（退出排队）。

响应 `200`：

```json
{ "ok": true, "data": { "waitlist_id": 5, "schedule_id": 2, "message": "已退出候补队列" } }
```

退出后**不压缩**任何人的 `position` —— 与候补队列的整体策略一致，
退出者的排序键原地作废，后面所有人的名次由 `rank_in_waitlist()` 现算，
于是在别人眼里名次自动前进了 1 位，而数据库一行都不用改。
这是"只增不压缩"策略的第二个受益点。

#### `GET /api/my/appointments`

响应 `200`：

```json
{
  "ok": true,
  "data": {
    "appointments": [
      {
        "id": 12, "schedule_id": 1, "doctor_name": "李建国", "department": "口腔科",
        "work_date": "2026-11-02", "period_label": "上午",
        "seq_no": 1, "status": "booked", "status_label": "待就诊",
        "source": "promoted", "source_label": "候补转正",
        "created_at": "2026-10-25 16:30:00",
        "confirm_deadline": null, "checked_in_at": null,
        "can_confirm": false, "can_check_in": false, "can_cancel": true
      }
    ],
    "waitlist": [
      {
        "id": 5, "schedule_id": 2, "doctor_name": "陈静", "department": "皮肤科",
        "work_date": "2026-11-03", "period_label": "下午",
        "position": 2, "status": "waiting", "status_label": "候补中",
        "joined_at": "2026-10-25 16:31:00"
      }
    ]
  }
}
```

三个 `can_*` 布尔标志由后端给出，**前端不要自己按 `status` 推断该显示哪个按钮**。
理由与 R4 决策 1「前端不预先判断号源还有没有」完全一致：
`can_check_in` 依赖"今天是不是就诊日"，而这个判断要用到**服务端的时钟**；
前端拿本地时钟去比，用户改一下系统时间就能骗出一个签到按钮。
后端给标志，等于把"能不能做"这件事也收归单一权威。
（注意 `can_confirm` 为 false 不代表不能确认，只是这条记录当前不需要确认 ——
真正的拒绝发生在 `POST .../confirm` 那一刻，那是 409 的活。）

> **这个接口刻意不返回 `doctor_id`**：预约记录只挂 `schedule_id`，
> 医生的姓名与科室由 JOIN 取出。
> 后果是 R4 的「我的预约」卡片**不做"查看医生"的链接** ——
> 拿 `schedule_id` 顶替 `doctor_id` 拼出来的链接会指向错误的医生，
> 宁可少一个入口，也不给一个会把用户带到错地方去的按钮。
> 若将来确实要这个入口，正确顺序是：先给接口补 `doctor_id`（改契约 → 改代码），
> 而不是让前端想办法凑。前端凑出来的字段，迟早会在某次改表之后失效。

---

## 5. 加分项接口（R5 五个全部实现）

| # | 加分项 | 涉及接口 |
|---|---|---|
| 1 | 退号冷却期 | 无新接口，改 `POST /api/appointments` 的前置校验 |
| 2 | 转正确认超时 | `POST /api/appointments/{id}/confirm`、`DELETE /api/appointments/{id}` |
| 3 | 就诊签到 | `POST /api/appointments/{id}/check-in` |
| 4 | 取消时限 | 无新接口，改 `DELETE /api/appointments/{id}` 的前置校验 |
| 5 | 违规拉黑 | `POST /api/admin/settle`、`POST /api/admin/users/{id}/blacklist` |

### 5.0 五个加分项共用一个可注入时钟

四个加分项（1、2、3、4）都要判断"现在几点"。项目原本的时间来源只有 SQL 的
`date('now','localtime')`，只到日期粒度，且**无法被测试控制** ——
要验证"15 分钟未确认就顺延"，不可能让测试真等一刻钟。

于是新增 `app/services/clock.py` 作为**全项目唯一的时间出口**：

| 出口 | 未冻结时 | 冻结后 |
|---|---|---|
| `clock.now_sql()` | `datetime('now','localtime')` | `'2026-10-25 22:00:00'`（字面量） |
| `clock.today_sql()` | `date('now','localtime')` | `'2026-10-25'` |
| `clock.now()` | `datetime.now()` | 冻结的那个值 |

业务代码一律走这三个出口，不再手写 `datetime('now','localtime')`。
测试用 `clock.freeze(...)` 把整个系统的"现在"拨到任意时刻，跑完 `clock.unfreeze()`。

> **为什么不用 monkeypatch 去 patch `datetime`**：那要 patch 掉 `datetime.datetime`
> 这个类本身，而 sqlite3 模块内部、日志模块都可能用到它，副作用不可控。
> 更关键的是我们根本不需要改 Python 的时间 ——
> 需要控制的是**SQL 里的 `now`**，所以时钟出口本来就该是一个「生成 SQL 片段」的东西。

冻结值插进 SQL 的是**内部生成的字面量**，不是用户输入，因此不存在注入风险
（与 `STATUS_SQL_EXPR` 里插值常量是同一个道理）。

### 5.1 转正确认超时（候补转正后 15 分钟内未确认则顺延）

原先 R0 预留的是 `POST /api/waitlist/{id}/confirm`，**实际改为**
`POST /api/appointments/{id}/confirm`。理由：待确认的人已经拿到号了，
这条记录记在 `appointments` 里（状态 `pending`），而不是还留在候补队列里 ——
记在 waitlist 上意味着"他还算在排队"，可他实际上已经出队了。

**递补流程（改后）**：

```
有人退号 → 取队首 → 建 pending 预约（占住号，记 confirm_deadline = now + 15min）
        → waitlist 记录置 promoted → 返回「已通知 XXX，请在 15 分钟内确认」
```

`DELETE /api/appointments/{id}` 的响应里，`promoted` 结构新增两个字段：

```json
{
  "released_seq_no": 1,
  "promoted": {
    "user_name": "赵六", "seq_no": 1,
    "waitlist_id": 7, "appointment_id": 42,
    "pending": true,
    "confirm_deadline": "2026-10-25 16:45:00"
  },
  "message": "已退号，已通知赵六，请其在 15 分钟内确认"
}
```

> **退号的人不需要知道下一位确没确认**：`promoted` 描述的是"我们把号给了谁"，
> 这是既成事实；至于他会不会确认，是另一件事，由他自己的页面去提示。

#### `POST /api/appointments/{appointment_id}/confirm`

确认一个 `pending` 的号。响应 `200`：

```json
{ "ok": true, "data": { "appointment_id": 69, "seq_no": 1,
                        "message": "确认成功，你的就诊序号是 1" } }
```

失败：`404 APPOINTMENT_NOT_FOUND`（不存在或不属于本人）、
`409 NOT_PENDING`（已确认过 / 是退号作废的，不是待确认的）、
`409 CONFIRM_TIMEOUT`（超时，号已顺延给下一位）。

> 提示语里**带上序号**而不是只说"确认成功"：这个接口的调用者刚从
> "待确认"状态过来，他最想知道的恰恰是自己到底几号。
> 确认成功后 `confirm_deadline` 就不再有意义，因此响应里不返回它——
> 想看当前状态就查 `GET /api/my/appointments`。

#### 超时判定是惰性的，不是定时任务

没有任何后台线程去轮询超时。**每次要读"这个排班现在什么情况"之前，先结算一次**：

```
settle_schedule(conn, schedule_id)
    ├─ 扫 pending 逾期的 → 作废预约 + waitlist 置 expired → 顺延下一位
    └─ 扫 已过日期且仍是 booked 的 → 置 no_show + no_show_count++ → 满 2 次拉黑
```

触发点三处：`POST /api/appointments`、`DELETE /api/appointments/{id}`、
`GET /api/my/appointments`，
外加一个手动入口 `POST /api/admin/settle`（见 5.5，为演示而开）。

> **为什么不做定时任务**：演示项目里起一个后台线程，服务一重启定时任务就没了，
> 而它恰好负责的是"清理僵尸状态" —— 这类最不该丢的东西却最容易丢。
> 惰性结算的好处是**只要有人来看，状态就一定是对的**；
> 没人来看的时候，超时与否不影响任何人的任何决策，早结算晚结算毫无差别。
> 这与 R3+ 的「候补队列是一台延迟求值的规则检查器」是同一个思路，
> 只是求值时机从"退号时"变成了"有人问时"。

### 5.2 退号冷却期（取消后 10 分钟内不可再约同一排班）

作用于**占号动作**：直接预约、以及递补时的规则校验。
**不作用于入候补** —— 排队不占号源，把排队也拦了只会白白剥夺患者的等待机会，
防黄牛防的是"退了又抢"，不是"退了又排"。

判定依据 `appointments.cancelled_at`：
同 user、同 schedule、状态为 `cancelled`、且取消时刻在最近 10 分钟内 → `409 COOLDOWN_ACTIVE`。

> 递补也查它是有必要的：否则黄牛可以「退号 → 立刻排队 → 有人退号时被递补」，
> 正好绕开冷却期。递补本来就要重跑一遍规则（见 4.3 顺延机制），
> 多这一条只是往那个已有的检查清单里加一项。

### 5.3 取消时限（开诊前 2 小时内不允许自助取消）

时段起点由 `policy.PERIOD_START_TIMES` 给出：`morning → 08:00`、
`afternoon → 14:00`、`evening → 18:30`。

判定：`(开诊时刻 - 现在) < 2 小时` → `409 CANCEL_WINDOW_CLOSED`。
未来日期的排班差值巨大，自然放行，不需要额外判"是不是今天"。

### 5.4 就诊签到

#### `POST /api/appointments/{appointment_id}/check-in`

响应 `200`：

```json
{ "ok": true, "data": { "appointment_id": 42, "seq_no": 1,
                        "checked_in_at": "2026-10-26 08:12:00",
                        "message": "签到成功，请等候叫号" } }
```

失败：`404`、`409 CHECKIN_NOT_OPEN`（还没到就诊当天）、
`409 CHECKIN_WINDOW_CLOSED`（日期已过，已按爽约处理）、`409 ALREADY_CHECKED_IN`。

**爽约结算**（惰性，与 5.1 同一次扫描）：排班日期已过且状态仍是 `booked`
→ 置 `no_show`，同时 `users.no_show_count += 1`；累计到 2 次 → `users.status = 'blacklisted'`。

> `no_show` 也留在 `ACTIVE_STATUS_CLAUSE` 和两个部分唯一索引里 ——
> 爽约的人**确实占了那个号**，只是没来。若把它当"已释放"，
> 后面的人就能约到同一个号，而这个号在医生那边的叫号单上是有名字的。

### 5.5 管理接口（仅演示用）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/admin/settle` | 手动触发一次全局结算（超时顺延 + 爽约拉黑），返回处理了多少条 |
| `POST` | `/api/admin/users/{user_id}/blacklist` | 拉黑（body 可带 `{"reason": "..."}`，仅记录不入库） |
| `DELETE` | `/api/admin/users/{user_id}/blacklist` | 解除拉黑 |

> **这三个接口同样不做鉴权**，与全局约定 3（本项目不做真实鉴权，只认 `X-User-Id`）
> 保持一致。真实系统里管理接口必须有独立的管理员鉴权与操作审计，
> 这里刻意省略，省略的理由要在 README 里明写，不能含糊过去。
>
> `POST /api/admin/settle` 存在的唯一理由是**演示**：
> 惰性结算平时由业务请求顺带触发，但答辩时要当众展示"超过 15 分钟没确认会怎样"，
> 总不能真等一刻钟。它把"没人来问也要结算一次"这个动作显式暴露出来。

---

## 6. 内部服务契约（不进 HTTP，但必须遵守）

| 模块 | 函数 | 职责 |
|---|---|---|
| `db.py` | `transaction(conn)` | 上下文管理器，用 `BEGIN IMMEDIATE` 开事务，路由层负责开关 |
| `services/slot_status.py` | `compute_status(booked_count, total_slots, is_closed, waiting_count)` | 返回 `(status, status_label, remaining)`，**全项目唯一入口** |
| `services/slot_status.py` | `STATUS_SQL_EXPR` | 同一套判定逻辑的 SQL 版，只用于 `WHERE` 筛选（见下） |
| `services/guards.py` | `assert_bookable(conn, user_id, schedule)` | 占号前三条防黄牛校验，违规抛业务异常 |
| `services/guards.py` | `assert_waitable(conn, user_id, schedule)` | 入候补前校验（**不含**每日限 1 号那条） |
| `services/guards.py` | `fetch_schedule(conn, schedule_id)` | 取排班，顺带查出 `today`（共四档校验要用） |
| `services/booking.py` | `create_booking(conn, user_id, schedule_id)` | 占号或入候补，返回 dict |
| `services/booking.py` | `cancel_booking(conn, user_id, appointment_id)` | 退号 + 递补（含顺延），返回 dict |
| `services/booking.py` | `leave_waitlist(conn, user_id, waitlist_id)` | 放弃候补，返回 dict |
| `services/booking.py` | `rank_in_waitlist(conn, schedule_id, position)` | 排序键 → 给人看的名次 |
| `services/booking.py` | `confirm_promotion(conn, user_id, appointment_id)` | 加分项 2：确认 `pending` 的号，`pending → booked` |
| `services/booking.py` | `check_in(conn, user_id, appointment_id)` | 加分项 3：签到，`booked → completed` |
| `services/booking.py` | `settle_schedule(conn, schedule_id)` | 加分项 2/3：结算单个排班的逾期 pending 与过期爽约 |
| `services/booking.py` | `settle_all(conn)` | 加分项 2/3：全局结算，供 `/api/admin/settle` 与惰性触发点调用 |
| `services/guards.py` | `assert_not_in_cooldown(conn, user_id, schedule)` | 加分项 1：退号冷却期 |
| `services/guards.py` | `assert_cancel_window_open(conn, schedule)` | 加分项 4：取消时限 |
| `services/policy.py` | （常量） | 加分项：全部阈值与时段映射的唯一来源 |
| `services/clock.py` | `now_sql()` / `today_sql()` / `now()` / `freeze()` | 加分项：全项目唯一的时间出口，可被测试冻结 |

参数 `conn` 一律是**已开启事务**的连接，服务层自己不 commit —— 事务边界归路由层管。这样 R5 写并发测试时，可以直接在服务层外面套事务做断言。

> `settle_*` 是**写入**函数，不是查询。所以它只能在事务里被调用 ——
> 惰性结算意味着"读一个页面"也可能需要写库，这一点在路由层体现为：
> `GET /api/my/appointments` 也套了 `db.transaction(conn)`。
> 一个 GET 请求开写事务看起来奇怪，但它做的事实上就是一次状态推进，
> 与"查询"是两回事。这也解释了为什么它不能做成 `async def` 的纯读接口。

### 为什么状态判定有两份（`compute_status` 与 `STATUS_SQL_EXPR`）

状态不落库，所以想在 SQL 里按状态筛选，就只能把判定逻辑在 SQL 里再写一遍。
这是明知有重复代价仍做的设计，取舍如下：

| 方案 | 做法 | 代价 |
|---|---|---|
| A. 全表取出在 Python 里筛 | 逻辑只有一份 | 每次筛选都要把所有排班捞出来，数据一大就撑不住 |
| B. 筛选下推到 SQL（**采用**） | 逻辑有两份，可能改一处忘另一处 | 用一致性测试锁住，见下 |
| C. 把落库状态改成真字段 | 只有一份且能筛 | **否决**：回到了"每次进出队都要记得同步"的老问题，见 `DATA_MODEL.md` 决策 2 |

选 B 就要认下这份代价，所以补两道保险：

1. 两份逻辑**放在同一个文件里、上下相邻**，改的时候必然看到对方；
2. 两条路径的判定顺序**逐条对齐**（停诊 → 有余号 → 有人排队 → 满），SQL 里写成 `CASE WHEN` 的顺序与 Python 的 `if/elif` 完全一致；
3. **显示永远走 `compute_status()`**，SQL 版只用于 `WHERE`。万一 SQL 写错，页面显示依然正确，会退化成"筛不准"而不是"显示错"；
4. **R5 已落盘** `tests/test_status_consistency.py`：把 24 种组合
   （停诊 × 占号数 × 排队人数）用两条路径各算一遍，逐条断言相等。

第 3 条是关键 —— 它决定了出错时的**退化方向**。筛选错只是不方便，显示错是数据事故。

#### 变异测试：给测试本身做体检

但第 4 条还藏着一个问题：**一个从没红过的测试，没人知道它到底在不在测东西。**
一致性测试通过，可能只是因为数据压根没走到那条分支。

于是再补一道**变异测试**：把 SQL 表达式故意改坏四种，
逐个验证一致性检查**确实会变红**。判定标准刻意写成"期待不一致"。

R5 实测，四个变异体全部被抓到：

| 故意改坏的地方 | 被抓到的排班数 |
|---|---|
| 漏掉候补分支 | 2 |
| 候补门槛从 `> 0` 改成 `> 1` | 1 |
| 把候补判定提到有余号之前（顺序颠倒） | 6 |
| 漏掉停诊分支 | 12 |

若哪个变异体能悄悄溜过去，就说明那条分支没被数据覆盖到，
上面那句"两条路径一致"只是碰巧没走到。所以还有一条
`test_all_four_statuses_are_covered` 守在前面：
四种状态在数据矩阵里必须都出现过 —— 这是变异测试能成立的前提。
