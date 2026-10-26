"""R3 验收单 · 可执行版。

这里每一条都对应 `PROJECT_STATE.md`「R3 验收结果」里的一行，
不是顺手堆的单元测试 —— 验收清单写过什么，这里就必须能跑出什么。
动 R4 前端之前先跑它，它是回归底线。

分组与验收清单一致，外加本次补的几项（E / F / G / H 组）。
"""

from __future__ import annotations


# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------


def _h(user_id: int) -> dict:
    """登录态请求头。"""
    return {"X-User-Id": str(user_id)}


def _book(client, user_id: int, schedule_id: int):
    return client.post(
        "/api/appointments",
        json={"schedule_id": schedule_id},
        headers=_h(user_id),
    )


def _cancel(client, user_id: int, appointment_id: int):
    return client.delete(f"/api/appointments/{appointment_id}", headers=_h(user_id))


def _leave(client, user_id: int, waitlist_id: int):
    return client.delete(f"/api/waitlist/{waitlist_id}", headers=_h(user_id))


def _data(resp) -> dict:
    """取成功响应的 data，顺带守住成功体的形状。"""
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    return body["data"]


def assert_error(resp, status: int, code: str) -> None:
    """断言失败响应的状态码与业务码。

    这一条同时守着「对外只有一种错误形状」那条契约：
    哪个接口漏了统一处理器，这里立刻会红。
    """
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is False, body
    assert body["error"]["code"] == code, body
    # 有 code 就必须有话说，不能让前端拿到一个空文案
    assert body["error"]["message"]


# ------------------------------------------------------------
# A. 预约主流程
# ------------------------------------------------------------


def test_book_gives_min_free_seq_not_max_plus_one(client, fx):
    """12 个号全满后空出 1 号，再约的人拿到 1 而不是 13。

    决策 3 的直接证据：MAX+1 会撞破 CHECK(seq_no <= total_slots)。
    """
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=12)
    pairs = fx.fill(sch, 12)
    fx.release(pairs[0][0])  # 把 1 号空出来

    assert _data(_book(client, fx.user("赵四"), sch))["seq_no"] == 1


def test_book_when_full_goes_to_waitlist(client, fx):
    """号满时预约不报错，转入候补并报出名次。"""
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    fx.fill(sch, 1)

    assert _data(_book(client, fx.user("甲"), sch))["position"] == 1
    assert _data(_book(client, fx.user("乙"), sch))["position"] == 2


def test_my_page_reports_rank_not_raw_key(client, fx):
    """对外给的是名次，库里存的是排序键，两者常常不相等。

    队首转正后排序键不压缩，所以库里可能留着 1、2、3 而实际只剩第 3 个人，
    对用户报的必须是"你排第 1 位"。
    """
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    [(appt0, holder0)] = fx.fill(sch, 1)

    yi = fx.user("乙")
    jia = fx.user("甲")
    _book(client, jia, sch)  # 队首
    wl_yi = _data(_book(client, yi, sch))["waitlist_id"]

    _cancel(client, holder0, appt0)  # 甲转正出队

    # 乙的排序键一动没动，对外名次却从 2 变成了 1
    assert fx.raw_position(wl_yi) == 2
    mine = client.get("/api/my/appointments", headers=_h(yi)).json()["data"]
    assert mine["waitlist"][0]["position"] == 1


# ------------------------------------------------------------
# B. 退号递补
# ------------------------------------------------------------


def test_cancel_promotes_head_and_inherits_seq(client, fx):
    """退号 → 队首当场转正，接过的是被释放的那个号。"""
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    [(appt0, holder0)] = fx.fill(sch, 1)
    _book(client, fx.user("甲"), sch)

    d = _data(_cancel(client, holder0, appt0))
    assert d["released_seq_no"] == 1
    assert d["promoted"]["user_name"] == "甲"
    assert d["promoted"]["seq_no"] == 1  # 继承，不是另发一个新号


