"""号源状态判定（唯一入口）+ 全项目文案中心。

为什么需要这个模块，见 docs/DATA_MODEL.md 决策 2：

    「已满」与「候补中」不存进数据库，一律现算。

理由：两者的唯一区别是「有没有人在排队」，而排队人数是候补队列的
瞬时状态。若存成 schedules 表的一个字段，就意味着每次有人进队、出队、
转正，都要记得同步改它 —— 多一处会忘的地方。忘了改，页面就会显示
「已满」，但后面其实站着两个人。

本模块刻意做成**纯函数**：不碰数据库、不做 IO、不 import 任何东西。
好处是 R5 写单元测试时可以直接 import 进来断言，
不需要起服务、不需要造数据、不需要事务，测起来极快。
"""

from __future__ import annotations

# 号源状态的四种取值。刻意用模块级常量而不是裸字符串，
# 这样拼错时是 AttributeError，而不是悄悄返回一个不存在的状态。
STATUS_OPEN = "open"        # 有余号
STATUS_FULL = "full"        # 满了，且没人排队
STATUS_WAITING = "waiting"  # 满了，且有人在排队
STATUS_CLOSED = "closed"    # 医生停诊

# 状态 → 中文标签。
# 前端一律用后端给的 status_label，不许自己拼中文 —— 否则两边文案会不一致。
STATUS_LABELS = {
    STATUS_OPEN: "可约",
    STATUS_FULL: "已满",
    STATUS_WAITING: "候补中",
    STATUS_CLOSED: "已停诊",
}

# 合法状态取值的集合，供路由层校验 query 参数。
# 刻意用上面的常量拼出来，而不是把四个字符串再抄一遍 ——
# 将来新增第五种状态时只改这一处，校验、筛选、标签三边自动同步。
ALL_STATUSES = (STATUS_OPEN, STATUS_FULL, STATUS_WAITING, STATUS_CLOSED)

# 时段 → 中文标签。
PERIOD_LABELS = {
    "morning": "上午",
    "afternoon": "下午",
    "evening": "晚上",
}

# 下面三张表与号源状态无关，放在这里只有一个理由：
# **全项目的中文文案集中在一处**。
# 若把预约状态、候补状态、来源的文案散落到各自的路由文件里，
# 早晚会出现同一个状态在两处叫法不同的情况，
# 比如「我的预约」页写着「已取消」，列表页却写着「已退号」。
# 前端一律取后端给的 label，绝不自己拼中文。
APPOINTMENT_STATUS_LABELS = {
    "booked": "待就诊",
    # R5 新增：候补转正后等待本人确认。
    # 文案刻意写成"待确认"而不是"待就诊" —— 他此刻还没真正拿到号，
    # 写成待就诊会让人以为可以直接去，结果因为没确认被系统顺延掉了。
    "pending": "待确认",
    "completed": "已完成",
    "cancelled": "已取消",
    "no_show": "已爽约",
}
WAITLIST_STATUS_LABELS = {
    "waiting": "候补中",
    "promoted": "已转正",
    "cancelled": "已取消",
    "expired": "已失效",
}
SOURCE_LABELS = {
    "direct": "直接预约",
    "promoted": "候补转正",
}


def compute_status(
    booked_count: int,
    total_slots: int,
    is_closed: int | bool,
    waiting_count: int = 0,
) -> tuple[str, str, int]:
    """算出一条排班当前的状态。

    Args:
        booked_count: 已发出的号数，来自 schedules.booked_count。
        total_slots: 号源总数，来自 schedules.total_slots。
        is_closed: 是否停诊，来自 schedules.is_closed（SQLite 里是 0/1）。
        waiting_count: 排队人数，需要调用方现查候补表。

    Returns:
        (status, status_label, remaining) 三元组。

    判定顺序说明：
        停诊排在最前面。停诊是人为设定，优先级高于一切算术 ——
        即使号源一个都没发出去，停诊就是停诊，不能因为「还有 20 个空号」
        就显示成可约。
    """
    # remaining 刻意不做 max(0, ...) 钳位。
    # 数据库有 CHECK (booked_count <= total_slots) 保证它不会为负，
    # 真出现了负数说明记账写错了，那是 bug 而不是显示问题，应该让它暴露出来。
    remaining = total_slots - booked_count

    if is_closed:
        status = STATUS_CLOSED
    elif booked_count < total_slots:
        status = STATUS_OPEN
    elif waiting_count > 0:
        # 同样都是「满」，区别只在于后面有没有人排队
        status = STATUS_WAITING
    else:
        status = STATUS_FULL

    return status, STATUS_LABELS[status], remaining


# 同一套判定逻辑的 SQL 版本，只用于 WHERE 筛选。
#
# 存在的唯一理由：状态不落库，库里没有能直接筛的列，
# 所以要在 SQL 里按状态过滤，就只能在 SQL 里把状态再算一遍。
# 详见 docs/API.md 第 6 节「为什么状态判定有两份」。
#
# 两条铁的纪律：
#   1. 判定顺序与上面 compute_status 的 if/elif 逐条对齐
#      （停诊 → 有余号 → 有人排队 → 满），顺序错了结果就错；
#   2. 分支值用 f-string 引用模块顶部的 STATUS_* 常量，不硬编码字符串，
#      这样常量改了 SQL 自动跟上，杜绝第三处取值。
#
# 插值进去的全是模块内常量，不是用户输入，因此不存在注入风险。
STATUS_SQL_EXPR = f"""
CASE
    WHEN s.is_closed = 1                            THEN '{STATUS_CLOSED}'
    WHEN s.booked_count < s.total_slots             THEN '{STATUS_OPEN}'
    WHEN (SELECT COUNT(*) FROM waitlist w
           WHERE w.schedule_id = s.id
             AND w.status = 'waiting') > 0          THEN '{STATUS_WAITING}'
    ELSE '{STATUS_FULL}'
END
"""


def period_label(period: str) -> str:
    """把时段枚举转成中文标签。

    用 get 兜底：万一数据库里混入了契约外的值，至少页面还能显示原文，
    不会直接抛 KeyError 变成 500。
    """
    return PERIOD_LABELS.get(period, period)


# 下面三个函数与 period_label 同出一辙：都是「查表 + 用原文兜底」。
# 单独包成函数而不是让路由直接写 .get(...)，
# 是为了让路由层看不出底下是 dict 还是别的实现，方便将来换成多语言。


def appointment_status_label(status: str) -> str:
    """预约状态 → 中文标签。"""
    return APPOINTMENT_STATUS_LABELS.get(status, status)


def waitlist_status_label(status: str) -> str:
    """候补记录状态 → 中文标签。"""
    return WAITLIST_STATUS_LABELS.get(status, status)


def source_label(source: str) -> str:
    """预约来源 → 中文标签，前端用它标出「候补转正」的号。"""
    return SOURCE_LABELS.get(source, source)
