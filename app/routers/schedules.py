"""排班列表与医生详情。

这两个接口是 R4 前端列表页与医生详情页的数据来源。

本模块只做两件事：把库里的原始行查出来，交给 slot_status 算出派生值。
业务判断一个都不在这里 —— 那是 R3 的预约服务和 R5 的 guards 的事。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query

from app import db
from app.errors import BizError
from app.schemas import DepartmentItem, DoctorDetail, OkResponse, ScheduleItem
from app.services.slot_status import (
    ALL_STATUSES,
    STATUS_SQL_EXPR,
    compute_status,
    period_label,
)

router = APIRouter(prefix="/api", tags=["排班与医生"])

# 排班查询的公共部分。
#
# waiting_count 用标量子查询现算，而不是 JOIN waitlist 再 GROUP BY。
# 两个理由：
#   1. 用 JOIN 会让「一个候补都没有」的排班被连接成 0 行或直接丢掉，
#      除非再写 LEFT JOIN + COALESCE，反而更绕；
#   2. 标量子查询保证一条排班永远只返回一行，不会被候补表放大行数，
#      后面不用再做去重。
_SCHEDULE_SELECT = f"""
    SELECT s.id            AS schedule_id,
           s.doctor_id     AS doctor_id,
           d.name          AS doctor_name,
           d.title         AS title,
           d.department    AS department,
           s.work_date     AS work_date,
           s.period        AS period,
           s.total_slots   AS total_slots,
           s.booked_count  AS booked_count,
           s.is_closed     AS is_closed,
           (SELECT COUNT(*) FROM waitlist w
             WHERE w.schedule_id = s.id AND w.status = 'waiting') AS waiting_count,
           {STATUS_SQL_EXPR} AS status
      FROM schedules s
      JOIN doctors d ON d.id = s.doctor_id
