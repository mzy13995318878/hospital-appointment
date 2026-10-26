#!/usr/bin/env python3
"""一键重建数据库。

用法:
    python scripts/reset_db.py

行为:
    删掉 data/clinic.db（连同 SQLite 可能残留的 -wal / -shm 文件），
    然后重新执行 sql/schema.sql 与 sql/seed.sql，恢复到演示用的初始状态。

什么时候用:
    1. 演示前想让数据回到干净状态；
    2. 改了 sql/ 里的脚本之后；
    3. 数据库出现脏数据（正常流程下不应该发生）。

注意:
    本操作会清空所有预约与候补记录，属于破坏性操作。
    本脚本只重建结构固定的演示数据，不会碰业务数据以外的任何东西。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# 把项目根加入模块搜索路径，这样才能 import app.db
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import db  # noqa: E402  （导入必须在加完路径之后）


def reset() -> None:
    """删库并重建。"""
    # 先删主库文件
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
        print(f"已删除 {db.DB_PATH.name}")

    # SQLite 在异常退出时可能留下 -wal / -shm 文件，
    # 留着它们会让新库读到一个不完整的旧状态，必须一起清掉
    for suffix in ("-wal", "-shm", "-journal"):
        leftover = db.DB_PATH.with_name(db.DB_PATH.name + suffix)
        if leftover.exists():
            leftover.unlink()
            print(f"已删除残留文件 {leftover.name}")

    db.init_db()
    print(f"已重建 {db.DB_PATH.name}")

    _report()


def _report() -> None:
    """打印重建后的数据概况，用于确认 seed 结果符合预期。

    除了「各表多少条」之外，这里**刻意多做一步自洽性校验**。
    理由是：种子数据是绕过所有业务校验直接写库的，
    应用层那条「同一天同一科室限 1 个号」的规则管不到它。
    库里的 CHECK 也只约束单表（比如 booked_count <= total_slots），
    管不了跨表的一致性。

    于是就有一类错误**只存在于种子数据里**：
    页面能正常显示、接口能正常返回，但数据是自相矛盾的。
    比如 booked_count 写了 10、实际只有 3 条预约记录 ——
    剩余号源会算成「还有 20 个」，而列表看起来完全正常。
    这类 bug 不会报错，只会让评委在台下越看越不对劲。

    所以重建之后当场把这几条查一遍，比事后靠眼睛发现便宜得多。
    """
    conn = db.connect()
    try:
        counts = {
            "doctors": conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0],
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "schedules": conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0],
            "appointments": conn.execute(
                "SELECT COUNT(*) FROM appointments"
            ).fetchone()[0],
            "waitlist": conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0],
        }
        print("\n数据概况:")
        for table, count in counts.items():
            print(f"  {table:<14} {count:>3} 条")

        _print_departments(conn)
        _print_demo_schedules(conn)
        _print_status_summary(conn)
        _check_consistency(conn)
    finally:
        conn.close()


# 排班查询的公共部分，三个打印/校验函数共用。
_SCHEDULE_QUERY = """
    SELECT s.id,
           d.name         AS doctor,
           d.department   AS dept,
           s.work_date    AS wdate,
           s.period,
           s.total_slots  AS total,
           s.booked_count AS booked,
           s.is_closed,
           (SELECT COUNT(*) FROM waitlist w
             WHERE w.schedule_id = s.id AND w.status = 'waiting') AS waiting
      FROM schedules s
      JOIN doctors d ON d.id = s.doctor_id
