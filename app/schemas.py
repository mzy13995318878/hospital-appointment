"""请求与响应的 Pydantic 模型。

本文件是 docs/API.md 第 4 节的代码化版本 —— 契约里写了哪些字段，
这里就有哪些字段，一个不多一个不少。

字段类型刻意用 Literal 而不是裸 str：
    比如 status 只允许取 open/full/waiting/closed 四个值。
    如果不小心返回了 "booked" 这种拼错的值，Pydantic 会直接抛错，
    而不是让错误数据一路流到前端变成一个空白标签。
    这是「让错误尽早暴露」原则的具体做法。

另外本文件只定义数据结构，不做任何业务判断。
"""

from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

# 号源状态。与 services/slot_status.py 里的常量一一对应，
# 这里再写一遍是为了让 OpenAPI 文档能展示出四个可选值。
ScheduleStatus = Literal["open", "full", "waiting", "closed"]
# 时段
Period = Literal["morning", "afternoon", "evening"]
# 用户状态
UserStatus = Literal["active", "blacklisted"]
# 预约状态。R5 新增 `pending`：候补者被递补选中、等待确认的那段时间。
# 它**占着号**（见 docs/DATA_MODEL.md 2.4），所以对外也必须是一个正式状态，
# 不能当作"还没生效的临时值"忽略掉 —— 忽略它，前端就会少显示一个号。
AppointmentStatus = Literal["booked", "pending", "cancelled", "completed", "no_show"]
# 候补记录状态
WaitlistStatus = Literal["waiting", "promoted", "cancelled", "expired"]
# 预约来源
BookingSource = Literal["direct", "promoted"]

T = TypeVar("T")


class OkResponse(BaseModel, Generic[T]):
    """成功响应的统一外壳。

    成功一律 {"ok": true, "data": <具体结构>}，
    失败一律 {"ok": false, "error": {...}}（见 app/errors.py）。
    前端只判断 ok 的真假，就能决定去取 data 还是取 error，
    不用去猜 HTTP 状态码的语义。

    做成泛型是为了让 /docs 里能看到每个接口 data 的真实结构，
    而不是含糊的 object。
    """

    ok: bool = True
    data: T


# ------------------------------------------------------------
# 4.1 身份
# ------------------------------------------------------------


class LoginRequest(BaseModel):
    """登录（也是隐式注册）请求。"""

    name: str = Field(..., min_length=1, max_length=20, description="姓名")
    id_card_last4: str = Field(..., description="身份证后 4 位")
    phone: str = Field(..., description="手机号")


class LoginData(BaseModel):
    """登录响应。

    is_new 用于让前端区分「欢迎回来」和「已为你自动注册」，
    这两种场景的提示文案不一样。
    """

    user_id: int
    name: str
    is_new: bool


class MeData(BaseModel):
    """当前登录用户的信息。

    no_show_count 是加分项（爽约统计、自动拉黑）要用到的字段，
    R2 先返回出来，R5 才真正往里加逻辑。
    """

    user_id: int
    name: str
    status: UserStatus
    no_show_count: int


# ------------------------------------------------------------
# 4.2 排班与医生
# ------------------------------------------------------------


class DepartmentItem(BaseModel):
    """科室下拉框的一项。

    doctor_count 只统计**有排班**的医生，与接口契约一致：
    某科室下所有医生恰好都没排班时，整个科室不出现在选项里，
    免得用户选了它却看到一片空白。
    """

    department: str
    doctor_count: int


class ScheduleItem(BaseModel):
    """排班列表的一项。

    注意 remaining、status、status_label、waiting_count 这四个字段
    数据库里都没有，全是查出来之后现算的（见 services/slot_status.py）。
    total_slots 与 booked_count 是库里的原始记账，一并返回，
    是为了让前端能显示「25/40」这种分数形式。
    """

    schedule_id: int
    doctor_id: int
    doctor_name: str
    title: str
    department: str
    work_date: str
    period: Period
    period_label: str
    total_slots: int
    booked_count: int
    remaining: int
    waiting_count: int
    status: ScheduleStatus
    status_label: str


class DoctorDetail(BaseModel):
    """医生详情。

    schedules 内嵌排班列表，与列表页共用 ScheduleItem 结构，
    这样前端渲染两种卡片时可以复用同一段代码。
    """

    id: int
    name: str
    title: str
    department: str
    intro: str
    specialty: str
    schedules: list[ScheduleItem]


# ------------------------------------------------------------
# 4.3 预约与候补
# ------------------------------------------------------------


