"""R5 五个加分项的测试。

本文件与前面四个测试文件有一个根本区别：**断言的是"时间过去之后会怎样"**。

    转正确认超时是 15 分钟，退号冷却是 10 分钟，取消时限是 2 小时 ——
    这些都不可能让测试真的去等。
    于是全靠 `app/services/clock.py` 那个可注入时钟：
    `clock.advance(16)` 一下，整个系统（包括 SQL 里的 now）就到了 16 分钟后。

    有一条 `test_mutation_*` 专门用来证明这件事真的生效了 ——
    把超时阈值改成十万分钟，同样的 advance(16) 就不该再触发任何事。
    若它照样"过期"了，说明超时根本不是由时钟决定的，
    那本文件里所有跟时间有关的断言就全是自欺欺人。

造场景一律用真实业务流程（走 HTTP），不直接改库里的状态字段。
唯一例外是 `fx.fill()` / `fx.enqueue()` 这类 conftest 里已经说明过的旁路手段，
它们的作用是"把系统摆到某个初始局面"，而不是"伪造结果"。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import clock, policy

# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------


def _at(day: str, hhmm: str) -> datetime:
    """把「日期 + 时分」拼成一个 datetime，供 clock.freeze 使用。

    `day` 一律由 `fx.date(n)` 给出（它是用真实 SQL 时钟算的），
    所以即便系统时钟已经被冻结，也能算出"真实的第 n 天"。
    """
    return datetime.strptime(f"{day} {hhmm}:00", "%Y-%m-%d %H:%M:%S")


def _uid(client_kwargs: dict) -> dict:
    return client_kwargs


def _as(user_id: int) -> dict:
    """构造请求头：本项目不做真实鉴权，只认 X-User-Id。"""
    return {"X-User-Id": str(user_id)}


def _book(client, user_id: int, schedule_id: int):
    return client.post(
        "/api/appointments", json={"schedule_id": schedule_id}, headers=_as(user_id)
    )


def _appt_status(conn, appointment_id: int) -> str:
    return conn.execute(
        "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()[0]


def _waitlist_status(conn, user_id: int, schedule_id: int) -> str:
    return conn.execute(
        "SELECT status FROM waitlist WHERE user_id = ? AND schedule_id = ?",
        (user_id, schedule_id),
    ).fetchone()[0]


def _user_row(conn, user_id: int):
    return conn.execute(
        "SELECT status, no_show_count FROM users WHERE id = ?", (user_id,)
    ).fetchone()


# ------------------------------------------------------------
# 加分项 2：转正确认超时
#
# 这一组是全文件的重点：它验证的不只是"能确认"，
# 更是"不确认会怎样" —— 后者才是这个加分项的全部意义。
# ------------------------------------------------------------


def test_promotion_is_pending_until_confirmed(client, fx, conn):
    """递补出来的是 pending，确认之后才真正 booked。

    R5 之前递补直接给 booked，那意味着系统替一个不在场的人做了决定：
    他可能根本不知道自己有个号，于是白白空着。
    """
    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    appt_id, holder = fx.fill(sch, 1)[0]
    a = fx.user("甲")
    fx.user("乙")  # 乙排在后面，本条用不到，但让队列不止一个人更真实
    fx.enqueue(a, sch, position=1)

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(holder))
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["promoted"]["user_name"] == "甲"
    assert data["promoted"]["pending"] is True
    assert data["promoted"]["confirm_deadline"] is not None

    new_id = data["promoted"]["appointment_id"]
    # 号被占住了：pending 也计入 booked_count，
    # 否则这 15 分钟里号会被别人直接约走，"留给他"就成了空话。
    assert _appt_status(conn, new_id) == "pending"
    assert fx.booked_count(sch) == 1
    assert fx.real_occupied(sch) == 1

    # 确认之后才变成 booked
    resp = client.post(
        f"/api/appointments/{new_id}/confirm", headers=_as(a)
    )
    assert resp.status_code == 200
    assert _appt_status(conn, new_id) == "booked"
    # 确认之后 deadline 清空：它只属于"待确认"这个阶段，
    # 留着会让人误以为这个号还会过期。
    assert conn.execute(
        "SELECT confirm_deadline FROM appointments WHERE id = ?", (new_id,)
    ).fetchone()[0] is None


def test_pending_expires_and_passes_to_next(client, fx, conn):
    """超时未确认 → 号作废 → 顺延给下一位（他也是 pending）。

    这是本加分项的核心路径：一个号不能因为某人没看手机就凭空消失，
    它必须真的流到队列里的下一个人手上。
    """
    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    appt_id, holder = fx.fill(sch, 1)[0]
    a = fx.user("甲")
    b = fx.user("乙")
    fx.enqueue(a, sch, position=1)
    fx.enqueue(b, sch, position=2)

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(holder))
    first_id = resp.json()["data"]["promoted"]["appointment_id"]

    # 时间过了 16 分钟（阈值 15），甲没有任何动作
    clock.advance(policy.CONFIRM_TIMEOUT_MINUTES + 1)

    # 触发惰性结算：谁来看一眼，状态就推进一次
    resp = client.get("/api/my/appointments", headers=_as(b))
    assert resp.status_code == 200

    assert _appt_status(conn, first_id) == "cancelled"
    # 甲的候补记录从 promoted 变 expired：
    # 前者说"曾经被选中"，后者说"最终没成"，统计递补成功率时是两个不同的数
    assert _waitlist_status(conn, a, sch) == "expired"

    # 号顺延给了乙，同样是个 pending
    row = conn.execute(
        "SELECT id FROM appointments"
        " WHERE schedule_id = ? AND user_id = ? AND status = 'pending'",
        (sch, b),
    ).fetchone()
    assert row is not None, "乙应当被递补，拿到一个待确认的号"
    assert fx.booked_count(sch) == 1, "号始终被占着，中间没有出现空窗"


def test_confirm_after_timeout_is_rejected(client, fx, conn):
    """超时后才想起来确认 → 409 CONFIRM_TIMEOUT，且号已经不在他手上了。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    appt_id, holder = fx.fill(sch, 1)[0]
    a = fx.user("甲")
    fx.enqueue(a, sch, position=1)

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(holder))
    new_id = resp.json()["data"]["promoted"]["appointment_id"]

    clock.advance(policy.CONFIRM_TIMEOUT_MINUTES + 1)

    resp = client.post(f"/api/appointments/{new_id}/confirm", headers=_as(a))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFIRM_TIMEOUT"
    assert _appt_status(conn, new_id) == "cancelled"


