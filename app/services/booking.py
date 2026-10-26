"""预约、候补、退号递补 —— 全项目的业务核心。

本模块是全项目唯一改动 schedules.booked_count 的地方。
计数只有一处会变，就不会出现「两处各自加减、最后对不上」的经典记账bug。

所有函数都要求传入一个**已在事务内**的连接，且自己绝不 commit/rollback ——
事务边界归路由层管（见 docs/API.md 第 6 节）。
这样 R5 写并发测试时，可以在服务层外面随便套事务做断言。

三条最重要的实现纪律：

1. 防超卖不靠「先查再写」，靠**把条件写进 UPDATE 的 WHERE**：
       UPDATE schedules SET booked_count = booked_count + 1
        WHERE id = ? AND booked_count < total_slots
   判断与写入被数据库合成一个不可打断的操作。

2. 就诊序号取 1..total 里**最小的空号**，不是 MAX+1
   （后者的反例见 docs/DATA_MODEL.md 决策 3）。

3. 候补队列的 position **只增不压缩**，对外展示的位次现算
   （理由见下方 _rank_of 的注释）。
"""

from __future__ import annotations

import sqlite3

from app.errors import BizError
from app.services import clock, guards, policy

# 时间戳一律用 localtime。
# SQLite 的 datetime('now') 默认按 UTC 算，东八区会差 8 小时，
# 演示时会出现「凌晨 3 点退的号」这种穿帮数据。
#
# R5 起，这个"现在"不再是一个写死的常量，而是由 clock.now_sql() 给出：
# 未冻结时它与上面那句逐字等价；测试里 clock.freeze() 之后，
# 它就变成测试指定的那一刻 —— 加分项的超时、冷却、时限全靠它来测。
#
# 于是下面所有写时间的地方都用 `{clock.now_sql()}` 而不是 `{clock.now_sql()}`：
# 模块级常量只在 import 时求值一次，而时钟必须**每次调用都重新取值**，
# 否则冻结发生在 import 之后就完全不生效 ——
# 这类 bug 的表现是"测试里时间没生效"，很难想到根因在常量上。


def _min_free_seq(conn: sqlite3.Connection, schedule_id: int, total_slots: int) -> int:
    """在 1..total_slots 里找出最小的未被占用序号。

    为什么不能用 MAX(seq_no)+1，见 docs/DATA_MODEL.md 决策 3：
    12 个号全占满时，有人退掉 1 号，MAX+1 得到 13，直接撞破
    CHECK(seq_no <= total_slots) —— 而最小空号算法拿到的是 1。

    过滤条件与 guards.ACTIVE_STATUS_CLAUSE 严格一致，
    两边说的都是「哪些记录仍然占着号」。
    """
    used = {
        r["seq_no"]
        for r in conn.execute(
            f"SELECT seq_no FROM appointments "
            f" WHERE schedule_id = ? AND {guards.ACTIVE_STATUS_CLAUSE}",
            (schedule_id,),
        )
    }
    for n in range(1, total_slots + 1):
        if n not in used:
            return n
    # 到不了这里：能调用本函数的前提是刚空出一个号。
    # 真走到这里说明记账已经错了，宁可报错也不要发一个越界的号。
    raise BizError("SLOT_FULL", "号源数据异常：没有可用序号")


def rank_in_waitlist(
    conn: sqlite3.Connection, schedule_id: int, position: int
) -> int:
    """算出某个人在候补队列里排第几位。

    **本函数是公开 API**，「我的预约」页也要用它把库里的排序键
    翻译成能给人看的名次，因此不在名字前加下划线。

    position 是**内部排序键**，不是给外人看的名次：
    有人转正出队后，我们刻意不去把后面所有人的 position 减 1。
    因为那需要一条 UPDATE 同时改多行，而 SQLite 是逐行检查唯一约束的，
    更新顺序一旦是「先改大的」，就会临时撞上还没改小的那条记录 ——
    一个只在特定数据下才复现的并发 bug。

    不压缩就没有这个问题：position 只管单调递增，相对顺序永远正确。
    代价是它会留下空洞（比如队列里存着 2、3、7），
    所以对外一律现算名次：数一数排在他前面还有几个人。
    """
    return conn.execute(
        "SELECT COUNT(*) FROM waitlist "
        " WHERE schedule_id = ? AND status = 'waiting' AND position <= ?",
        (schedule_id, position),
    ).fetchone()[0]


