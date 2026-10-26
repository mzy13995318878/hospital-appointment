"""预约前的规则校验（防黄牛三条）。

本模块只做判断，**不做任何写入**。所有函数都要求传入一个已在事务内的
连接 —— 校验通过到真正写入之间，绝不能被人插进来，否则「检查时还有号」
和「写入时号已被抢走」就对不上了。

三条防黄牛规则的分工见 docs/DATA_MODEL.md 决策 6：

    能写进索引的（同时段不重复预约）→ 库里还有部分唯一索引兜底
    跨三表的（同天同科室限 1 号）→ 只能在应用层做

**为什么应用层有了校验，库里还要留索引**：
应用层校验是为了给出准确的错误码（"你已约过此时段"），
索引是为了在应用层被绕过时（直接写库、代码改错、未来新增入口）
让插入直接失败，而不是悄悄产生脏数据。
两者防的是不同环节，不是重复劳动。
"""

from __future__ import annotations

import sqlite3

from app import db
from app.errors import BizError
from app.services import clock, policy

# 「仍然占着号」的预约状态。
#
# 清单定义在 app/db.py（ACTIVE_APPOINTMENT_STATUSES），这里只是引用它生成的片段。
# 为什么不在这里再写一遍：这份清单同时出现在四个地方 ——
#   sql/schema.sql 的两个唯一索引 WHERE、db.py 的迁移、
#   本文件的运行时判断、booking.py 的最小空号算法。
# 各处自抄一份，R5 加 `pending` 时就必然漏改某一处，
# 而漏掉的那处恰好是"库级兜底"，平时看不出来。
#
# 写成 IN 白名单而不是 <> 'cancelled' 黑名单：将来若新增一种
# 「仍然占着号」的状态，用黑名单会悄悄把它漏掉，
# 后果是两个人拿到同一个就诊序号 —— 这种错误在系统里是致命的。
ACTIVE_STATUS_CLAUSE = db.ACTIVE_STATUS_CLAUSE


def _start_datetime_expr(alias: str = "s") -> str:
    """「这个排班几点开诊」的 SQL 表达式。

    库里只存了 `morning / afternoon / evening` 这种时段枚举，
    没有具体钟点，而取消时限要算的正是"距离开诊还有多久"。
    于是把枚举映射成时刻，映射表放在 `policy.PERIOD_START_TIMES`。

    时刻写在 policy 里而不是散在这里：改开诊时间这件事
    应该只改一个数字，而不是来业务校验里翻 CASE 分支。
    """
    branches = "\n".join(
        f"           WHEN '{p}' THEN {alias}.work_date || ' {t}:00'"
        for p, t in policy.PERIOD_START_TIMES.items()
    )
    default = policy.DEFAULT_PERIOD_START
    return (
        f"CASE {alias}.period\n{branches}\n"
        f"           ELSE {alias}.work_date || ' {default}:00'\n"
        f"       END"
    )


def minutes_until_start_expr(alias: str = "s") -> str:
    """「距离开诊还有几分钟」的 SQL 表达式，为负表示已经开诊了。

    用 `julianday()` 相减再乘 1440（一天的分钟数）。
    SQLite 没有原生的日期差函数，julianday 是标准做法 ——
    它把日期转成儒略日浮点数，相减得到天数，再换算成分钟。

    `now` 一律取 `clock.now_sql()`：这样测试把时钟拨快 3 小时后，
    同一个排班会从"还有 5 小时开诊"变成"已经开诊了"，
    取消时限的判定随之改变，不需要测试去改数据。
    """
    return (
        f"(julianday({_start_datetime_expr(alias)})"
        f" - julianday({clock.now_sql()})) * 24 * 60"
    )


def fetch_schedule(conn: sqlite3.Connection, schedule_id: int) -> sqlite3.Row:
    """取出排班基本信息，不存在则抛业务异常。

    顺带把科室名一起查出来：同天同科室限 1 号那条规则要用，
    这样调用方不必再查一次 doctors 表。

    再顺带查出「今天」：`date('now','localtime')`。
    判断排班日期是否已过，得有一个"今天"作为基准；
    由 SQL 给出，与 seed.sql 生成排班日期用的是**同一个时钟**。
    若改用 Python 的 date.today()，换台时区不同的机器就会算出不同的今天。

    R5 起，这个「今天」由 `clock.today_sql()` 给出 —— 未冻结时它与上面
    那句逐字等价，冻结后就变成测试指定的日期。
    业务代码因此不必知道自己正跑在测试里。

    顺带再查出「距离开诊还有多少分钟」：加分项 4（取消时限）要用。
    一并查出而不是用到时再查一次，是为了让调用方拿到的 row 里
    **时间相关的信息全都在同一个时钟下** —— 分两次查，中间若时钟被推进
    （测试里就常这么干），两个数字会来自不同的时刻。
    """
    row = conn.execute(
        "SELECT s.id, s.work_date, s.period, s.total_slots, "
        "       s.booked_count, s.is_closed, d.department, "
        f"       {clock.today_sql()} AS today, "
        f"       {minutes_until_start_expr('s')} AS minutes_until_start "
        "  FROM schedules s "
        "  JOIN doctors d ON d.id = s.doctor_id "
        " WHERE s.id = ?",
        (schedule_id,),
    ).fetchone()
    if row is None:
        raise BizError("SCHEDULE_NOT_FOUND")
    return row