def test_confirm_twice_is_rejected(client, fx, conn):
    """确认过了再点一次 → NOT_PENDING，而不是"确认成功"再确认一遍。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    appt_id, holder = fx.fill(sch, 1)[0]
    a = fx.user("甲")
    fx.enqueue(a, sch, position=1)

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(holder))
    new_id = resp.json()["data"]["promoted"]["appointment_id"]
    assert client.post(
        f"/api/appointments/{new_id}/confirm", headers=_as(a)
    ).status_code == 200

    resp = client.post(f"/api/appointments/{new_id}/confirm", headers=_as(a))
    assert resp.status_code == 409
    # 已确认与已超时是两回事，给出的话也不一样：
    # 前者是虚惊一场，后者是真的错过了
    assert resp.json()["error"]["code"] == "NOT_PENDING"


def test_mutation_timeout_really_depends_on_clock(client, fx, conn, monkeypatch):
    """变异测试：证明上面那几条"超时"确实是时钟与阈值共同决定的。

    把确认时限改成十万分钟（约 69 天），**推进量仍按原来的 15+1 分钟** ——
    若那条 pending 照样作废了，说明超时判定根本没用到这个阈值，
    那么 `test_pending_expires_and_passes_to_next` 通过就只是巧合，
    可能是某段代码把它无条件作废了。

    推进量必须**写死成原阈值**，不能跟着 policy 走：
    第一版这里写的是 `advance(policy.CONFIRM_TIMEOUT_MINUTES + 1)`，
    而 policy 刚被改成十万，于是推进了十万零一分钟，刚好又超过了新的截止时刻 ——
    变异测试自己把要验证的东西给绕过去了，还绿着。
    这种"测试参数跟着被测参数一起变"的写法，
    会让变异测试变成一次毫无意义的重跑。

    与 test_status_consistency.py 里的变异测试是同一个道理：
    **一个从没红过的测试，没人知道它到底在不在测东西。**
    """
    original_timeout = policy.CONFIRM_TIMEOUT_MINUTES
    monkeypatch.setattr(policy, "CONFIRM_TIMEOUT_MINUTES", 100000)

    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    appt_id, holder = fx.fill(sch, 1)[0]
    a = fx.user("甲")
    fx.enqueue(a, sch, position=1)

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(holder))
    new_id = resp.json()["data"]["promoted"]["appointment_id"]

    # 只推进原本该过期的那 16 分钟
    clock.advance(original_timeout + 1)
    client.get("/api/my/appointments", headers=_as(a))

    # 阈值被放大后，同样的推进不足以让它过期
    assert _appt_status(conn, new_id) == "pending"


# ------------------------------------------------------------
# 加分项 1：退号冷却期
# ------------------------------------------------------------


def test_cooldown_blocks_immediate_rebooking(client, fx):
    """刚退的号不能立刻抢回来 —— 这正是冷却期要挡的行为。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=3)
    u = fx.user("退号人")

    resp = _book(client, u, sch)
    assert resp.status_code == 200
    appt_id = resp.json()["data"]["appointment_id"]

    assert client.delete(
        f"/api/appointments/{appt_id}", headers=_as(u)
    ).status_code == 200

    resp = _book(client, u, sch)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "COOLDOWN_ACTIVE"