def _fetch_head(conn: sqlite3.Connection, schedule_id: int) -> sqlite3.Row | None:
    """取候补队首（position 最小的那条），队列为空则返回 None。

    单独抽出来是因为递补要**循环调用**它：每顺延掉一个不合格的人，
    就得重新查一次队首。
    """
    return conn.execute(
        "SELECT w.id, w.user_id, u.name "
        "  FROM waitlist w JOIN users u ON u.id = w.user_id "
        " WHERE w.schedule_id = ? AND w.status = 'waiting' "
        " ORDER BY w.position LIMIT 1",
        (schedule_id,),
    ).fetchone()


def _expire_waitlist(conn: sqlite3.Connection, waitlist_id: int) -> None:
    """把一条候补作废，让位给后面的人。

    用 `expired` 而不是 `cancelled`，是为了在库里区分**谁动的手**：

        cancelled —— 用户自己放弃（leave_waitlist）
        expired   —— 系统在递补时发现他已不符合规则

    两者的对外后果完全一样（都能重新排队），
    但将来统计"多少人自己放弃了、多少人因为规则冲突被顺延"时是两个不同的数。
    """
    conn.execute(
        f"UPDATE waitlist SET status = 'expired', resolved_at = {clock.now_sql()} "
        f" WHERE id = ?",
        (waitlist_id,),
    )


def create_booking(
    conn: sqlite3.Connection, user_id: int, schedule_id: int
) -> dict:
    """预约一个号；号满了就自动进候补队列。

    两种结果都是**正常业务流**，都返回 200，靠 type 字段区分 ——
    号满不是错误，是"换个方式继续"，见 docs/API.md 第 3 节末尾。

    Returns:
        {"type": "booked", ...} 或 {"type": "waitlisted", ...}

    Raises:
        BizError: 停诊、拉黑、重复预约、超出每日限额、重复排队、冷却期内。
    """
    # 先结算，再看这个排班现在什么情况。
    # 顺序不能倒：结算会释放"确认超时"的号，
    # 倒过来的话我们拿着的是一份已经过期的 booked_count，
    # 于是用户会看到"号满了去排队"，而实际上刚刚空出来一个号。
    settle_schedule(conn, schedule_id)
    schedule = guards.fetch_schedule(conn, schedule_id)

    # 先判断满不满，决定走哪条分支。
    # 这个 SELECT 只是决定路线，**不做任何决定**：真正的写入仍由下面的
    # 条件 UPDATE 拍板。因此即便判断与更新之间被插进了别的事务（
    # 在 IMMEDIATE 事务内不会发生），也不会超卖。
    if schedule["booked_count"] < schedule["total_slots"]:
        guards.assert_bookable(conn, user_id, schedule)

        # 防超卖的关键一步：把「还有号」这个条件写进 WHERE。
        # 判断与写入由数据库合成一个操作，不存在中间状态。
        # 顺带把 is_closed=0 也写进来 —— 即使应用层校验被绕过，
        # 停诊的排班也占不到号（纵深防御，不是重复劳动）。
        cur = conn.execute(
            "UPDATE schedules SET booked_count = booked_count + 1 "
            " WHERE id = ? AND booked_count < total_slots AND is_closed = 0",
            (schedule_id,),
        )
        if cur.rowcount == 1:
            seq_no = _min_free_seq(conn, schedule_id, schedule["total_slots"])
            appt_id = conn.execute(
                f"INSERT INTO appointments"
                f"(user_id, schedule_id, seq_no, status, source, created_at) "
                f"VALUES (?, ?, ?, 'booked', 'direct', {clock.now_sql()})",
                (user_id, schedule_id, seq_no),
            ).lastrowid

            # 抢到号 = 自动出队（契约 4.3）。
            # 若此人此前正排着这个排班的队，这条记录必须作废：
            # 留着就是一张死条目，将来有人退号、轮询到它时，
            # 会给一个已经持证的人再发一张，直接撞 ux_appt_user_slot 变 500。
            conn.execute(
                f"UPDATE waitlist SET status = 'cancelled', resolved_at = {clock.now_sql()} "
                f" WHERE schedule_id = ? AND user_id = ? AND status = 'waiting'",
                (schedule_id, user_id),
            )

            return {
                "type": "booked",
                "appointment_id": appt_id,
                "seq_no": seq_no,
                "message": f"预约成功，就诊序号 {seq_no}",
            }
        # rowcount 不为 1：号在这几行之间被抢走了。
        # 事务内本不该发生，落到下面，让用户进候补队列即可 ——
        # 这比抛一个没人看得懂的内部错误要好得多。

    # ---- 候补分支 ----
    guards.assert_waitable(conn, user_id, schedule)

    # 位置取「当前队尾 + 1」。这里必须用 MAX 而不是 COUNT：
    # 历史记录（已转正/已取消）也占着 position 值，
    # COUNT 数出来的位置可能已经被别人用过了。
    next_pos = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM waitlist "
        " WHERE schedule_id = ? AND status = 'waiting'",
        (schedule_id,),
    ).fetchone()[0]

    wl_id = conn.execute(
        f"INSERT INTO waitlist(user_id, schedule_id, position, status, joined_at) "
        f"VALUES (?, ?, ?, 'waiting', {clock.now_sql()})",
        (user_id, schedule_id, next_pos),
    ).lastrowid

    rank = rank_in_waitlist(conn, schedule_id, next_pos)
    return {
        "type": "waitlisted",
        "waitlist_id": wl_id,
        "position": rank,
        "message": f"号源已满，你已进入候补队列第 {rank} 位",
    }


