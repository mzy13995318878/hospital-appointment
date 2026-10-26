"""身份接口：登录与查看当前用户。

题目只要求「模拟注册登录，不对接公安身份核验」，因此这里刻意
不做密码、不做短信、不做 JWT —— README 里会主动说明这是刻意简化，
而不是没做完。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app import db
from app.deps import current_user
from app.errors import BizError
from app.schemas import LoginData, LoginRequest, MeData, OkResponse

router = APIRouter(prefix="/api/auth", tags=["身份"])


@router.post("/login", response_model=OkResponse[LoginData])
def login(
    body: LoginRequest,
    conn: sqlite3.Connection = Depends(db.get_conn),
) -> dict:
    """登录；三要素查不到就自动注册。

    注意这里**没有**用事务包住「先查再插」。
    常规写法是：

        row = SELECT ...            # 第一步：查
        if row is None:
            INSERT ...              # 第二步：插

    这两步之间若有两个人同时提交同样的三要素（比如手抖双击），
    两人都会查到「不存在」，然后都去插 —— 触发唯一索引冲突报错，
    后一个人的请求直接 500，体验很差。

    这里改用 INSERT OR IGNORE 一步到位：
        - 冲突了就静默忽略，不报错；
        - 靠 rowcount 区分「真插入了」还是「被忽略了」。

    这正好是「用数据库约束兜底，而不是靠应用层先查一遍」的思路，
    和 R3 抢号用的是同一套思想。
    """
    # 只校验长度，不强制为数字 —— 真实身份证最后一位可能是字母 X
    if len(body.id_card_last4) != 4 or len(body.phone) != 11:
        raise BizError("INVALID_IDENTITY")

    cur = conn.execute(
        "INSERT OR IGNORE INTO users (name, id_card_last4, phone) VALUES (?, ?, ?)",
        (body.name, body.id_card_last4, body.phone),
    )

    if cur.rowcount == 1:
        # 插入成功 → 新用户，lastrowid 就是新分配的 id
        user_id = cur.lastrowid
        is_new = True
    else:
        # 被 IGNORE 说明三要素已存在，取回既有用户
        row = conn.execute(
            "SELECT id FROM users WHERE name = ? AND id_card_last4 = ? AND phone = ?",
            (body.name, body.id_card_last4, body.phone),
        ).fetchone()
        user_id = row["id"]
        is_new = False

    return {
        "ok": True,
        "data": {"user_id": user_id, "name": body.name, "is_new": is_new},
    }


@router.get("/me", response_model=OkResponse[MeData])
def me(user: sqlite3.Row = Depends(current_user)) -> dict:
    """返回当前登录用户。

    current_user 依赖会在缺请求头、请求头非法、用户不存在时抛 401，
    所以这里拿到的 user 一定有效，不需要再判空。
    """
    return {
        "ok": True,
        "data": {
            "user_id": user["id"],
            "name": user["name"],
            "status": user["status"],
            "no_show_count": user["no_show_count"],
        },
    }