def test_cooldown_expires_after_window(client, fx):
    """冷却期过了就能重新约 —— 它不是惩罚，只是一个时间窗。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=3)
    u = fx.user("退号人")

    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]
    client.delete(f"/api/appointments/{appt_id}", headers=_as(u))

    clock.advance(policy.CANCEL_COOLDOWN_MINUTES + 1)

    resp = _book(client, u, sch)
    assert resp.status_code == 200, "过了冷却期就应当可以重新预约"


def test_cooldown_does_not_block_other_schedules(client, fx):
    """冷却期只针对"同一个排班"，不该把人整片封死。"""
    doc = fx.doctor()
    s1 = fx.schedule(doc, offset_days=1, period="morning", total=3)
    s2 = fx.schedule(doc, offset_days=1, period="afternoon", total=3)
    u = fx.user("退号人")

    appt_id = _book(client, u, s1).json()["data"]["appointment_id"]
    client.delete(f"/api/appointments/{appt_id}", headers=_as(u))

    resp = _book(client, u, s2)
    assert resp.status_code == 200, "退了上午的号，下午的号照约不误"


# ------------------------------------------------------------
# 加分项 4：取消时限
# ------------------------------------------------------------


def test_cancel_is_allowed_when_far_from_start(client, fx):
    """离开诊还早 → 可以自助退号（这是正常情况，先守住它）。"""
    today = fx.date(0)
    sch = fx.schedule(fx.doctor(), offset_days=0, period="morning", total=5)
    u = fx.user("当日患者")

    # 冻结到今天 05:00，距 08:00 开诊还有 180 分钟
    clock.freeze(_at(today, "05:00"))
    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(u))
    assert resp.status_code == 200


def test_cancel_window_closes_near_start(client, fx, conn):
    """离开诊不足 2 小时 → 不能自助退号。

    冻结到今天 05:00 预约，再推进 2 小时到 07:00（距开诊 60 分钟）。
    """
    today = fx.date(0)
    sch = fx.schedule(fx.doctor(), offset_days=0, period="morning", total=5)
    u = fx.user("当日患者")

    clock.freeze(_at(today, "05:00"))
    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]

    clock.advance(policy.CANCEL_CUTOFF_MINUTES)  # 07:00，距开诊只剩 60 分钟

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(u))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CANCEL_WINDOW_CLOSED"
    # 关键：被拒绝之后，号**必须还在他手上**
    assert _appt_status(conn, appt_id) == "booked"


# ------------------------------------------------------------
# 加分项 3：就诊签到与爽约统计
# ------------------------------------------------------------


def test_check_in_on_visit_day(client, fx, conn):
    """就诊当天可以签到，签到后状态变 completed。"""
    today = fx.date(0)
    sch = fx.schedule(fx.doctor(), offset_days=0, period="morning", total=5)
    u = fx.user("签到人")

    clock.freeze(_at(today, "07:30"))
    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]

    resp = client.post(f"/api/appointments/{appt_id}/check-in", headers=_as(u))
    assert resp.status_code == 200
    assert resp.json()["data"]["seq_no"] == 1
    assert _appt_status(conn, appt_id) == "completed"
    assert conn.execute(
        "SELECT checked_in_at FROM appointments WHERE id = ?", (appt_id,)
    ).fetchone()[0] is not None


def test_check_in_too_early_is_rejected(client, fx, conn):
    """还没到就诊日 → 不能签到。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=5)
    u = fx.user("心急人")
    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]

    resp = client.post(f"/api/appointments/{appt_id}/check-in", headers=_as(u))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CHECKIN_NOT_OPEN"
    assert _appt_status(conn, appt_id) == "booked"