def cancel_booking(
    conn: sqlite3.Connection, user_id: int, appointment_id: int
) -> dict:
    """退号，并立刻把空出来的号递补给候补队首（**待其确认**）。

    五件事必须在同一个事务里完成，少一件数据就不一致：
        软删除预约 → 计数减一 → 取候补队首 → 建待确认预约 → 计数加一

    但**最终拿到号的不一定是"第 1 位"**：队首有可能当场就不符合占号条件，
    此时顺延到下一位（理由见 `_promote_next` 里那段长注释）。

    R5 加了两个前置判断（加分项 1 与 4）：
        退号前先过一遍取消时限（临近开诊不让退），
        并且先结算一次（清掉别人的僵尸状态再动手）。

    Returns:
        {"released_seq_no": int, "promoted": dict | None, "message": str}

    Raises:
        BizError: 预约不存在（或不属于当前用户）、已取消过、临近开诊、
            排班日期已过。
    """
    appt = conn.execute(
        "SELECT id, schedule_id, seq_no, status FROM appointments "
        " WHERE id = ? AND user_id = ?",
        (appointment_id, user_id),
    ).fetchone()
    # 查不到有两种可能：id 根本不存在，或者存在但不是这个人的。
    # 契约把两者都定为 404 —— 顺便也避免了「用别人 id 试探」
    # 泄露「这个号存在但不属于你」的信息。
    if appt is None:
        raise BizError("APPOINTMENT_NOT_FOUND")
    if appt["status"] != "booked":
        raise BizError("ALREADY_CANCELLED")

    schedule_id = appt["schedule_id"]
    released_seq_no = appt["seq_no"]

    # 先结算再动手，理由与 create_booking 里那句一致：
    # 结算会释放掉"确认超时"的号，倒过来的话我们拿到的是过期的数据。
    settle_schedule(conn, schedule_id)
    schedule = guards.fetch_schedule(conn, schedule_id)

    # 加分项 4：临近开诊不让自助退号。
    # 放在软删除**之前**：一旦先删了再发现不能退，
    # 就得把刚才那一串改动回滚，而"先判断再动手"从来都比"动手失败再撤销"可靠。
    guards.assert_cancel_window_open(schedule)

    # 软删除（决策 4）：记录留着，加分项的爽约统计要用它
    conn.execute(
        f"UPDATE appointments SET status = 'cancelled', cancelled_at = {clock.now_sql()} "
        f" WHERE id = ?",
        (appointment_id,),
    )
    conn.execute(
        "UPDATE schedules SET booked_count = booked_count - 1 WHERE id = ?",
        (schedule_id,),
    )

    promoted, skipped = _promote_next(conn, schedule_id, exclude_user_id=user_id)

    if promoted is not None:
        if promoted["pending"]:
            message = (
                f"已退号，已通知{promoted['user_name']}，"
                f"请其在 {policy.CONFIRM_TIMEOUT_MINUTES} 分钟内确认"
            )
        else:
            message = (
                f"已退号，{promoted['user_name']} 自动转正，"
                f"接过 {promoted['seq_no']} 号"
            )
        if skipped:
            message += f"（顺延了 {skipped} 位不符合规则的候补者）"
    else:
        message = f"已退号，{released_seq_no} 号已释放"

    return {
        "released_seq_no": released_seq_no,
        "promoted": promoted,
        "message": message,
    }


