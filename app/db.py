"""数据库连接与初始化。

本模块是全项目唯一创建 SQLite 连接的地方。其它模块一律通过 get_conn()
获取连接，不得自己调 sqlite3.connect —— 否则会漏掉下面三个关键设置：

1. 外键约束：SQLite 默认关闭，必须每个连接单独打开才生效；
2. 行工厂：默认返回元组，取字段要靠下标，改成 Row 后可以用列名；
3. 超时时间：并发写时会抢锁，不设 timeout 会立刻抛 "database is locked"。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 项目根：本文件位于 <项目根>/app/db.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 数据库文件放在 data 目录下，与代码分离
DB_PATH = PROJECT_ROOT / "data" / "clinic.db"
# SQL 脚本目录
SQL_DIR = PROJECT_ROOT / "sql"

# 环境变量名：设了它就用它指向的库文件
DB_PATH_ENV = "CLINIC_DB"

# 并发等待锁的时间（秒）。
# 两个请求同时写库时，后到的会在这里等，超时才报错。
# 我们的写事务都很短（几毫秒），5 秒足够，也避免异常请求长时间占锁。
LOCK_TIMEOUT_SECONDS = 5.0

# 「仍然占着号」的预约状态。
#
# 这四值出现在四个地方，必须逐字一致：
#   1. sql/schema.sql      —— 两个唯一索引的 WHERE（建新库时用）
#   2. 本文件              —— 迁移时重建索引（升级旧库时用）
#   3. services/guards.py  —— ACTIVE_STATUS_CLAUSE（运行时判断，由本常量生成）
#   4. services/booking.py —— 最小空号算法要靠它排掉已释放的号
#
# R5 加入 pending 时，漏改任何一处都会出事，而**漏改索引那处最隐蔽**：
# 应用层照样拦得住重复预约，表面一切正常，
# 只有"绕过应用层直接写库"这条路径悄悄失去保护 ——
# 而那正是索引存在的唯一理由。
#
# 于是把清单收在本文件，由 guards.py 引用来生成 SQL 片段。
# schema.sql 那份无法 import（它是纯文本），只能靠注释互相指认 ——
# 这是明知有重复代价的取舍，与 STATUS_SQL_EXPR 那两处是同一类问题。
ACTIVE_APPOINTMENT_STATUSES = ("booked", "pending", "completed", "no_show")

# 由上面的清单拼出的 SQL 片段，供迁移时建索引。
# 用 ', '.join 拼而不是再抄一遍字符串，改清单时这里自动跟上。
ACTIVE_STATUS_CLAUSE = "status IN (" + ", ".join(
    f"'{s}'" for s in ACTIVE_APPOINTMENT_STATUSES
) + ")"


def resolve_db_path() -> Path:
    """决定这次要用哪个库文件：环境变量优先，没设就用默认的。

    为什么需要这一层：R5 的并发测试要用**真的 uvicorn 进程**，
    而 FastAPI 的依赖覆盖（conftest 里改 db.get_conn 那一套）
    只在当前进程内有效，跨不了进程边界。
    起了独立进程，它只会按默认路径去开 data/clinic.db ——
    那样并发测试会把演示库写进一堆「占位 37 号」的脏数据，
    演示时就穿帮了；多个测试并行跑还会互相抢同一个文件。

    于是改成"配置外部化"：环境变量有就听它的，没有就原样回落。
    生产环境从来不设这个变量，行为与之前**完全一致** ——
    为测试方便而让生产代码变复杂的做法，才是真的得不偿失。

    Returns:
        应当使用的数据库文件路径。
    """
    override = os.environ.get(DB_PATH_ENV)
    return Path(override) if override else DB_PATH


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """建立一个配置完备的数据库连接。

    这里刻意不做连接池：SQLite 是文件级锁，多连接反而会增加争抢，
    FastAPI 每个请求现开一个连接的开销在这个量级下可以忽略。

    Args:
        db_path: 数据库文件路径。留空则由 resolve_db_path() 决定。

    Returns:
        已开启外键、已设置行工厂的 SQLite 连接。
    """
    # 默认参数用 None 而不是 DB_PATH，是为了绕开 Python 默认参数只求值一次的坑：
    # 写成 db_path=DB_PATH 时，这个值在 import 的那一刻就绑定死了，
    # 之后改环境变量、改模块变量都不会生效。
    # 留空再在函数体内解析，每次调用都是最新的结果。
    if db_path is None:
        db_path = resolve_db_path()

    conn = sqlite3.connect(
        db_path,
        timeout=LOCK_TIMEOUT_SECONDS,
        # 关掉 Python 的隐式事务：我们要自己控制 BEGIN IMMEDIATE 的时机，
        # 否则 sqlite3 会在第一条写语句时自动开一个普通事务，
        # 那就变成了 deferred 事务，有死锁风险（见 docs/GUIDE.md 第 5.5 节）
        isolation_level=None,
        # ---- 关掉跨线程检查。这是 R5 并发测试抓出来的真 bug，理由如下 ----
        #
        # 【现象】并发请求下每个请求都 500，报
        #   ProgrammingError: SQLite objects created in a thread
        #   can only be used in that same thread.
        #
        # 【根因】FastAPI 处理 `yield` 型依赖时，
        #   会把这个生成器**扔进一个专门的线程**去驱动（contextmanager_in_threadpool），
        #   而同步路由函数（我们的路由全是 `def` 不是 `async def`）会被
        #   Starlette 丢进**另一个线程池线程**执行。
        #   于是「创建连接的线程」与「使用连接的线程」必然不是同一个，
        #   sqlite3 模块默认的 check_same_thread=True 当场把它拦下。
        #
        # 【为什么关掉它仍然是安全的】
        #   那个检查防的是「两个线程同时操作同一个连接」，这确实会毁掉数据。
        #   但我们的模型是**一个请求一个连接**：
        #   生成器一旦 yield 就停在原地等待，直到请求结束才被唤醒去 close，
        #   所以任意时刻真正在碰这个连接的只有一个线程 —— 只是它不是创建者。
        #   我们关掉的是一条与我们的用法不匹配的保守规则，不是关掉保护本身。
        #
        # 【为什么不去改路由】
        #   把路由改成 `async def` 也能绕开：那样请求就不进线程池了，
        #   全在事件循环线程里执行，线程自然一致。
        #   但代价是**所有请求被串行化** —— 同步的 sqlite3 调用会堵住事件循环，
        #   一个请求在写库时，其他请求连进都进不来。
        #   那等于为了让一个检查通过，把整个服务的并发能力废掉，本末倒置。
        #
        # 【为什么要强调只有并发测试能发现它】
        #   TestClient 串行处理请求，anyio 的线程池会**复用同一个 worker 线程**，
        #   于是创建者和使用者碰巧是同一线程，23 条单测全绿，问题被完美掩盖。
        #   只有并发起来、请求落到不同线程时才暴露。
        #   这是「必须用真 uvicorn 做并发测试」最有力的一条证据。
        check_same_thread=False,
    )
    # 外键约束是连接级开关，每个新连接都要开一次
    conn.execute("PRAGMA foreign_keys = ON")
    # 让查询结果支持 row["列名"] 而不是 row[0]
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    """建表并灌入演示数据。

    只在数据库文件不存在时执行，因此服务每次启动都能直接调用，
    不会覆盖已有数据 —— 这正是题目要求的「数据持久化，重启不丢」。

    Args:
        db_path: 数据库文件路径。留空则由 resolve_db_path() 决定。
    """
    if db_path is None:
        db_path = resolve_db_path()

    if db_path.exists():
        return

    # data 目录可能还不存在（比如刚 clone 下来）
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        _executescript(conn, SQL_DIR / "schema.sql")
        _executescript(conn, SQL_DIR / "seed.sql")
    finally:
        conn.close()


def ensure_up_to_date(db_path: Path | None = None) -> None:
    """确保库存在，且结构是当前版本。

    与 `init_db()` 的区别：init_db 只管"文件不存在就建一个"，
    对**已存在但结构过时**的库无能为力 —— 而 R5 恰恰给 appointments
    加了两个字段、一个状态值和两个索引的 WHERE 条件。

    没有这层的话，升级代码后启动服务，旧演示库会一直带着旧结构跑：
    平时一切正常，直到有人被递补、要插入 `pending` 记录，
    撞上 CHECK 约束直接 500。
    那种故障最讨厌的地方在于**它只在特定操作路径上出现**，
    演示前怎么点都点不出来，答辩时当场给你表演一次。

    Args:
        db_path: 数据库文件路径。留空则由 resolve_db_path() 决定。
    """
    if db_path is None:
        db_path = resolve_db_path()

    if not db_path.exists():
        init_db(db_path)
        return

    conn = connect(db_path)
    try:
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """把已有的库升级到当前结构。

    每一步都写成幂等的：重复执行不会出错，也不会重复生效。
    这一点很重要 —— 迁移是在服务启动时自动跑的，
    没人能保证它只被执行一次（比如启动时崩了又重启）。

    Args:
        conn: 一个已建立的连接，来自 ensure_up_to_date()。
    """
    _migrate_appointments_table(conn)
    _ensure_indexes(conn)


def _migrate_appointments_table(conn: sqlite3.Connection) -> None:
    """给 appointments 补上 R5 新增的两列与 pending 状态。

    分两种情况：

    1. **只是缺列**（库是 R5 之后建的、或已被迁移过）：
       `ALTER TABLE ADD COLUMN` 就够了，代价极低。
    2. **缺 pending 这个状态值**：
       CHECK 约束是建表时写死的，**SQLite 无法用 ALTER 修改它**。
       只能走标准流程：建新表 → 复制数据 → 删旧表 → 改名。

    第二种为什么要建新表而不是直接改 sqlite_master：
    手工改 `sqlite_master` 里那句 SQL 是 SQLite 官方明确禁止的操作，
    改完库的**内部 schema cookie 与文本不同步**，
    后续任何 DDL 都可能让整个文件损坏。这种"能跑就行"的捷径，
    在一个要拿去答辩、随时可能被人多开几次的项目里是绝对不能走的。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'appointments'"
    ).fetchone()
    # 表不存在说明这个库还没初始化，交给 init_db 处理，这里不动手
    if row is None or row[0] is None:
        return

    existing_ddl = row[0]

    if "'pending'" in existing_ddl:
        # CHECK 已经含 pending，只需补两列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(appointments)")}
        for col in ("confirm_deadline", "checked_in_at"):
            if col not in cols:
                conn.execute(f"ALTER TABLE appointments ADD COLUMN {col} TEXT")
        return

    _rebuild_appointments(conn)


