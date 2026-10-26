"""FastAPI 依赖。

本文件只放「每个接口都要用的公共依赖」，当前只有一个 current_user。
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Header

from app import db
from app.errors import BizError


def current_user(
    # 刻意用 str 接收再自己转 int，而不是直接声明成 int。
    # 原因：契约（docs/API.md 第 3 节）要求「缺失或无效 → 401」，
    # 若声明成 int，传 "abc" 时 FastAPI 会先抛 422，出现两个码表达同一件事。
    # 这里统一收敛成 401，让前端只需要处理一个状态码。
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> sqlite3.Row:
    """解析 X-User-Id 请求头，拿到当前登录用户。

    为什么必须用 Depends 拿 conn，而不是自己再开一个连接：
        FastAPI 的依赖缓存默认开启，因此同一请求里 current_user 和路由函数
        拿到的是**同一个连接**。这一点在 SQLite 上很关键 ——
        多开一个连接就多一个文件锁的持有者，R5 的并发测试
        （几十个请求同时抢号）会被自己人卡死。

    Returns:
        users 表的一行。

    Raises:
        BizError: UNAUTHORIZED，请求头缺失、不是整数、或用户不存在。
    """
    if x_user_id is None:
        raise BizError("UNAUTHORIZED")

    try:
        user_id = int(x_user_id)
    except ValueError as exc:
        raise BizError("UNAUTHORIZED") from exc

    row = conn.execute(
        "SELECT id, name, status, no_show_count FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise BizError("UNAUTHORIZED")

    return row