def _promote_next(
    conn: sqlite3.Connection,
    schedule_id: int,
    exclude_user_id: int | None = None,
) -> tuple[dict | None, int]:
    """把空出来的号递补给候补队首，返回 `(递补结果, 顺延人数)`。

    **R5 的关键改动**：递补出来的不是 `booked` 而是 **`pending`** ——
    号先给他留着，他在 `policy.CONFIRM_TIMEOUT_MINUTES` 分钟内确认了才真正拿到。
    确认的动作见 `confirm_promotion()`，超时的处理见 `settle_schedule()`。

    **为什么 pending 也要把 booked_count 加一**：
    它确实占着那个号。不加的话，这 15 分钟里别人能直接把号约走，
    等他来确认时号早没了 —— "给他留着"就成了句空话。
    代价是队列在这段时间里被卡住，后面的人要等，
    而这个等待被上面的超时阈值明确限定了，不会无限期。

    为什么不能"直接把队首转正"：
    入队时只校验"你没在排这条队"，**刻意不校验**每日限 1 号（见 guards 注释）。
    于是存在一条完全合规的操作路径能绕开规则二 ——
      在 A 医生这儿排队 → 同天同科室的 B 医生有号 → 直接约走 →
      A 这儿有人退号 → 此人手握同天同科室的两个号，甚至直接撞唯一索引 500。

    入队时不知道未来，出队时才知道。队列是一个**延迟求值**的规则检查点，
    而 `guards.assert_promotable()` 就是这台检查器的检查清单。

    Args:
        conn: 已在事务内的连接。
        schedule_id: 要递补的排班。
        exclude_user_id: 排除掉的人（退号者本人），见下面循环里的说明。

    Returns:
        (promoted 字典或 None, 被顺延的人数)。
    """
    schedule = guards.fetch_schedule(conn, schedule_id)

    # 先排除排班自身的问题：停诊或日期已过属于排班的事，不是某个人的事，
    # 这时整个队列都发不出号，直接停手且谁的条目都不动。
    if schedule["is_closed"] or schedule["work_date"] < schedule["today"]:
        return None, 0

    skipped = 0
    while True:
        head = _fetch_head(conn, schedule_id)
        if head is None:
            return None, skipped

        # 退号者本人不能把刚退掉的号原样拿回去。
        # 否则他只要先排队、再退号，就能反复"退 → 递补给自己"，
        # 把这个号永久锁在手里，后面排队的人永远轮不上 ——
        # 这正是防黄牛要防的霸号行为。
        if exclude_user_id is not None and head["user_id"] == exclude_user_id:
            _expire_waitlist(conn, head["id"])
            skipped += 1
            continue

        try:
            guards.assert_promotable(conn, head["user_id"], schedule)
        except BizError:
            # 队首此刻已不符合占号条件 → 作废它，顺延看下一位。
            _expire_waitlist(conn, head["id"])
            skipped += 1
            continue

        # 递补者继承被释放的序号（决策 5）。
        # 这里刻意不直接用 released_seq_no，而是重跑一遍最小空号算法：
        # 正常情况下两者必然相等（队列非空 ⇒ 退号前排班是满的 ⇒
        # 此前没有任何空号），但万一数据被手工改乱了，
        # 通用算法也不会发出一个撞号的序号。
        seq_no = _min_free_seq(conn, schedule_id, schedule["total_slots"])
        new_id = conn.execute(
            f"INSERT INTO appointments"
            f"(user_id, schedule_id, seq_no, status, source, created_at,"
            f" confirm_deadline) "
            f"VALUES (?, ?, ?, 'pending', 'promoted', {clock.now_sql()},"
            f" {clock.shift_sql(policy.CONFIRM_TIMEOUT_MINUTES)})",
            (head["user_id"], schedule_id, seq_no),
        ).lastrowid
        conn.execute(
            f"UPDATE waitlist SET status = 'promoted', resolved_at = {clock.now_sql()} "
            f" WHERE id = ?",
            (head["id"],),
        )
        conn.execute(
            "UPDATE schedules SET booked_count = booked_count + 1 WHERE id = ?",
            (schedule_id,),
        )
        # 截止时刻由 SQL 算出，这里再查回来——
        # 不自己用 Python 拼一个"现在 + 15 分钟"，是为了让返回给前端的值
        # 与写进库里的值**必然是同一个**，不存在两个时钟。
        deadline = conn.execute(
            "SELECT confirm_deadline FROM appointments WHERE id = ?", (new_id,)
        ).fetchone()[0]

        return {
            "user_name": head["name"],
            "seq_no": seq_no,
            "waitlist_id": head["id"],
            "appointment_id": new_id,
            "pending": True,
            "confirm_deadline": deadline,
        }, skipped