def assert_not_closed(schedule: sqlite3.Row) -> None:
    """停诊的排班一律不能约，也不能排队。

    注意这条判断的是「人为设定」，优先级高于一切算术 ——
    即使一个号都没发出去，停诊就是停诊。
    """
    if schedule["is_closed"]:
        raise BizError("SCHEDULE_CLOSED")


def assert_not_expired(schedule: sqlite3.Row) -> None:
    """日期已经过去的排班，既不能预约也不能排队。

    与停诊那条是同一类判断：**事实优先于算术**。
    一个过去的排班即便一个号都没发出去，也约不了 ——
    号源是带时间属性的资源，约到昨天的号没有任何意义。

    **刻意不拦今天当天的号**：现场挂号是真实存在的业务，
    当天早上抢当天上午的号完全合理。所以判断用严格小于，不含等号。
    """
    if schedule["work_date"] < schedule["today"]:
        raise BizError("SCHEDULE_EXPIRED")


def assert_not_blacklisted(conn: sqlite3.Connection, user_id: int) -> None:
    """加分项（违规拉黑）的占位检查。

    R3 阶段 users.status 恒为 active，这条永远通过。
    现在就写在这，是为了让 R5 加拉黑逻辑时不用再动预约主流程。
    """
    row = conn.execute(
        "SELECT status FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is not None and row["status"] == "blacklisted":
        raise BizError("USER_BLACKLISTED")


def assert_slot_not_taken(
    conn: sqlite3.Connection, user_id: int, schedule: sqlite3.Row
) -> None:
    """规则一：同一用户同一出诊时段不可重复预约。

    库里还有 ux_appt_user_slot 这个部分唯一索引兜底（见模块 docstring）。
    """
    row = conn.execute(
        f"SELECT id FROM appointments "
        f" WHERE user_id = ? AND schedule_id = ? AND {ACTIVE_STATUS_CLAUSE}",
        (user_id, schedule["id"]),
    ).fetchone()
    if row is not None:
        raise BizError("ALREADY_BOOKED_THIS_SLOT")


def assert_daily_dept_limit(
    conn: sqlite3.Connection, user_id: int, schedule: sqlite3.Row
) -> None:
    """规则二：同一用户同一天同一科室最多 1 个号。

    这条必须跨 appointments + schedules + doctors 三张表才能判断，
    唯一索引表达不了，所以只有应用层这一道防线 ——
    也正因如此，这条规则最需要 R5 的单元测试覆盖。

    **候补不在此列**：候补者没有占有任何号源，
    把"同科室排了两位医生的候补"也算违规，会让患者白白失去机会，
    与便民目标相反（该取舍见 docs/DATA_MODEL.md 决策 6 边界说明）。
    """
    row = conn.execute(
        f"SELECT a.id "
        f"  FROM appointments a "
        f"  JOIN schedules s ON s.id = a.schedule_id "
        f"  JOIN doctors   d ON d.id = s.doctor_id "
        f" WHERE a.user_id = ? AND s.work_date = ? AND d.department = ? "
        f"   AND a.{ACTIVE_STATUS_CLAUSE}",
        (user_id, schedule["work_date"], schedule["department"]),
    ).fetchone()
    if row is not None:
        raise BizError("DAILY_DEPT_LIMIT")


def assert_not_in_cooldown(
    conn: sqlite3.Connection, user_id: int, schedule: sqlite3.Row
) -> None:
    """加分项 1：刚退号的人，冷却期内不能再约同一个排班。

    判定依据是 `appointments.cancelled_at` —— 冷却期的起点就是
    "上一次退号的时刻"，这个时刻本来就记着，不必再开一张 cooldowns 表。
    再开一张表存同样的信息，等于给"两处数据可能对不上"主动创造机会。

    **只拦占号动作，不拦排队**（排队由 assert_waitable 管）：
    排队不占用任何号源，把排队也拦了只会白白剥夺患者的等待机会。
    防黄牛要防的是"退了又抢"，不是"退了又排"。

    **但递补要查这一条**：递补是占号动作。
    否则黄牛可以「退号 → 立刻排队 → 有人退号时被递补」，正好绕开冷却期。
    """
    row = conn.execute(
        "SELECT 1 FROM appointments "
        " WHERE user_id = ? AND schedule_id = ? AND status = 'cancelled' "
        "   AND cancelled_at IS NOT NULL "
        f"   AND cancelled_at > {clock.shift_sql(-policy.CANCEL_COOLDOWN_MINUTES)} "
        " LIMIT 1",
        (user_id, schedule["id"]),
    ).fetchone()
    if row is not None:
        raise BizError(
            "COOLDOWN_ACTIVE",
            f"你刚退掉这个时段的号，请 {policy.CANCEL_COOLDOWN_MINUTES} 分钟后再试",
        )


def assert_cancel_window_open(schedule: sqlite3.Row) -> None:
    """加分项 4：临近开诊不允许自助取消。

    截止线由 `policy.CANCEL_CUTOFF_MINUTES` 给出（开诊前 2 小时）。

    为什么要有这条：临近开诊时空出来的号，已经来不及再递补给候补者 ——
    他人在别处，赶不过来。于是这个号实际上会白白空掉，
    而候补队列里明明还站着几个人。
    还不如干脆不让退，把"临时变卦"这件事挡在 2 小时之外。

    先判过期再判临近：两者的处理路径不同 ——
    过期的号本就该由结算流程转成 no_show，退号这个动作对它没有意义。
    """
    if schedule["work_date"] < schedule["today"]:
        raise BizError("SCHEDULE_EXPIRED", "该排班日期已过，无法退号")
    if schedule["minutes_until_start"] < policy.CANCEL_CUTOFF_MINUTES:
        raise BizError(
            "CANCEL_WINDOW_CLOSED",
            f"距开诊不足 {policy.CANCEL_CUTOFF_MINUTES // 60} 小时，"
            f"已不能自助退号，请联系分诊台",
        )


def assert_bookable(
    conn: sqlite3.Connection, user_id: int, schedule: sqlite3.Row
) -> None:
    """占号前的全部校验（直接预约），任何一条不过就抛业务异常。"""
    assert_not_closed(schedule)
    assert_not_expired(schedule)
    assert_not_blacklisted(conn, user_id)
    assert_not_in_cooldown(conn, user_id, schedule)
    assert_slot_not_taken(conn, user_id, schedule)
    assert_daily_dept_limit(conn, user_id, schedule)


def assert_promotable(
    conn: sqlite3.Connection, user_id: int, schedule: sqlite3.Row
) -> None:
    """递补（候补转正）前的全部校验。

    **刻意与 `assert_bookable` 分开成两个函数**，尽管它们此刻的内容几乎一样：

        assert_bookable   —— 用户主动预约，他此刻就在屏幕前
        assert_promotable —— 系统替一个不在场的人做决定

    两者的**失败后果完全不同**。主动预约被拒，用户看到一条提示，
    仅此而已；递补被拒，系统必须把他的候补记录顺延作废、继续找下一个人 ——
    被拒是正常流程的一环，不是异常情况。

    将来两者注定会分岔（比如递补时要额外判断"他是否在冷却期后重新排的队"），
    合成一个函数时就只能往里塞 if 参数，
    那才是真正的坏味道：一个函数靠参数决定自己是什么。

    R3+ 的注释里说「候补队列是一台延迟求值的规则检查器」，
    本函数就是那台检查器的检查清单。加一条规则只改这里。
    """
    assert_not_blacklisted(conn, user_id)
    assert_not_in_cooldown(conn, user_id, schedule)
    assert_slot_not_taken(conn, user_id, schedule)
    assert_daily_dept_limit(conn, user_id, schedule)


def assert_waitable(
    conn: sqlite3.Connection, user_id: int, schedule: sqlite3.Row
) -> None:
    """入候补前的全部校验。

    刻意**不含**每日限 1 号那条 —— 理由见 assert_daily_dept_limit 的注释。
    但含停诊与拉黑：停诊的排班连队都排不了，否则递补时会无处可补。
    过期的排班同理：昨天的号永远不会有人退，排上去只是白等一场。
    """
    assert_not_closed(schedule)
    assert_not_expired(schedule)
    assert_not_blacklisted(conn, user_id)
    row = conn.execute(
        "SELECT id FROM waitlist "
        " WHERE schedule_id = ? AND user_id = ? AND status = 'waiting'",
        (schedule["id"], user_id),
    ).fetchone()
    if row is not None:
        raise BizError("ALREADY_WAITING")
