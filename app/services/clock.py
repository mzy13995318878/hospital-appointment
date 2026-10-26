"""可注入时钟 —— 全项目唯一的时间出口。

为什么需要这个文件：

    R5 的五个加分项里有四个要判断"现在几点"：
        转正确认超时（15 分钟）、退号冷却（10 分钟）、
        取消时限（开诊前 2 小时）、爽约结算（日期已过）。

    项目原本的时间来源是直接写进 SQL 的 `date('now','localtime')`，
    它有两个问题：
        1. 只有日期粒度，没有时刻；
        2. **它没法被测试控制** —— 要验证"15 分钟不确认就顺延"，
           总不能让测试真等一刻钟。

    于是把时间收成一个出口：业务代码一律调 `now_sql()` / `today_sql()`，
    不再手写 `datetime('now','localtime')`。
    测试里 `freeze(...)` 一下，整个系统的"现在"就停在你指定的那一刻。

为什么不直接 monkeypatch `datetime`：

    那要 patch 掉 `datetime.datetime` 这个类本身，而 sqlite3 模块内部、
    logging 模块、以及任何第三方库都可能用到它，副作用不可控。
    更关键的是 —— 我们要控制的根本不是 Python 的时间，
    而是 **SQL 里的那个 now**。所以时钟出口本来就该是
    一个「生成 SQL 片段」的东西，而不是「生成 datetime 对象」的东西。

为什么冻结值可以直接拼进 SQL（注入风险的说明）：

    `now_sql()` 在冻结时返回的是 `'2026-10-25 22:00:00'` 这种字面量，
    它由 `strftime` 从一个 datetime 对象格式化而来 ——
    能进到这里的值只有两种：strftime 的输出，或者模块里写死的常量。
    没有任何一条路径能把外部输入送到这里。
    与 `slot_status.STATUS_SQL_EXPR` 里插值常量是同一个道理。

并发说明：

    冻结状态是模块级全局的。常规测试串行执行，不会互相干扰；
    R5 的并发测试跑在**独立进程**里（见 tests/conftest.py 的 LiveServer），
    也碰不到这个全局变量。因此这里刻意不加锁 ——
    为一个不会发生的场景加保护，反而会让人误以为它可能发生。
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 冻结后的"现在"。None 表示跟随真实时间。
_frozen: datetime | None = None

# strftime 的格式与 SQLite 的 datetime() 输出格式严格一致。
# 不一致的后果很隐蔽：SQL 里比较的是字符串，
# '2026-10-25 9:00:00' 会小于 '2026-10-25 10:00:00'（字符串逐位比较），
# 于是"还没到截止时间"被算成"已经超了"。
_FMT = "%Y-%m-%d %H:%M:%S"

# 未冻结时用的 SQL 表达式。
# localtime 不能省：SQLite 的 datetime('now') 按 UTC 算，
# 东八区会差 8 小时，演示时会出现"凌晨 3 点退的号"这种穿帮数据。
_REAL_NOW_SQL = "datetime('now','localtime')"
_REAL_TODAY_SQL = "date('now','localtime')"


def now() -> datetime:
    """当前的"现在"（datetime 对象）。冻结时返回冻结值。

    主要用于测试里构造相对时间，业务代码优先用 `now_sql()`
    —— 判断交给 SQL 做，才能保证 Python 与 SQL 看到的是同一个时钟。
    """
    return _frozen if _frozen is not None else datetime.now()


def now_sql() -> str:
    """可直接嵌进 SQL 的「当前时刻」表达式。"""
    if _frozen is None:
        return _REAL_NOW_SQL
    return "'" + _frozen.strftime(_FMT) + "'"


def today_sql() -> str:
    """可直接嵌进 SQL 的「今天」表达式（日期，无时刻）。"""
    if _frozen is None:
        return _REAL_TODAY_SQL
    return "'" + _frozen.strftime("%Y-%m-%d") + "'"


def shift_sql(minutes: int) -> str:
    """「当前时刻 + minutes 分钟」的 SQL 表达式。

    用于算 `confirm_deadline` 这类"从现在起多久之内"。
    两种形态都合法：
        未冻结：datetime(datetime('now','localtime'), '+15 minutes')
        冻结后：datetime('2026-10-25 22:00:00', '+15 minutes')

    minutes 由内部常量给出（policy 里那几个阈值），不是用户输入。
    """
    return f"datetime({now_sql()}, '{minutes:+d} minutes')"


def freeze(moment: datetime) -> datetime:
    """把整个系统的"现在"冻结到指定时刻。

    Returns:
        传入的时刻，方便写成 `t0 = clock.freeze(...)`。
    """
    global _frozen
    _frozen = moment
    return moment


def advance(minutes: int) -> datetime:
    """把"现在"往前推 minutes 分钟（会自动进入冻结状态）。

    基准是当前的 `now()`：已冻结就从冻结值继续推，
    未冻结就先冻结在真实时刻再推，然后一直冻着。
    这样测试里连续两次 advance(10) 得到的是确定的 +20 分钟，
    不会因为两次调用之间真实时间走了几毫秒而漂移。
    """
    return freeze(now() + timedelta(minutes=minutes))


def unfreeze() -> None:
    """恢复真实时间。测试收尾时必须调用，否则会污染后续用例。"""
    global _frozen
    _frozen = None


def is_frozen() -> bool:
    """当前是否处于冻结状态（测试自检用）。"""
    return _frozen is not None