def test_cancel_with_empty_queue_returns_null(client, fx):
    """队列没人时 promoted 为 null，号源回到可约池。"""
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    [(appt0, holder0)] = fx.fill(sch, 1)

    d = _data(_cancel(client, holder0, appt0))
    assert d["promoted"] is None
    assert fx.booked_count(sch) == 0


def test_promoted_appointment_is_recorded_as_promoted(client, fx, conn):
    """递补来的预约 source 记为 promoted。

    加分项的「多少人靠候补看上病」要靠它统计，
    因此这个值必须在转正的那一刻就写对。
    """
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    [(appt0, holder0)] = fx.fill(sch, 1)
    _book(client, fx.user("甲"), sch)

    new_id = _data(_cancel(client, holder0, appt0))["promoted"]["appointment_id"]
    source = conn.execute(
        "SELECT source FROM appointments WHERE id = ?", (new_id,)
    ).fetchone()[0]
    assert source == "promoted"


# ------------------------------------------------------------
# C. 防黄牛三条 + 两条边界
# ------------------------------------------------------------


def test_same_slot_twice_rejected(client, fx):
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=5)
    u = fx.user("甲")
    _book(client, u, sch)
    assert_error(_book(client, u, sch), 409, "ALREADY_BOOKED_THIS_SLOT")


def test_daily_dept_limit_rejected(client, fx):
    """同一天同一科室限 1 个号，换个医生也不行 —— 限的是科室不是人。"""
    s1 = fx.schedule(fx.doctor("李医生", "内科"), period="morning", total=5)
    s2 = fx.schedule(fx.doctor("王医生", "内科"), period="afternoon", total=5)

    u = fx.user("甲")
    _book(client, u, s1)
    assert_error(_book(client, u, s2), 409, "DAILY_DEPT_LIMIT")


def test_already_waiting_rejected(client, fx):
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    fx.fill(sch, 1)
    u = fx.user("甲")
    _book(client, u, sch)
    assert_error(_book(client, u, sch), 409, "ALREADY_WAITING")


def test_different_dept_same_day_allowed(client, fx):
    """同天不同科室是允许的 —— 这是规则二的反向边界。"""
    s1 = fx.schedule(fx.doctor("李医生", "内科"), total=5)
    s2 = fx.schedule(fx.doctor("王医生", "外科"), period="afternoon", total=5)

    u = fx.user("甲")
    _book(client, u, s1)
    assert _data(_book(client, u, s2))["type"] == "booked"


def test_waitlist_not_counted_toward_daily_limit(client, fx):
    """排队不占号源，所以不受每日限 1 号的约束。

    候补者什么都没拿到，把「同科室排了两位医生」也算违规，
    会让患者白白失去机会，与便民目标相反。
    """
    s1 = fx.schedule(fx.doctor("李医生", "内科"), total=5)
    s2 = fx.schedule(fx.doctor("王医生", "内科"), period="afternoon", total=1)
    fx.fill(s2, 1)

    u = fx.user("甲")
    _book(client, u, s1)
    assert _data(_book(client, u, s2))["type"] == "waitlisted"


# ------------------------------------------------------------
# D. 被拒无残留 + 记账一致性
# ------------------------------------------------------------


def test_rejected_booking_leaves_no_trace(client, fx):
    """规则拦截时事务必须整个回滚：既没留下预约，也没动 booked_count。"""
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=5)
    u = fx.user("甲")
    _book(client, u, sch)

    for _ in range(3):
        assert_error(_book(client, u, sch), 409, "ALREADY_BOOKED_THIS_SLOT")

    assert fx.booked_count(sch) == 1
    assert fx.real_occupied(sch) == 1