class BookRequest(BaseModel):
    """预约请求。

    只有 schedule_id 一个字段：用户身份从请求头 X-User-Id 拿，
    不放在请求体里 —— 否则前端可以随便冒充别人约号。
    """

    schedule_id: int


class BookedData(BaseModel):
    """预约成功，拿到了号。"""

    type: Literal["booked"] = "booked"
    appointment_id: int
    seq_no: int
    message: str


class WaitlistedData(BaseModel):
    """号已满，转入候补队列。"""

    type: Literal["waitlisted"] = "waitlisted"
    waitlist_id: int
    position: int
    message: str


# 判别联合：让 /docs 里清清楚楚显示这个接口有两种返回形态。
# 前端靠 type 字段决定显示「预约成功」还是「已排队」，
# 不必去猜字段在不在。
BookData = Annotated[BookedData | WaitlistedData, Field(discriminator="type")]


class PromotedInfo(BaseModel):
    """递补的人是谁、接过了哪个号。

    R5 起递补出来的是 `pending`（待确认），所以多了两个字段：

        pending           —— 是否需要他确认。为 True 时这个号只是"留给他"
        confirm_deadline  —— 确认截止时刻，前端用它做倒计时

    保留 `pending` 这个布尔而不是让前端去猜：
    将来若改成"某些递补不需要确认"（比如现场人工递补），
    前端那套判断逻辑不用跟着改。
    """

    user_name: str
    seq_no: int
    waitlist_id: int
    appointment_id: int
    pending: bool = True
    confirm_deadline: str | None = None


class CancelData(BaseModel):
    """退号结果。

    promoted 为 None 表示队列里没人，号源回到可约池。
    """

    released_seq_no: int
    promoted: PromotedInfo | None = None
    message: str


class LeaveWaitlistData(BaseModel):
    """放弃候补的结果。

    刻意只回 id 和一句话：前端拿到后把那张卡片移除就够了，
    不需要知道自己退出后队列变成了什么样 —— 那是别人的名次，与他无关。
    """

    waitlist_id: int
    schedule_id: int
    message: str


class ConfirmData(BaseModel):
    """加分项 2：确认一个待确认的号。"""

    appointment_id: int
    seq_no: int
    message: str


class CheckInData(BaseModel):
    """加分项 3：签到结果。"""

    appointment_id: int
    seq_no: int
    checked_in_at: str
    message: str


class SettleData(BaseModel):
    """加分项 2/3：一次惰性结算的结果，主要给演示用。

    schedules 是"扫了多少个排班"，而不是库里的排班总数 ——
    扫的只是**可能有事发生**的那些（有 pending 的、或有过期未签到的）。
    返回它，是为了让演示时能一眼看出"这次结算确实动到了东西"。
    """

    promotion_expired: int
    no_show: int
    blacklisted: int
    schedules: int


class BlacklistData(BaseModel):
    """加分项 5：拉黑 / 解除拉黑的结果。"""

    user_id: int
    status: UserStatus
    no_show_count: int
    message: str


class MyAppointmentItem(BaseModel):
    """我的预约里的一条记录。

    status_label / source_label 由后端给出，
    前端不用自己准备「待就诊」「候补转正」这套中文。

    三个 `can_*` 同理：该显示哪个按钮由后端说了算。
    尤其 `can_check_in` 依赖「今天是不是就诊日」，
    这个判断必须用**服务端的时钟**，前端拿本地时钟比，
    用户改一下系统时间就能骗出一个签到按钮。
    """

    id: int
    schedule_id: int
    doctor_name: str
    department: str
    work_date: str
    period_label: str
    seq_no: int
    status: AppointmentStatus
    status_label: str
    source: BookingSource
    source_label: str
    created_at: str
    confirm_deadline: str | None = None
    checked_in_at: str | None = None
    can_confirm: bool = False
    can_check_in: bool = False
    can_cancel: bool = False


class MyWaitlistItem(BaseModel):
    """我的候补里的一条记录。

    position 是**当前名次**，不是库里存的排序键 ——
    队首转正后不会把后面所有人的排序键减一（理由见
    services/booking.py 里 rank_in_waitlist 的注释）。
    """

    id: int
    schedule_id: int
    doctor_name: str
    department: str
    work_date: str
    period_label: str
    position: int
    status: WaitlistStatus
    status_label: str
    joined_at: str


class MyData(BaseModel):
    """我的预约页的整包数据。

    预约与候补一次性返回，前端不用发两个请求。
    """

    appointments: list[MyAppointmentItem]
    waitlist: list[MyWaitlistItem]