def confirm_promotion(
    conn: sqlite3.Connection, user_id: int, appointment_id: int
) -> dict:
    """加分项 2：确认一个「待确认」的号，`pending → booked`。

    被递补选中不会自动成号 —— 系统只是把号给他留着。
    这一步的意义在于：递补发生时人往往不在屏幕前，
    若默认成号，会出现"他根本不知道自己有个号，也没来"，
    于是这个号白空一场，而队列里其他人本来是能来的。

    前置条件：调用方应**先在一个已提交的事务里**结算过一次
    （见路由层 `confirm_appointment` 的注释，那里解释了为什么必须是独立事务）。
    结算与确认不在同一个事务里，是因为结算的结果（"他已超时"）正是本函数
    抛出 CONFIRM_TIMEOUT 的依据 —— 若两者同事务，抛异常会把结算一并回滚，
    于是号又变回 pending，用户刷一下页面发现自己居然还能确认。

    Raises:
        BizError: 预约不存在或不属于本人（404）、
            不处于待确认状态（409 NOT_PENDING）、
            已超时（409 CONFIRM_TIMEOUT）。
    """
    appt = conn.execute(
        "SELECT id, schedule_id, seq_no, status FROM appointments "
        " WHERE id = ? AND user_id = ?",
        (appointment_id, user_id),
    ).fetchone()
    if appt is None:
        raise BizError("APPOINTMENT_NOT_FOUND")

    if appt["status"] == "pending":
        conn.execute(
            "UPDATE appointments SET status = 'booked', confirm_deadline = NULL"
            " WHERE id = ?",
            (appointment_id,),
        )
        return {
            "appointment_id": appointment_id,
            "seq_no": appt["seq_no"],
            "message": f"确认成功，你的就诊序号是 {appt['seq_no']}",
        }

    # 走到这里说明不是 pending。分清"已经确认过"和"已经过期"，
    # 给用户两句不同的话 —— 前者是虚惊一场，后者是真的错过了。
    if appt["status"] == "booked":
        raise BizError("NOT_PENDING", "该预约已确认，无需重复操作")
    if appt["status"] == "cancelled":
        raise BizError("CONFIRM_TIMEOUT", "确认已超时，号已顺延给下一位")
    raise BizError("NOT_PENDING", "该预约不处于待确认状态")