def test_ledger_matches_reality_after_full_round(client, fx):
    """走完 预约 → 候补 → 退号 → 转正 一轮，记账必须与实际逐一吻合。"""
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=2)
    [(appt1, holder1), _] = fx.fill(sch, 2)  # 两个号都有人，排班已满

    _book(client, fx.user("丙"), sch)  # 满 → 丙排到第 1 位
    _cancel(client, holder1, appt1)  # 退 1 号 → 丙递补，接过的正是 1 号

    assert fx.booked_count(sch) == fx.real_occupied(sch) == 2
    assert fx.seq_nos(sch) == [1, 2]  # 序号严格 1..N，没有空洞也没有越界


# ------------------------------------------------------------
# E. 递补顺延（本次补的核心）
# ------------------------------------------------------------


def test_promotion_skips_head_who_holds_same_dept_slot(client, fx):
    """队首已持有同天同科室的号 → 顺延下一位，他本人标记 expired。

    这是本次补的最关键一条。不补的话，规规矩矩走 UI 就能让人
    同时握着同天同科室的两个号，规则二形同虚设。
    """
    s1 = fx.schedule(fx.doctor("李医生", "内科"), period="morning", total=5)
    s2 = fx.schedule(fx.doctor("王医生", "内科"), period="afternoon", total=1)
    [(appt2, holder2)] = fx.fill(s2, 1)  # s2 满

    jia = fx.user("甲")
    yi = fx.user("乙")
    wl_jia = _data(_book(client, jia, s2))["waitlist_id"]  # 甲排第 1 位
    _book(client, yi, s2)  # 乙排第 2 位

    # 甲顺手把同天同科室的 s1 约走了（排队不占额度，所以约得到）
    assert _data(_book(client, jia, s1))["type"] == "booked"

    # 此刻 s2 有人退号：甲已不符合规则，应当顺延给乙
    d = _data(_cancel(client, holder2, appt2))
    assert d["promoted"]["user_name"] == "乙"
    assert d["promoted"]["seq_no"] == 1
    assert fx.waitlist_status(wl_jia) == "expired"


def test_promotion_skips_the_canceller_himself(client, fx):
    """退号者本人排在队里，也不能把号原样拿回去。

    否则他先排队再退号，就能反复"退 → 递补给自己"，把这个号永久锁在手里。
    """
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    [(appt0, holder0)] = fx.fill(sch, 1)

    wl_self = fx.enqueue(holder0, sch, 1)  # 他悄悄排到了队首
    _book(client, fx.user("乙"), sch)  # 乙排第 2 位

    d = _data(_cancel(client, holder0, appt0))
    assert d["promoted"]["user_name"] == "乙"
    assert fx.waitlist_status(wl_self) == "expired"


def test_direct_booking_removes_own_waitlist_entry(client, fx):
    """排队的人后来直接约到号 → 那条候补记录必须自动作废。

    留着就是一张死条目，将来被轮询到时会给已持证的人再发一张，
    直接撞上 ux_appt_user_slot 变成 500。
    """
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=2)
    [(appt1, holder1)] = fx.fill(sch, 1)  # 只占 1 号，2 号空着

    u = fx.user("甲")
    wl = fx.enqueue(u, sch, 1)  # 他此前正在排队

    assert _data(_book(client, u, sch))["seq_no"] == 2
    assert fx.waitlist_status(wl) == "cancelled"

    # 死条目已清除，之后退号不会撞索引
    _cancel(client, holder1, appt1)
    assert fx.real_occupied(sch) == 1


# ------------------------------------------------------------
# F. 放弃候补
# ------------------------------------------------------------


def test_leave_waitlist_advances_others(client, fx):
    """退出者不改任何人的排序键，后面的人名次自动前进。"""
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    fx.fill(sch, 1)

    jia = fx.user("甲")
    yi = fx.user("乙")
    wl_jia = _data(_book(client, jia, sch))["waitlist_id"]
    wl_yi = _data(_book(client, yi, sch))["waitlist_id"]

    assert _data(_leave(client, jia, wl_jia))["schedule_id"] == sch
    assert fx.waitlist_status(wl_jia) == "cancelled"

    # 乙的排序键一动没动（没有压缩），对外名次却从 2 变成了 1
    assert fx.raw_position(wl_yi) == 2
    mine = client.get("/api/my/appointments", headers=_h(yi)).json()["data"]
    assert mine["waitlist"][0]["position"] == 1


