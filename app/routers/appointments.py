"""预约、退号、我的预约。

本模块只做三件事，业务判断一行都不在这里：

    1. 校验用户已登录（交给 deps.current_user）
    2. **管住事务边界**（BEGIN IMMEDIATE / COMMIT / ROLLBACK）
    3. 把 service 层返回的结果装进契约规定的结构

**为什么事务边界在路由层而不是 service 层**：
    service 层只管在事务里做事，开与关由调用方决定。
    这样 R5 写并发测试时，可以直接调 service 层的函数，
    在外面套事务、做断言、再回滚，不需要起 HTTP 服务。
    若事务开在 service 内部，测试就没法插手了。

整个项目最难的一段（退号递补）在 services/booking.py 里，这里只是它的入口。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app import db, deps
from app.errors import BizError
from app.services import booking, clock, guards, policy
from app.schemas import (
    BlacklistData,
    BookData,
    BookRequest,
    CancelData,
    CheckInData,
    ConfirmData,
    LeaveWaitlistData,
    MyData,
    OkResponse,
    SettleData,
)
from app.services.slot_status import (
    appointment_status_label,
    period_label,
    source_label,
    waitlist_status_label,
)

router = APIRouter(prefix="/api", tags=["预约与候补"])


@router.post("/appointments", response_model=OkResponse[BookData])
def create_appointment(
    body: BookRequest,
    user: sqlite3.Row = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """预约一个号。号满自动转候补，两种都是 200。

    号满不是错误，是"换个方式继续"—— 所以 SLOT_FULL 不在错误码表里
    （见 docs/API.md 第 3 节末尾）。
    """
    with db.transaction(conn):
        result = booking.create_booking(conn, user["id"], body.schedule_id)
    return {"ok": True, "data": result}


@router.delete(
    "/appointments/{appointment_id}", response_model=OkResponse[CancelData]
)
def cancel_appointment(
    appointment_id: int,
    user: sqlite3.Row = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """退号。有候补时，队首当场自动转正。

    退号与递补必须原子：只完成一半会变成「号空着但没人接」，
    或者「有人拿到了不存在的号」。
    """
    with db.transaction(conn):
        result = booking.cancel_booking(conn, user["id"], appointment_id)
    return {"ok": True, "data": result}


@router.delete("/waitlist/{waitlist_id}", response_model=OkResponse[LeaveWaitlistData])
def leave_waitlist(
    waitlist_id: int,
    user: sqlite3.Row = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """放弃候补，退出排队。

    没有这个接口，用户一旦排上队就再也无法反悔 —— 排队是一回事，
    被锁住是另一回事。它同时也让候补的生命周期完整了：
        排队 →（顺延 / 转正 / 主动放弃）
    """
    with db.transaction(conn):
        result = booking.leave_waitlist(conn, user["id"], waitlist_id)
    return {"ok": True, "data": result}


@router.get("/my/appointments", response_model=OkResponse[MyData])
def my_appointments(
    user: sqlite3.Row = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """我的预约页：正式预约 + 候补队列一次性返回。

    一个 GET 却套了写事务 —— 因为 R5 起它会**顺带结算一次**：
    打开这个页面时先把"确认超时"和"过期爽约"结算掉，再告诉用户现在的状态。

    不这么做的话，用户会看到一个"待确认"的卡片，
    而它其实早就超时作废了 —— 页面显示的是过去的真相。
    惰性结算的触发点里，这一个最贴近用户：他打开页面就是想知道"现在怎么样了"。

    另外它与上面两个写接口共用同一个连接，
    这一点由 FastAPI 的依赖缓存保证（见 app/deps.py 的注释）。
    """
    with db.transaction(conn):
        booking.settle_all(conn)
        data = {
            "appointments": _my_appointments(conn, user["id"]),
            "waitlist": _my_waitlist(conn, user["id"]),
        }
    return {"ok": True, "data": data}


# ------------------------------------------------------------
# 加分项接口（契约见 docs/API.md 第 5 节）
# ------------------------------------------------------------


@router.post(
    "/appointments/{appointment_id}/confirm", response_model=OkResponse[ConfirmData]
)
def confirm_appointment(
    appointment_id: int,
    user: sqlite3.Row = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """加分项 2：确认候补转正的号。

    被递补选中不会自动成号 —— 系统只是把号留给他，等他点这一下。

    **这里刻意开了两个事务，不是写重了**：

        第一个：结算（把逾期的 pending 作废、顺延给下一位），然后提交
        第二个：确认，失败就抛异常

    合并成一个的后果很具体：确认失败时会抛 BizError，事务随之回滚，
    于是第一个事务刚刚作废掉的号**又变回 pending**。
    用户看到"确认已超时"，刷新一下页面却发现自己还能确认 ——
    而那个号已经被顺延给别人了，他这一下确认会把两个人的数据搞乱。

    根本原因在于：这里的"失败"不是"这笔操作没做成"，
    而是"结算的结果就是：你已经超时了"。
    **依赖结算结果的判断，必须与结算本身分开提交。**
    """
    with db.transaction(conn):
        booking.settle_pending_of_user(conn, user["id"])
    with db.transaction(conn):
        result = booking.confirm_promotion(conn, user["id"], appointment_id)
    return {"ok": True, "data": result}


@router.post(
    "/appointments/{appointment_id}/check-in",
    response_model=OkResponse[CheckInData],
)
def check_in_appointment(
    appointment_id: int,
    user: sqlite3.Row = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """加分项 3：就诊当天签到。

    没有这一步，系统永远不知道一个号到底用没用掉，
    「占了号不来」与「来了」在库里长得一模一样，
    爽约统计与自动拉黑就都无从谈起。
    """
    with db.transaction(conn):
        result = booking.check_in(conn, user["id"], appointment_id)
    return {"ok": True, "data": result}


@router.post("/admin/settle", response_model=OkResponse[SettleData])
def settle(
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """加分项 2/3：手动触发一次全局结算（超时顺延 + 爽约拉黑）。

    存在的唯一理由是**演示**：惰性结算平时由业务请求顺带触发，
    但答辩时要当众展示"超过 15 分钟没确认会怎样"，总不能真等一刻钟。
    它把"没人来问也要结算一次"这个动作显式暴露出来。

    与其余接口一样不做鉴权 —— 见 docs/API.md 5.5 的说明，
    真实系统里这里必须有独立的管理员鉴权与操作审计。
    """
    with db.transaction(conn):
        result = booking.settle_all(conn)
    return {"ok": True, "data": result}


@router.post(
    "/admin/users/{user_id}/blacklist", response_model=OkResponse[BlacklistData]
)
def blacklist_user(
    user_id: int,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """加分项 5：拉黑一个用户（演示用，手动触发）。

    自动拉黑由爽约累计触发（见 booking._bump_no_show），
    这里是给演示留的手动入口：不用先爽约两次，
    就能当场展示"被拉黑的人约不到号"。
    """
    with db.transaction(conn):
        result = _set_blacklist(conn, user_id, True)
    return {"ok": True, "data": result}


@router.delete(
    "/admin/users/{user_id}/blacklist", response_model=OkResponse[BlacklistData]
)
def unblacklist_user(
    user_id: int,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """加分项 5：解除拉黑。"""
    with db.transaction(conn):
        result = _set_blacklist(conn, user_id, False)
    return {"ok": True, "data": result}


def _set_blacklist(
    conn: sqlite3.Connection, user_id: int, blacklist: bool
) -> dict:
    """拉黑 / 解除拉黑的共同实现。

    两个路由只差一个布尔值，合成一个私有函数。
    刻意**不**做成 `POST /admin/users/{id}/blacklist?on=0` 这种用参数控制动作的接口：
    "改状态"用 PUT/DELETE 表达，语义是确定的；
    用查询参数控制开关，等于把动作藏进参数里，看调用日志时完全看不出做了什么。
    """
    row = conn.execute(
        "SELECT id, status, no_show_count FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise BizError("APPOINTMENT_NOT_FOUND", "该用户不存在")

    new_status = "blacklisted" if blacklist else "active"
    conn.execute(
        "UPDATE users SET status = ? WHERE id = ?", (new_status, user_id)
    )
    return {
        "user_id": user_id,
        "status": new_status,
        "no_show_count": row["no_show_count"],
        "message": "已限制该账号" if blacklist else "已解除限制",
    }


def _my_appointments(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """查当前用户的有效预约。

    只取"仍然占着号"的记录，已取消的不出现在这个列表里 ——
    退了就是退了，不该占着用户的视线。
    （真要查历史，加分项的爽约统计会用到，那是从表里直接查。）
    """
    rows = conn.execute(
        f"SELECT a.id, a.schedule_id, a.seq_no, a.status, a.source, a.created_at, "
        f"       a.confirm_deadline, a.checked_in_at, "
        f"       d.name AS doctor_name, d.department, s.work_date, s.period, "
        f"       {clock.today_sql()} AS today, "
        f"       {guards.minutes_until_start_expr('s')} AS minutes_until_start "
        f"  FROM appointments a "
        f"  JOIN schedules s ON s.id = a.schedule_id "
        f"  JOIN doctors   d ON d.id = s.doctor_id "
        f" WHERE a.user_id = ? AND a.{guards.ACTIVE_STATUS_CLAUSE} "
        f" ORDER BY s.work_date, s.period",
        (user_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "schedule_id": r["schedule_id"],
            "doctor_name": r["doctor_name"],
            "department": r["department"],
            "work_date": r["work_date"],
            "period_label": period_label(r["period"]),
            "seq_no": r["seq_no"],
            "status": r["status"],
            "status_label": appointment_status_label(r["status"]),
            "source": r["source"],
            "source_label": source_label(r["source"]),
            "created_at": r["created_at"],
            "confirm_deadline": r["confirm_deadline"],
            "checked_in_at": r["checked_in_at"],
            # 三个"现在能做什么"的标志由后端给出，前端不要自己按 status 推。
            # 尤其 can_check_in 依赖服务端的时钟 —— 用前端的本地时间去比，
            # 用户改一下系统时间就能给自己变出一个签到按钮。
            "can_confirm": r["status"] == "pending",
            "can_check_in": r["status"] == "booked"
            and r["work_date"] == r["today"],
            "can_cancel": r["status"] == "booked"
            and r["work_date"] >= r["today"]
            and r["minutes_until_start"] >= policy.CANCEL_CUTOFF_MINUTES,
        }
        for r in rows
    ]


def _my_waitlist(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """查当前用户还在排着的候补。

    只取 waiting 状态的：已转正的去看正式预约，
    已取消的不该再让用户惦记。
    """
    rows = conn.execute(
        "SELECT w.id, w.schedule_id, w.position, w.status, w.joined_at, "
        "       d.name AS doctor_name, d.department, s.work_date, s.period "
        "  FROM waitlist w "
        "  JOIN schedules s ON s.id = w.schedule_id "
        "  JOIN doctors   d ON d.id = s.doctor_id "
        " WHERE w.user_id = ? AND w.status = 'waiting' "
        " ORDER BY s.work_date, s.period",
        (user_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "schedule_id": r["schedule_id"],
            "doctor_name": r["doctor_name"],
            "department": r["department"],
            "work_date": r["work_date"],
            "period_label": period_label(r["period"]),
            # 库里存的是排序键，这里翻译成给人看的名次。
            # 一个用户同时排的队最多两三条，逐条换算的开销可以忽略。
            "position": booking.rank_in_waitlist(
                conn, r["schedule_id"], r["position"]
            ),
            "status": r["status"],
            "status_label": waitlist_status_label(r["status"]),
            "joined_at": r["joined_at"],
        }
        for r in rows
    ]