def leave_waitlist(
    conn: sqlite3.Connection, user_id: int, waitlist_id: int
) -> dict:
    """主动放弃候补（退出排队）。

    **刻意不碰任何人的 position**：退出者的排序键原地作废即可，
    后面所有人看到的名次会自动前进一位 —— 因为名次是**现算**的
    （见 rank_in_waitlist 的注释）。
    数据库一行都不用改，这是"只增不压缩"策略的第二个受益点：
    第一处避开了撞键，这一处让"退出"变成 O(1) 且不会失败。

    Returns:
        {"waitlist_id": int, "schedule_id": int, "message": str}

    Raises:
        BizError: 记录不存在或不属于本人（404）、已放弃或已转正（409）。
    """
    row = conn.execute(
        "SELECT id, schedule_id, status FROM waitlist "
        " WHERE id = ? AND user_id = ?",
        (waitlist_id, user_id),
    ).fetchone()
    # 与退号同一个考量：查不到既有"根本不存在"，也有"存在但不是你的"，
    # 契约把两者都定为 404，免得被人用 id 试探出「这条记录存在」。
    if row is None:
        raise BizError("WAITLIST_NOT_FOUND")
    if row["status"] != "waiting":
        raise BizError("WAITLIST_ALREADY_LEFT")

    conn.execute(
        f"UPDATE waitlist SET status = 'cancelled', resolved_at = {clock.now_sql()} "
        f" WHERE id = ?",
        (waitlist_id,),
    )
    return {
        "waitlist_id": waitlist_id,
        "schedule_id": row["schedule_id"],
        "message": "已退出候补队列",
    }


