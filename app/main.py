"""应用入口。

本文件只做「装配」这一件事：建 app、挂异常处理器、注册路由、挂静态目录。
业务逻辑一行都不在这里，读下来 30 秒就能看清整个应用长什么样。

启动：
    uvicorn app.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.errors import register_exception_handlers
from app.routers import appointments, auth, schedules

# 静态文件目录（R4 的四个页面会放在这里）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时确保数据库就位，并且是**当前版本的结构**。

    用 `db.ensure_up_to_date()` 而不是 `db.init_db()`：
    后者只管"文件不存在就建一个"，对已存在但结构过时的库无能为力。
    R5 给 appointments 加了两个字段、一个 `pending` 状态、
    以及两个唯一索引的 WHERE 条件 —— 旧演示库若不做迁移，
    平时一切正常，直到有人被递补、要插 `pending` 记录时撞上 CHECK 约束。

    数据本身依旧不丢：ensure_up_to_date 只在文件缺失时才灌种子，
    库已存在时只做结构升级，一条业务数据都不动。
    这与题目要求的「数据持久化，重启不丢」是一致的 ——
    只有主动跑 scripts/reset_db.py 才会重建。
    """
    db.ensure_up_to_date()
    yield


app = FastAPI(
    title="医院门诊号源预约与候补系统",
    description="数媒程序开发组 10 月考核项目",
    version="0.5.0",
    lifespan=lifespan,
)

# 统一响应与错误码的转换规则
register_exception_handlers(app)

# 先注册 API 路由，再挂载静态目录。
# 顺序反了会出事：StaticFiles 挂在 "/" 上是兜底路由，
# 它能匹配任何路径。若它先注册，/api/schedules 会先被它截住，
# 然后返回 404 —— 而且这种 404 不带我们的统一响应体，排查起来很费劲。
app.include_router(auth.router)
app.include_router(schedules.router)
app.include_router(appointments.router)

# html=True 让访问 "/" 时自动去找 index.html，R4 需要。
# 现在目录里还没有页面，访问会 404，属于预期内。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