def test_check_in_twice_is_rejected(client, fx):
    """重复签到 → ALREADY_CHECKED_IN。"""
    today = fx.date(0)
    sch = fx.schedule(fx.doctor(), offset_days=0, period="morning", total=5)
    u = fx.user("签到人")

    clock.freeze(_at(today, "07:30"))
    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]
    assert client.post(
        f"/api/appointments/{appt_id}/check-in", headers=_as(u)
    ).status_code == 200

    resp = client.post(f"/api/appointments/{appt_id}/check-in", headers=_as(u))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ALREADY_CHECKED_IN"


# ------------------------------------------------------------
# 加分项 3 + 5：爽约累计与自动拉黑
# ------------------------------------------------------------


def _book_then_let_it_expire(client, fx, conn, user_id: int, schedule_id: int):
    """约一个号，然后把时钟推到"排班日期之后"，让它自然过期。"""
    _book(client, user_id, schedule_id)


def test_no_show_accumulates_and_blacklists(client, fx, conn):
    """爽约 2 次自动拉黑，之后连约号都被拒。

    两个排班刻意安排在不同科室：同天同科室限 1 个号，
    否则第二个号根本约不到，测试就变成了在测防黄牛规则。
    """
    d1 = fx.doctor("医生甲", "内科")
    d2 = fx.doctor("医生乙", "外科")
    s1 = fx.schedule(d1, offset_days=1, period="morning", total=5)
    s2 = fx.schedule(d2, offset_days=1, period="afternoon", total=5)
    u = fx.user("爽约人")

    _book(client, u, s1)
    _book(client, u, s2)

    # 推到后天：两个排班（都在明天）都已成为过去
    clock.freeze(_at(fx.date(2), "10:00"))

    resp = client.post("/api/admin/settle")
    assert resp.status_code == 200
    stats = resp.json()["data"]
    assert stats["no_show"] == 2
    assert stats["blacklisted"] == 1

    row = _user_row(conn, u)
    assert row["status"] == "blacklisted"
    assert row["no_show_count"] == 2

    # 被拉黑之后连新号都约不了
    s3 = fx.schedule(d1, offset_days=3, period="morning", total=5)
    resp = _book(client, u, s3)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "USER_BLACKLISTED"


def test_one_no_show_is_not_enough(client, fx, conn):
    """只爽约 1 次不拉黑 —— 一次可能是忘了、堵车、临时有事。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=5)
    u = fx.user("偶尔失手")
    _book(client, u, sch)

    clock.freeze(_at(fx.date(2), "10:00"))
    stats = client.post("/api/admin/settle").json()["data"]

    assert stats["no_show"] == 1
    assert stats["blacklisted"] == 0
    row = _user_row(conn, u)
    assert row["status"] == "active"
    assert row["no_show_count"] == 1


def test_no_show_still_holds_the_slot(client, fx, conn):
    """爽约的人仍然占着那个号 —— 医生那边的叫号单上有他的名字。

    把爽约当成"自动释放"，就会有两个人拿着同一个号。
    """
    sch = fx.schedule(fx.doctor(), offset_days=1, total=5)
    u = fx.user("爽约人")
    _book(client, u, sch)
    before = fx.booked_count(sch)

    clock.freeze(_at(fx.date(2), "10:00"))
    client.post("/api/admin/settle")

    assert fx.booked_count(sch) == before, "爽约不释放号源，计数不该变"
    assert fx.real_occupied(sch) == before, "no_show 仍然算作占着号"


# ------------------------------------------------------------
# 加分项 5：违规拉黑（手动入口）
# ------------------------------------------------------------


def test_manual_blacklist_blocks_booking(client, fx, conn):
    """手动拉黑后约不到号；解除之后又能约了。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=5)
    u = fx.user("被拉黑人")

    resp = client.post(f"/api/admin/users/{u}/blacklist")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "blacklisted"

    resp = _book(client, u, sch)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "USER_BLACKLISTED"

    assert client.delete(
        f"/api/admin/users/{u}/blacklist"
    ).status_code == 200
    assert _book(client, u, sch).status_code == 200