def test_leave_waitlist_twice_rejected(client, fx):
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    fx.fill(sch, 1)

    u = fx.user("甲")
    wl = _data(_book(client, u, sch))["waitlist_id"]
    _leave(client, u, wl)
    assert_error(_leave(client, u, wl), 409, "WAITLIST_ALREADY_LEFT")


def test_leave_others_waitlist_not_found(client, fx):
    """拿别人的候补 id 去删，与删一条不存在的记录得到同一结果。

    统一成 404 是为了不让「这条记录存在但不属于你」这个事实泄露出去。
    """
    sch = fx.schedule(fx.doctor("李医生", "内科"), total=1)
    fx.fill(sch, 1)

    wl = _data(_book(client, fx.user("甲"), sch))["waitlist_id"]
    assert_error(_leave(client, fx.user("乙"), wl), 404, "WAITLIST_NOT_FOUND")
    assert fx.waitlist_status(wl) == "waiting"  # 没有被搞掉


# ------------------------------------------------------------
# G. 响应形状
# ------------------------------------------------------------


def test_type_error_uses_uniform_error_shape(client, fx):
    """schedule_id 传字符串时，返回的也必须是我们的形状。

    FastAPI 原生给的是 422 + {"detail": [...]}，
    那会让前端 api.js「只判断 ok」这一套写法直接失效。

    必须先登录才能看到这个结果：鉴权依赖的解析顺序在参数校验之前，
    没登录时拿到的是 401 —— 先确认你是谁，再看你说得对不对。
    """
    u = fx.user("甲")
    r = client.post(
        "/api/appointments", json={"schedule_id": "abc"}, headers=_h(u)
    )
    assert_error(r, 400, "INVALID_PARAM")

    r = client.post("/api/appointments", json={}, headers=_h(u))
    assert_error(r, 400, "INVALID_PARAM")


# ------------------------------------------------------------
# H. 过期日期
# ------------------------------------------------------------


def test_past_schedule_cannot_be_booked(client, fx):
    """昨天的号约不了 —— 号源是带时间属性的资源。"""
    past = fx.schedule(fx.doctor("李医生", "内科"), offset_days=-1, total=5)
    assert_error(_book(client, fx.user("甲"), past), 409, "SCHEDULE_EXPIRED")


def test_today_schedule_can_still_be_booked(client, fx):
    """今天的号必须还能约：现场挂号是真实业务，不能一刀切。

    判定用的是严格小于，所以这条是上一条的反向边界。
    """
    today = fx.schedule(fx.doctor("李医生", "内科"), offset_days=0, total=5)
    assert _data(_book(client, fx.user("甲"), today))["type"] == "booked"


# ------------------------------------------------------------
# I. 种子数据自检
# ------------------------------------------------------------


def test_seed_data_is_healthy(seeded_client, seeded_conn):
    """种子数据本身不能坏。

    演示数据是为了好看，会随时调整；这条测试保证调整之后
    四种状态仍然齐全、记账仍然对得上 —— 演示前一跑就知道有没有穿帮。
    """
    schedules = seeded_client.get("/api/schedules").json()["data"]
    assert {s["status"] for s in schedules} == {"open", "full", "waiting", "closed"}

    for s in schedules:
        real = seeded_conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE schedule_id = ?"
            " AND status IN ('booked','completed','no_show')",
            (s["schedule_id"],),
        ).fetchone()[0]
        assert s["booked_count"] == real
        assert s["booked_count"] <= s["total_slots"]