"""

# 「今天及以后」的筛选条件，供列表页与医生详情页共用。
#
# 时钟必须写 date('now','localtime')，与 guards 判定「排班已过期」
# 用的是同一个时钟（见 guards.fetch_schedule 的注释）。
# 漏掉 localtime 会按 UTC 算，东八区早上 8 点之前会算成"昨天"，
# 于是当天上午的号在早上查不到 —— 这种 bug 只在半天里出现，极难复现。
#
# 刻意**不删**过去的排班记录：删了就没有历史了，
# 将来要统计"这位医生上个月出诊几天"时无从下手。
# 只是让它们默认不出现在列表里（理由见 docs/API.md 4.2 节）。
_FUTURE_COND = "s.work_date >= date('now','localtime')"

# 时段排序：morning 要排在 afternoon 前面。
# 直接按字符串排会得到相反的结果（a < m，afternoon 反而靠前），
# 所以映射成数字再排。
#
# 表达式里刻意不写表别名前缀：这段既要用于内层查询（那时表叫 s），
# 又要用于外层包子查询之后的排序（那时列名已被 AS period 改名），
# 不写前缀才能两处通用。
_PERIOD_ORDER = (
    "CASE period WHEN 'morning' THEN 1 WHEN 'afternoon' THEN 2 ELSE 3 END"
)


def _to_item(row: sqlite3.Row) -> dict:
    """把数据库一行转成契约规定的列表项结构。

    remaining / status / status_label 三个字段库里都没有，
    全部在这里现算。列表页和医生详情页共用本函数，
    保证两处算出来的状态永远一致 —— 不会出现列表显示「候补中」、
    点进去详情显示「已满」这种事。

    注意：查出来的这一行里其实已经带了一个同名 status 列（SQL 算的，
    那是为了能在 WHERE 里筛选）。**这里刻意不取它，坚持用 compute_status
    重算一遍**。两条路径因此有了主次：

        显示 = 权威路径（Python 算）
        筛选 = 性能路径（SQL 算）

    好处是万一 SQL 版写错了，页面显示依然是对的，
    故障退化成「筛不准」而不是「显示错」。
    代价是两份逻辑可能不同步，由 R5 的一致性测试兜住。
    """
    status, status_label, remaining = compute_status(
        booked_count=row["booked_count"],
        total_slots=row["total_slots"],
        is_closed=row["is_closed"],
        waiting_count=row["waiting_count"],
    )
    return {
        "schedule_id": row["schedule_id"],
        "doctor_id": row["doctor_id"],
        "doctor_name": row["doctor_name"],
        "title": row["title"],
        "department": row["department"],
        "work_date": row["work_date"],
        "period": row["period"],
        "period_label": period_label(row["period"]),
        "total_slots": row["total_slots"],
        "booked_count": row["booked_count"],
        "remaining": remaining,
        "waiting_count": row["waiting_count"],
        "status": status,
        "status_label": status_label,
    }


@router.get("/departments", response_model=OkResponse[list[DepartmentItem]])
def list_departments(conn: sqlite3.Connection = Depends(db.get_conn)) -> dict:
    """科室下拉框的选项来源。

    刻意用 `JOIN schedules` 内连接：某科室下所有医生恰好都没排班时，
    这个科室不会出现在选项里。否则用户选了它却看到一片空白，
    第一反应是「系统坏了」，而不是「这个科室今天没号」。

    doctor_count 用 COUNT(DISTINCT d.id) 而不是 COUNT(*)：
    一位医生可能同时有上午和下午两条排班，
    直接 COUNT(*) 数出来的是排班数，不是医生数。
    """
    rows = conn.execute(
        "SELECT d.department AS department, COUNT(DISTINCT d.id) AS doctor_count "
        "  FROM doctors d "
        "  JOIN schedules s ON s.doctor_id = d.id "
        " GROUP BY d.department "
        " ORDER BY d.department"
    ).fetchall()
    return {"ok": True, "data": [dict(r) for r in rows]}


@router.get("/schedules", response_model=OkResponse[list[ScheduleItem]])
def list_schedules(
    department: str | None = Query(default=None, description="按科室筛选，如「口腔科」"),
    date: str | None = Query(
        default=None, description="按出诊日期筛选，格式 YYYY-MM-DD"
    ),
    status: str | None = Query(
        default=None, description="按号源状态筛选：open / full / waiting / closed"
    ),
    only_future: bool = Query(
        default=True, description="只返回今天及以后的排班，传 0 可看全部"
    ),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """排班列表，支持科室、日期、状态三重筛选。

    这三个筛选是 R4 列表页的必需项 —— 8 条排班不筛的话，
    演示时要在屏幕上翻半天才能找到想约的那个号。

    另有一个默认开启的 `only_future`：过去的排班不删，但默认不显示。
    因为它们根本约不了，显示出来只是让用户点了才知道不行。

    SQL 用参数化拼接：条件个数不定，但**值**一律走 ? 占位符，
    绝不把用户输入拼进 SQL 字符串。
    """
    # 枚举型参数必须显式校验。
    # 不交给 Pydantic 做，是因为 Pydantic 会给 422，而契约规定 400 + INVALID_PARAM。
    # 更关键的理由是：若非法值静默返回空列表，
    # 「status=opne 写错了」就会被包装成「今天一个号都没有」——
    # 一个参数错误伪装成了业务事实，这类 bug 极难排查。
    if status is not None and status not in ALL_STATUSES:
        raise BizError("INVALID_PARAM", f"status 只能取 {'/'.join(ALL_STATUSES)}")

    # 条件与参数分开收集，最后再拼装
    conditions: list[str] = []
    params: list[str] = []

    if only_future:
        conditions.append(_FUTURE_COND)
    if department:
        conditions.append("d.department = ?")
        params.append(department)
    if date:
        conditions.append("s.work_date = ?")
        params.append(date)

    sql = _SCHEDULE_SELECT
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    if status:
        # status 是算出来的派生列。SQL 不允许在同一层的 WHERE 里
        # 引用 SELECT 的别名，所以要包一层子查询，在外层过滤。
        sql = f"SELECT * FROM ({sql}) WHERE status = ?"
        params.append(status)

    # 这里拼接的是模块内的常量片段，不是用户输入，因此不存在注入风险
    sql += f" ORDER BY work_date, {_PERIOD_ORDER}, department"

    rows = conn.execute(sql, params).fetchall()
    return {"ok": True, "data": [_to_item(r) for r in rows]}


@router.get("/doctors/{doctor_id}", response_model=OkResponse[DoctorDetail])
def doctor_detail(
    doctor_id: int,
    only_future: bool = Query(
        default=True, description="只返回今天及以后的排班，传 0 可看全部"
    ),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """医生详情，含其全部排班。

    内嵌的 schedules 与列表页共用 _to_item，
    前端渲染两种卡片时可以复用同一段模板代码。

    `only_future` 的默认值与列表页保持一致：
    两处若不统一，列表页看不到昨天的号、点进详情却看得到，
    用户会认为这两个页面在互相矛盾。
    """
    doc = conn.execute(
        "SELECT id, name, title, department, intro, specialty FROM doctors WHERE id = ?",
        (doctor_id,),
    ).fetchone()
    if doc is None:
        raise BizError("DOCTOR_NOT_FOUND")

    where = "s.doctor_id = ?"
    if only_future:
        where += f" AND {_FUTURE_COND}"

    rows = conn.execute(
        f"{_SCHEDULE_SELECT} WHERE {where} "
        f"ORDER BY s.work_date, {_PERIOD_ORDER}",
        (doctor_id,),
    ).fetchall()

    return {
        "ok": True,
        "data": {
            "id": doc["id"],
            "name": doc["name"],
            "title": doc["title"],
            "department": doc["department"],
            "intro": doc["intro"],
            "specialty": doc["specialty"],
            "schedules": [_to_item(r) for r in rows],
        },
    }