def check_in(conn: sqlite3.Connection, user_id: int, appointment_id: int) -> dict:
    """加分项 3：就诊签到，`booked → completed`。

    为什么要签到这一步：没有它，系统永远不知道一个号**到底用没用掉**。
    于是"占了号不来"和"来了"在系统眼里是一回事，
    爽约统计、自动拉黑这两个加分项就都无从谈起。

    Raises:
        BizError: 预约不存在或不属于本人（404）、
            还没到就诊当天（409 CHECKIN_NOT_OPEN）、
            日期已过（409 CHECKIN_WINDOW_CLOSED）、
            已签到（409 ALREADY_CHECKED_IN）。
    """
    appt = conn.execute(
        "SELECT id, schedule_id, seq_no, status FROM appointments "
        " WHERE id = ? AND user_id = ?",
        (appointment_id, user_id),
    ).fetchone()
    if appt is None:
        raise BizError("APPOINTMENT_NOT_FOUND")

    # 先结算：排班日期一过，未签到的会被结算成 no_show。
    # 放在查状态之前，这样"刚刚过期"的预约在这里看到的是结算后的结果，
    # 而不是一个即将被改写的旧状态。
    settle_schedule(conn, appt["schedule_id"])

    schedule = guards.fetch_schedule(conn, appt["schedule_id"])
    status = conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()[0]

    if status == "completed":
        raise BizError("ALREADY_CHECKED_IN", "你已签到，无需重复操作")
    if status == "no_show":
        raise BizError("CHECKIN_WINDOW_CLOSED", "就诊日期已过，已按爽约处理")
    if status != "booked":
        # pending 要先确认才能签到 —— 一个还没确认的号谈不上"人到没到"。
        raise BizError("NOT_PENDING", "该预约需先确认才能签到")

    if schedule["work_date"] > schedule["today"]:
        raise BizError("CHECKIN_NOT_OPEN", "就诊当天才开放签到")
    if schedule["work_date"] < schedule["today"]:
        raise BizError("CHECKIN_WINDOW_CLOSED", "就诊日期已过，不能再签到")

    conn.execute(
        f"UPDATE appointments SET status = 'completed',"
        f" checked_in_at = {clock.now_sql()} "
        f" WHERE id = ?",
        (appointment_id,),
    )
    checked_at = conn.execute(
        "SELECT checked_in_at FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()[0]
    return {
        "appointment_id": appointment_id,
        "seq_no": appt["seq_no"],
        "checked_in_at": checked_at,
        "message": f"签到成功，你的就诊序号是 {appt['seq_no']}，请等候叫号",
    }


# ------------------------------------------------------------
# 惰性结算
#
# 转正确认超时与爽约统计都需要"到了某个时刻自动执行一段逻辑"。
# 常规做法是起一个后台定时任务，本项目刻意不这么做，理由见
# settle_schedule 的注释 —— 这里只放实现。
# ------------------------------------------------------------


def settle_schedule(conn: sqlite3.Connection, schedule_id: int) -> dict:
    """把一个排班的「僵尸状态」推进到应有的样子。

    处理两类：

        pending 逾期 —— 该确认的时候没确认 → 号作废，顺延给下一位
        就诊日已过且未签到 —— `booked` → `no_show`，爽约计数 +1

    **为什么是惰性的（有人来问才结算），而不是定时任务**：

    1. 演示项目里起一个后台线程，服务一重启它就没了，
       而它负责的恰恰是"清理僵尸状态" ——
       这类最不该丢的东西，放在最容易丢的地方。
    2. 没人来看的时候，超时与否**不影响任何人的任何决策**。
       早结算晚结算毫无差别，那就不该为它占用任何资源。
    3. 惰性结算有个额外的好处：它天然是幂等的，
       且天然不可能"漏掉"—— 因为每次读之前都结算，
       读到的一定是此刻的真相。

    这与 R3+ 那句「候补队列是一台延迟求值的规则检查器」是同一个思路，
    只是求值时机从"退号时"变成了"有人问时"。

    Returns:
        {"promotion_expired": int, "no_show": int, "blacklisted": int}
    """
    expired = _settle_expired_pending(conn, schedule_id)
    no_show, blacklisted = _settle_no_shows(conn, schedule_id)
    return {
        "promotion_expired": expired,
        "no_show": no_show,
        "blacklisted": blacklisted,
    }


def settle_all(conn: sqlite3.Connection) -> dict:
    """结算全部需要结算的排班，供 `POST /api/admin/settle` 与惰性触发点调用。

    只挑出**确实可能有事发生**的排班，而不是遍历全部：
        有 pending 记录的，或
        有 booked 记录但日期已过的
    其它排班扫了也是空转。

    Returns:
        {"promotion_expired": int, "no_show": int, "blacklisted": int,
         "schedules": int}
    """
    rows = conn.execute(
        "SELECT DISTINCT s.id FROM schedules s "
        "  JOIN appointments a ON a.schedule_id = s.id "
        " WHERE a.status = 'pending' "
        f"    OR (a.status = 'booked' AND s.work_date < {clock.today_sql()})",
    ).fetchall()

    total = {"promotion_expired": 0, "no_show": 0, "blacklisted": 0}
    for r in rows:
        part = settle_schedule(conn, r[0])
        for key in total:
            total[key] += part[key]
    total["schedules"] = len(rows)
    return total


def _settle_expired_pending(conn: sqlite3.Connection, schedule_id: int) -> int:
    """作废逾期的 pending 预约，并把号顺延给下一位。

    Returns:
        作废的条数。
    """
    rows = conn.execute(
        "SELECT id, user_id FROM appointments "
        " WHERE schedule_id = ? AND status = 'pending' "
        "   AND confirm_deadline IS NOT NULL "
        f"   AND confirm_deadline < {clock.now_sql()}",
        (schedule_id,),
    ).fetchall()

    for r in rows:
        _expire_pending(conn, r["id"], r["user_id"])
        # 号空出来了，立刻通知下一位（他自己也会拿到一个 pending）。
        # 不在这里循环到底，交给 _promote_next 那条 while 去顺延，
        # "找下一个合格的人"这件事全项目只有那一段代码。
        _promote_next(conn, schedule_id)
    return len(rows)


def settle_pending_of_user(conn: sqlite3.Connection, user_id: int) -> int:
    """作废某个用户逾期的 pending 预约（不知道他排的是哪个排班时用）。

    只扫这一个人的记录，不去动别人 ——
    `confirm_promotion` 被调用时，我们只需要关心发起请求的这个人
    手里的号有没有过期，全表扫描是白费力气。

    Returns:
        作废的条数。
    """
    rows = conn.execute(
        "SELECT id, schedule_id FROM appointments "
        " WHERE user_id = ? AND status = 'pending' "
        "   AND confirm_deadline IS NOT NULL "
        f"   AND confirm_deadline < {clock.now_sql()}",
        (user_id,),
    ).fetchall()

    for r in rows:
        _expire_pending(conn, r["id"], user_id)
        _promote_next(conn, r["schedule_id"])
    return len(rows)


def _expire_pending(
    conn: sqlite3.Connection, appointment_id: int, user_id: int
) -> None:
    """把一个逾期的 pending 预约作废，并把号源还回去。

    状态置 `cancelled` 而不是新增一种 `expired`：
    appointments 的 status 已经够多了，而"用户自己退的"与"系统作废的"
    在这个层面的后果完全一致 —— 都是这个号不再属于他。
    要区分谁动的手，看 `source='promoted'` 就够了：
    递补来的号被作废，只可能是超时。
    """
    schedule_id = conn.execute(
        "SELECT schedule_id FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()[0]

    conn.execute(
        f"UPDATE appointments SET status = 'cancelled',"
        f" cancelled_at = {clock.now_sql()} "
        f" WHERE id = ? AND status = 'pending'",
        (appointment_id,),
    )
    conn.execute(
        "UPDATE schedules SET booked_count = booked_count - 1 WHERE id = ?",
        (schedule_id,),
    )
    # 对应的候补记录从 promoted 改回 expired：
    # promoted 说的是"曾经被选中"，expired 说的是"最终没成"。
    # 将来统计"递补成功率"时，这两个值是不同的分子分母，值得分清楚。
    conn.execute(
        f"UPDATE waitlist SET status = 'expired', resolved_at = {clock.now_sql()} "
        f" WHERE id = (SELECT id FROM waitlist "
        f"              WHERE user_id = ? AND schedule_id = ?"
        f"                AND status = 'promoted' "
        f"              ORDER BY resolved_at DESC LIMIT 1)",
        (user_id, schedule_id),
    )


def _settle_no_shows(
    conn: sqlite3.Connection, schedule_id: int
) -> tuple[int, int]:
    """结算爽约：就诊日已过仍未签到 → `no_show`，并累计爽约次数。

    `no_show` **仍然占着那个号**（它在 `ACTIVE_STATUS_CLAUSE` 里），
    所以这里刻意不动 `booked_count` ——
    这个人没来，但那个号确实被他占掉了，医生那边的叫号单上有他的名字。
    把它当"已释放"放出去，就会有两个人拿着同一个号。

    Returns:
        (爽约人数, 本次新被拉黑的人数)
    """
    rows = conn.execute(
        "SELECT a.id, a.user_id, a.status FROM appointments a "
        "  JOIN schedules s ON s.id = a.schedule_id "
        " WHERE a.schedule_id = ? AND a.status IN ('booked', 'pending') "
        f"   AND s.work_date < {clock.today_sql()}",
        (schedule_id,),
    ).fetchall()

    no_show_count = 0
    blacklisted = 0

    for r in rows:
        if r["status"] == "pending":
            # 排班都结束了还没确认 → 作废即可，**不算爽约**。
            # 他从没确认过，也就谈不上"占了号没来"；
            # 记一笔爽约会让一个只是没看手机的人背上污点。
            _expire_pending(conn, r["id"], r["user_id"])
            continue

        conn.execute(
            "UPDATE appointments SET status = 'no_show' WHERE id = ?", (r["id"],)
        )
        no_show_count += 1
        if _bump_no_show(conn, r["user_id"]):
            blacklisted += 1

    return no_show_count, blacklisted


def _bump_no_show(conn: sqlite3.Connection, user_id: int) -> bool:
    """爽约计数 +1，达到阈值就拉黑。

    Returns:
        本次是否**新**把某个人拉黑了（已达标的返回 False，
        否则每结算一次都会重复报一次"新增拉黑 1 人"）。
    """
    row = conn.execute(
        "SELECT status FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    # 已经被拉黑的人不再重复计数：他的号约不出来，不存在新的爽约。
    if row is None or row["status"] == "blacklisted":
        return False

    conn.execute(
        "UPDATE users SET no_show_count = no_show_count + 1 WHERE id = ?",
        (user_id,),
    )
    new_count = conn.execute(
        "SELECT no_show_count FROM users WHERE id = ?", (user_id,)
    ).fetchone()[0]

    if new_count >= policy.NO_SHOW_BLACKLIST_THRESHOLD:
        conn.execute(
            "UPDATE users SET status = 'blacklisted' WHERE id = ?", (user_id,)
        )
        return True
    return False