"""


def _state_of(row: sqlite3.Row) -> str:
    """把一条排班翻译成号源状态，口径与 services/slot_status 保持一致。"""
    if row["is_closed"]:
        return "已停诊"
    if row["booked"] < row["total"]:
        return "可约"
    if row["waiting"] > 0:
        return f"候补中(排队{row['waiting']}人)"
    return "已满"


def _print_departments(conn: sqlite3.Connection) -> None:
    """科室 × 医生 × 排班的分布。

    看这一屏就能判断「像不像一家医院」：
    科室是不是只有两三个、每个科室是不是只有一个医生。
    """
    rows = conn.execute(
        """
        SELECT d.department AS dept,
               COUNT(DISTINCT d.id) AS doctors,
               COUNT(s.id)          AS scheds
          FROM doctors d
          LEFT JOIN schedules s ON s.doctor_id = d.id
         GROUP BY d.department
         ORDER BY d.department
        """
    ).fetchall()
    print(f"\n科室分布（共 {len(rows)} 个科室）:")
    for r in rows:
        print(f"  {r['dept']:<6} {r['doctors']:>2} 位医生 / {r['scheds']:>2} 条排班")


def _print_demo_schedules(conn: sqlite3.Connection) -> None:
    """演示组排班（id 1–8）逐条列出。

    这 8 条是答辩现场要操作的靶子，必须一条条确认状态对得上。
    背景排班有几十条，只统计不罗列 —— 全列出来的话，
    真正要看的东西会被淹没在一屏屏的列表里。
    """
    rows = conn.execute(f"{_SCHEDULE_QUERY} WHERE s.id <= 8 ORDER BY s.id").fetchall()
    print("\n演示组排班（答辩现场操作的就是这 8 条）:")
    for r in rows:
        print(
            f"  [{r['id']}] {r['dept']} {r['doctor']} "
            f"{r['wdate']} {r['period']:<9} "
            f"{r['booked']:>2}/{r['total']:<2} → {_state_of(r)}"
        )


def _print_status_summary(conn: sqlite3.Connection) -> None:
    """全部排班的状态分布，确认四种号源状态都造出来了。"""
    rows = conn.execute(f"{_SCHEDULE_QUERY} ORDER BY s.id").fetchall()
    tally: dict[str, int] = {}
    for r in rows:
        # 「候补中(排队2人)」归并成「候补中」，避免排队人数不同就分成多档
        key = _state_of(r).split("(")[0]
        tally[key] = tally.get(key, 0) + 1
    print(f"\n排班状态分布（共 {len(rows)} 条）:")
    for key in ("可约", "已满", "候补中", "已停诊"):
        if key in tally:
            print(f"  {key:<6} {tally[key]:>3} 条")


def _check_consistency(conn: sqlite3.Connection) -> None:
    """跨表自洽性校验。

    这四条都不是数据库约束能兜住的 —— 要么跨表，要么属于业务规则。
    任何一条不通过，都说明 sql/seed.sql 里的数据自相矛盾，
    而这类问题在接口和页面上**完全看不出来**。

    有效预约的判定一律复用 db.ACTIVE_STATUS_CLAUSE，
    不在这里另写一份状态清单 —— 硬编码状态清单这件事
    在这个项目里已经栽过一次（见 PROJECT_STATE.md 决策 22）。
    """
    checks = [
        (
            "booked_count 与实际有效预约数一致",
            f"""
            SELECT COUNT(*) FROM (
                SELECT s.id
                  FROM schedules s
                  JOIN appointments a
                    ON a.schedule_id = s.id AND {db.ACTIVE_STATUS_CLAUSE}
                 GROUP BY s.id
                HAVING COUNT(*) <> s.booked_count
            )
            """,
        ),
        (
            "停诊排班没有未完成的预约",
            # 只看 booked / pending：completed 与 no_show 是停诊之前
            # 就已经发生的历史，它们存在是合理的
            """
            SELECT COUNT(*) FROM schedules s
             WHERE s.is_closed = 1
               AND EXISTS (SELECT 1 FROM appointments a
                            WHERE a.schedule_id = s.id
                              AND a.status IN ('booked', 'pending'))
            """,
        ),
        (
            "无「同一用户同一天同一科室」的多条预约",
            # 必做项 5 的硬规则。种子数据是绕过校验直接写库的，
            # 这里补查一遍，免得演示时「我的预约」里出现自相矛盾的记录
            f"""
            SELECT COUNT(*) FROM (
                SELECT a.user_id
                  FROM appointments a
                  JOIN schedules s ON s.id = a.schedule_id
                  JOIN doctors   d ON d.id = s.doctor_id
                 WHERE {db.ACTIVE_STATUS_CLAUSE}
                 GROUP BY a.user_id, s.work_date, d.department
                HAVING COUNT(*) > 1
            )
            """,
        ),
        (
            "没有人既持有号又在候补同一排班",
            """
            SELECT COUNT(*) FROM waitlist w
             WHERE w.status = 'waiting'
               AND EXISTS (SELECT 1 FROM appointments a
                            WHERE a.user_id = w.user_id
                              AND a.schedule_id = w.schedule_id
                              AND a.status IN ('booked', 'pending'))
            """,
        ),
    ]

    print("\n自洽性校验:")
    bad = 0
    for label, sql in checks:
        n = conn.execute(sql).fetchone()[0]
        if n == 0:
            print(f"  [OK] {label}")
        else:
            print(f"  [!!] {label} —— 有 {n} 处不一致，请检查 sql/seed.sql")
            bad += 1
    if bad:
        print(f"\n  共 {bad} 项未通过，数据不可用于演示。")
    else:
        print("  全部通过。")


if __name__ == "__main__":
    reset()