def test_blacklisted_user_can_still_leave_waitlist(client, fx):
    """被拉黑的人仍能退出排队 —— 拉黑约束的是"得到新号"，不是"放弃"。

    把放弃的权利也一起没收，队列里就会留下一个永远不会出队的死条目。
    """
    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    fx.fill(sch, 1)
    u = fx.user("被拉黑人")
    wl_id = fx.enqueue(u, sch, position=1)

    client.post(f"/api/admin/users/{u}/blacklist")

    resp = client.delete(f"/api/waitlist/{wl_id}", headers=_as(u))
    assert resp.status_code == 200


# ------------------------------------------------------------
# 惰性结算
# ------------------------------------------------------------


def test_settle_is_idempotent(client, fx, conn):
    """结算跑两遍，第二遍什么也不做。

    幂等性是惰性结算能成立的前提：它被三处业务入口顺带调用，
    同一秒内被触发十次也不该产生十倍的效果。
    """
    sch = fx.schedule(fx.doctor(), offset_days=1, total=5)
    u = fx.user("爽约人")
    _book(client, u, sch)

    clock.freeze(_at(fx.date(2), "10:00"))

    first = client.post("/api/admin/settle").json()["data"]
    second = client.post("/api/admin/settle").json()["data"]

    assert first["no_show"] == 1
    assert second["no_show"] == 0, "第二遍不该重复记一次爽约"
    assert _user_row(conn, u)["no_show_count"] == 1


def test_my_appointments_exposes_action_flags(client, fx):
    """「我的预约」给出 can_* 标志，前端不自己按 status 推断。"""
    today = fx.date(0)
    sch = fx.schedule(fx.doctor(), offset_days=0, period="morning", total=5)
    u = fx.user("当日患者")

    clock.freeze(_at(today, "05:00"))
    _book(client, u, sch)

    item = client.get("/api/my/appointments", headers=_as(u)).json()["data"][
        "appointments"
    ][0]
    assert item["status"] == "booked"
    assert item["can_check_in"] is True, "就诊当天应当可以签到"
    assert item["can_cancel"] is True, "距开诊 3 小时应当可以退号"
    assert item["can_confirm"] is False
    assert item["status_label"] == "待就诊"

    # 推进到距开诊只剩 1 小时：不能退了，但仍能签到
    clock.advance(policy.CANCEL_CUTOFF_MINUTES)
    item = client.get("/api/my/appointments", headers=_as(u)).json()["data"][
        "appointments"
    ][0]
    assert item["can_cancel"] is False
    assert item["can_check_in"] is True


def test_pending_shows_deadline_in_my_appointments(client, fx):
    """待确认的号在「我的预约」里能看到截止时刻，前端据此做倒计时。"""
    sch = fx.schedule(fx.doctor(), offset_days=1, total=1)
    appt_id, holder = fx.fill(sch, 1)[0]
    a = fx.user("甲")
    fx.enqueue(a, sch, position=1)

    client.delete(f"/api/appointments/{appt_id}", headers=_as(holder))

    item = client.get("/api/my/appointments", headers=_as(a)).json()["data"][
        "appointments"
    ][0]
    assert item["status"] == "pending"
    assert item["status_label"] == "待确认"
    assert item["can_confirm"] is True
    assert item["confirm_deadline"] is not None


@pytest.mark.parametrize(
    "period,hhmm",
    [("morning", "05:30"), ("afternoon", "11:30"), ("evening", "16:00")],
)
def test_cancel_cutoff_uses_each_period_start(client, fx, period, hhmm):
    """三个时段的开诊时刻不同，取消时限的判定也随之不同。

    时刻表在 policy.PERIOD_START_TIMES：8:00 / 14:00 / 18:30。
    冻结到"距各自开诊 2.5 小时"，三种情况下都还能退。
    """
    today = fx.date(0)
    sch = fx.schedule(fx.doctor(), offset_days=0, period=period, total=5)
    u = fx.user("当日患者")

    clock.freeze(_at(today, hhmm))
    appt_id = _book(client, u, sch).json()["data"]["appointment_id"]

    resp = client.delete(f"/api/appointments/{appt_id}", headers=_as(u))
    assert resp.status_code == 200, f"{period} 距开诊 2.5 小时，应当允许退号"