def _rebuild_appointments(conn: sqlite3.Connection) -> None:
    """用 schema.sql 里的新定义重建 appointments 表（保数据）。

    新表的 DDL **从 schema.sql 里现取**，不在本文件另抄一份：
    抄一份就意味着"改了建表语句要记得改两个地方"，
    而这类双份维护正是 R2 以来一直在消灭的东西。

    放在一个事务里：中途任何一步失败，库都回到原样。
    迁移写到一半的库比没迁移的库更危险 ——
    它看起来是新结构，实际上数据可能已经缺了一半。
    """
    schema_sql = (SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    new_ddl = _extract_table_ddl(schema_sql, "appointments").replace(
        "CREATE TABLE IF NOT EXISTS appointments (",
        "CREATE TABLE appointments__new (",
        1,
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(new_ddl)
        # 逐列复制而不是 INSERT INTO ... SELECT *：
        # 新旧表的列顺序可能因 ALTER 而不同，* 会把数据错位塞进错误的列。
        # 写全列名后，多出来的两列落 NULL（历史记录本就不涉及确认与签到）。
        conn.execute(
            "INSERT INTO appointments__new"
            " (id, user_id, schedule_id, seq_no, status, source,"
            "  created_at, cancelled_at)"
            " SELECT id, user_id, schedule_id, seq_no, status, source,"
            "        created_at, cancelled_at"
            "   FROM appointments"
        )
        conn.execute("DROP TABLE appointments")
        conn.execute("ALTER TABLE appointments__new RENAME TO appointments")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _extract_table_ddl(schema_sql: str, table: str) -> str:
    """从建表脚本里抠出某张表的完整 CREATE 语句。

    用括号配对来定位结尾，而不是找第一个 `);` ——
    表定义里有 `CHECK (...)` 这种带括号的子句，找第一个分号会提前截断。

    Args:
        schema_sql: schema.sql 的全文。
        table: 表名。

    Returns:
        完整的 `CREATE TABLE ... (...);` 语句。

    Raises:
        ValueError: 脚本里没有这张表的建表语句。说明脚本被改坏了。
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = schema_sql.find(marker)
    if start < 0:
        raise ValueError(f"schema.sql 里找不到 {table} 的建表语句")

    depth = 0
    i = start + len(marker) - 1  # 指向建表语句开头的那个 '('
    while i < len(schema_sql):
        ch = schema_sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return schema_sql[start : i + 2]  # 含结尾的 ');'


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """重建那两个唯一索引，并补上 R5 新增的普通索引。

    两个唯一索引**必须无条件 DROP 再 CREATE**：
    `CREATE UNIQUE INDEX IF NOT EXISTS` 只在索引不存在时建，
    索引已存在但 WHERE 条件过时，它一声不吭地跳过 ——
    于是旧库会一直带着"不含 pending"的旧约束，
    而 pending 记录恰恰是能绕过它插进去的那一种。

    普通索引用 IF NOT EXISTS 就够了：它们没有 WHERE 条件会变。
    """
    conn.execute("DROP INDEX IF EXISTS ux_appt_slot")
    conn.execute("DROP INDEX IF EXISTS ux_appt_user_slot")
    conn.execute(
        "CREATE UNIQUE INDEX ux_appt_slot ON appointments (schedule_id, seq_no)"
        f" WHERE {ACTIVE_STATUS_CLAUSE}"
    )
    conn.execute(
        "CREATE UNIQUE INDEX ux_appt_user_slot ON appointments (user_id, schedule_id)"
        f" WHERE {ACTIVE_STATUS_CLAUSE}"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_appointments_pending"
        " ON appointments (status, confirm_deadline)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_appointments_schedule"
        " ON appointments (schedule_id)"
    )


def _executescript(conn: sqlite3.Connection, sql_file: Path) -> None:
    """执行一个 SQL 脚本文件。

    Args:
        conn: 已建立的连接。
        sql_file: 脚本文件路径。

    Raises:
        FileNotFoundError: 脚本文件不存在，说明目录结构被破坏了。
    """
    if not sql_file.exists():
        raise FileNotFoundError(f"缺少 SQL 脚本: {sql_file}")

    # 用 utf-8 显式读取，避免 Windows 下默认编码把中文注释读成乱码
    sql = sql_file.read_text(encoding="utf-8")
    conn.executescript(sql)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """写事务。用法：

        with db.transaction(conn):
            create_booking(conn, user_id, schedule_id)

    用 `BEGIN IMMEDIATE` 而不是默认的 deferred 事务，原因很实在：

        deferred 事务要等到第一条写语句才去拿锁。两个事务若都先读了
        再想写，就会互相等对方手里的读锁，谁也进行不下去 —— 死锁。
        IMMEDIATE 是进屋就把钥匙攥在手里，后来者直接排队，
        代价只是并发度低一点，换来的是永远不会死锁。

    事务边界刻意留在路由层（谁开谁关），服务层只管在事务里做事。
    这样 R5 写并发测试时，可以在服务层外面随便套事务做断言。

    Args:
        conn: 由 get_conn() 提供的连接，isolation_level=None（自动提交），
            因此必须由本函数显式开启事务。
    """
    # 放在 try 外面：BEGIN 自身失败时，不能去 ROLLBACK 一个没开的事务
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖注入用的连接生成器。

    用法：在路由函数里声明 conn: sqlite3.Connection = Depends(get_conn)，
    FastAPI 会在请求进来时建立连接，请求结束后自动关闭。

    Yields:
        一个配置完备的数据库连接。
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
