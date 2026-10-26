"""状态判定的两条路径必须永远给出同一个答案。

## 为什么状态判定会有两份

状态是派生值，库里没有这一列。于是：

- **显示**走 Python：路由取出一行，交给 `compute_status()` 算。
- **筛选**走 SQL：因为 `WHERE` 里不能引用 `SELECT` 的别名，
  若想按状态过滤，就只能在 SQL 里把状态再算一遍（`STATUS_SQL_EXPR`）。

两份逻辑做同一件事，天然存在"改了一边忘了另一边"的风险。
本文件的第一个用例就是把它们钉在一起。

## 为什么不干脆只留一份

只用 Python 的话，筛选就得把所有排班全查出来在内存里过滤。
现在数据只有几十条，将来上了几百个医生、几千条排班，
每次筛选都要把全表拉出来再丢掉大部分，得不偿失。

两条路径有主次之分（见 `routers/schedules.py` 的 `_to_item` 注释）：

        显示 = 权威路径（Python 算）
        筛选 = 性能路径（SQL 算）

万一 SQL 版写错了，页面显示依然是对的，
故障退化成"筛不准"而不是"显示错"。

## 最后一个用例是**变异测试**

它证明的不是"代码现在是对的"，而是
**"代码要是写错了，这些测试一定会变红"**。

一个从没红过的测试，没人知道它到底有没有在测东西。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.slot_status import (  # noqa: E402
    STATUS_CLOSED,
    STATUS_FULL,
    STATUS_OPEN,
    STATUS_SQL_EXPR,
    STATUS_WAITING,
    ALL_STATUSES,
    compute_status,
)

# ------------------------------------------------------------
# 造数据：把四种状态的所有组合都造一遍
# ------------------------------------------------------------

# 排队人数取 0 / 1 / 2：
# 取到 2 是为了覆盖「候补人数门槛」这类变异 ——
# 若只造 1 个人排队，"把门槛从 >0 改成 >1" 的变异就抓不到。
_WAITING_CHOICES = (0, 1, 2)
# 已占号数取 0..3（总数固定 3），覆盖「有余号」「刚好满」两种情形
_BOOKED_CHOICES = (0, 1, 2, 3)
_TOTAL = 3


def _build_matrix(fx) -> None:
    """造出 is_closed × booked × waiting 的全部组合，每种一条排班。

    刻意让「有余号 + 有人在排队」这种组合也出现：
    它在真实业务里存在（有人排队时别人退了号），
    而且只有它才能抓住"判定顺序颠倒"这一类变异。

    24 条排班必须分散到 24 个不同的日期：
    同一个医生同一天上午只能有一条排班，这是库里的唯一约束
    （ux_schedule_doctor_date_period）。撞上它说明**约束在正常工作**，
    是造数据的写法不对，不是约束该放松。
    """
    doc = fx.doctor("矩阵医生", "测试科")
    day = 0
    for is_closed in (0, 1):
        for booked in _BOOKED_CHOICES:
            for waiting in _WAITING_CHOICES:
                day += 1
                sch = fx.schedule(
                    doc, offset_days=day, total=_TOTAL, is_closed=is_closed
                )
                fx.fill(sch, booked)
                # position 必须每人一个值：库里有 (schedule_id, position) 唯一约束。
                # 同一个排班里两个人并列第 1 位是不可能发生的事，
                # 撞上它恰恰说明这条约束在正常工作。
                for i in range(waiting):
                    fx.enqueue(fx.user("排队人"), sch, position=i + 1)


def _python_status(conn, schedule_id: int) -> str:
    """权威路径：把这一行的派生值交给 compute_status 算。"""
    row = conn.execute(
        "SELECT booked_count, total_slots, is_closed FROM schedules WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    waiting = conn.execute(
        "SELECT COUNT(*) FROM waitlist"
        " WHERE schedule_id = ? AND status = 'waiting'",
        (schedule_id,),
    ).fetchone()[0]
    status, _label, _remaining = compute_status(
        booked_count=row["booked_count"],
        total_slots=row["total_slots"],
        is_closed=row["is_closed"],
        waiting_count=waiting,
    )
    return status


def _sql_statuses(conn, expr: str) -> dict[int, str]:
    """性能路径：用给定的 SQL 表达式一次性算出所有排班的状态。

    表达式是参数，这样变异测试才能把"坏掉的表达式"塞进来跑同一个检查。
    """
    return {
        r[0]: r[1]
        for r in conn.execute(f"SELECT s.id, {expr} AS st FROM schedules s").fetchall()
    }


def _mismatches(conn, expr: str) -> list[tuple[int, str, str]]:
    """列出「两条路径结论不同」的排班。

    返回 `(id, Python 结论, SQL 结论)` 的列表，空列表表示完全一致。
    这是变异测试要复用的那个检查本身。
    """
    ids = [r[0] for r in conn.execute("SELECT id FROM schedules").fetchall()]
    sql = _sql_statuses(conn, expr)
    bad = []
    for sid in ids:
        py = _python_status(conn, sid)
        if py != sql[sid]:
            bad.append((sid, py, sql[sid]))
    return bad


# ------------------------------------------------------------
# 用例
# ------------------------------------------------------------


def test_both_paths_agree_on_every_combination(conn, fx) -> None:
    """24 种组合逐一比对，Python 与 SQL 必须给出同一个状态。

    这是本文件的核心：两份逻辑一旦有人只改了一边，这里立刻变红。
    """
    _build_matrix(fx)
    mismatches = _mismatches(conn, STATUS_SQL_EXPR)

    assert mismatches == [], (
        "两条路径的结论不一致，说明 compute_status 与 STATUS_SQL_EXPR 有一边被改过：\n"
        + "\n".join(f"  排班 {i}: Python 说 {p}，SQL 说 {s}" for i, p, s in mismatches)
    )


def test_all_four_statuses_are_covered(conn, fx) -> None:
    """四种状态在数据矩阵里都必须至少出现一次。

    这是**变异测试的前置条件**：
    若矩阵里根本没有停诊的排班，那么"删掉停诊分支"的变异就抓不到 ——
    一个永远不被触发的分支，等于没有保护。
    先证明覆盖完整，再谈变异测试有没有意义。
    """
    _build_matrix(fx)
    seen = {_python_status(conn, r[0]) for r in conn.execute("SELECT id FROM schedules")}

    assert seen == set(ALL_STATUSES), (
        f"数据矩阵没有覆盖全部状态，缺 {set(ALL_STATUSES) - seen}，"
        f"变异测试对缺失的那些分支将失去意义"
    )


def test_remaining_is_total_minus_booked(conn, fx) -> None:
    """剩余号 = 总数 - 已占，且不与状态脱节。

    「满」的排班剩余必须是 0，否则用户会看到「已满」却还剩 3 个号。
    """
    _build_matrix(fx)
    for r in conn.execute(
        "SELECT id, booked_count, total_slots FROM schedules"
    ).fetchall():
        _s, _label, remaining = compute_status(
            booked_count=r["booked_count"],
            total_slots=r["total_slots"],
            is_closed=0,
        )
        assert remaining == r["total_slots"] - r["booked_count"]


# ---- 变异体：故意写坏的 SQL 表达式 ----

_WAITING_SUBQ = (
    "(SELECT COUNT(*) FROM waitlist w"
    " WHERE w.schedule_id = s.id AND w.status = 'waiting')"
)

MUTANTS = {
    "漏掉候补分支": (
        f"CASE WHEN s.is_closed = 1 THEN '{STATUS_CLOSED}'"
        f"     WHEN s.booked_count < s.total_slots THEN '{STATUS_OPEN}'"
        f"     ELSE '{STATUS_FULL}' END"
    ),
    "候补门槛从 >0 改成 >1": (
        f"CASE WHEN s.is_closed = 1 THEN '{STATUS_CLOSED}'"
        f"     WHEN s.booked_count < s.total_slots THEN '{STATUS_OPEN}'"
        f"     WHEN {_WAITING_SUBQ} > 1 THEN '{STATUS_WAITING}'"
        f"     ELSE '{STATUS_FULL}' END"
    ),
    "把候补判定提到有余号之前（顺序颠倒）": (
        f"CASE WHEN s.is_closed = 1 THEN '{STATUS_CLOSED}'"
        f"     WHEN {_WAITING_SUBQ} > 0 THEN '{STATUS_WAITING}'"
        f"     WHEN s.booked_count < s.total_slots THEN '{STATUS_OPEN}'"
        f"     ELSE '{STATUS_FULL}' END"
    ),
    "漏掉停诊分支": (
        f"CASE WHEN s.booked_count < s.total_slots THEN '{STATUS_OPEN}'"
        f"     WHEN {_WAITING_SUBQ} > 0 THEN '{STATUS_WAITING}'"
        f"     ELSE '{STATUS_FULL}' END"
    ),
}


def test_consistency_check_catches_every_mutant(conn, fx) -> None:
    """把 SQL 表达式逐个改坏，一致性检查必须**每一个都抓得到**。

    这是在给测试本身做体检：
    若某个变异体能悄悄溜过去，说明那个分支根本没被数据覆盖到，
    上面的"两条路径一致"就是一句空话 —— 它只是碰巧没走到那条路。

    判定标准故意写成 `mismatches` 非空，而不是"应该相等"：
    我们期待的就是**不一致**。
    """
    _build_matrix(fx)

    # 先确认原始表达式本身是干净的，否则下面抓到的可能是它自己有问题
    assert _mismatches(conn, STATUS_SQL_EXPR) == []

    for name, mutant in MUTANTS.items():
        caught = _mismatches(conn, mutant)
        assert caught, (
            f"变异体「{name}」没有被抓到 —— "
            f"说明这个分支没被数据覆盖，一致性测试对它形同虚设"
        )
        print(f"  变异体「{name}」被抓到，影响 {len(caught)} 条排班")
