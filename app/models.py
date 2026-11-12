"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import AwareDatetime, BaseModel, Field, HttpUrl


class ProjectStatus(str, Enum):
    """投标状态。

    OPEN         可投标（存在有效截止时间，且未取消）
    CLOSED       不可投标（公告明确取消采购）
    PENDING      待核实（抓取/解析失败，或无法得到有效截止时间，不能把旧时间当最终期限）
    NOT_FOUND    列表页未发现与该项目编号相关的同站点公告
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class NoticeKind(str, Enum):
    ORIGINAL = "original"          # 原公告
    CORRECTION = "correction"      # 更正公告（含澄清、变更、延期）
    CANCELLATION = "cancellation"  # 取消公告（更正的一种，且明确取消采购）


class FetchRequest(BaseModel):
    """采集请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class NoticeRecord(BaseModel):
    """单条公告详情的解析结果（按发布时间排序）。"""

    url: HttpUrl
    kind: NoticeKind
    title: str
    published_at: datetime | None = None
    deadline: datetime | None = Field(
        None, description="该公告声明的投标截止时间；未提及则为 None，合并时沿用已生效时间"
    )
    cancelled: bool = False
    cancellation_reason: str | None = None


class SourceLink(BaseModel):
    """来源链接（列表页 + 参与合并的详情页）。"""

    role: str  # list / original / correction / cancellation
    url: HttpUrl
    title: str | None = None


class FetchResponse(BaseModel):
    """采集响应。"""

    project_code: str
    list_url: HttpUrl
    status: ProjectStatus
    can_bid: bool = Field(..., description="当前是否可投标")
    effective_deadline: datetime | None = Field(
        None, description="有效投标截止时间；待核实/未找到时为 None，绝不返回旧时间冒充最终期限"
    )
    cancelled: bool = False
    notices: list[NoticeRecord] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# 排期核查（开标场次同室时间交叠检测）
# --------------------------------------------------------------------------- #
class ScheduleCheckStatus(str, Enum):
    """排期核查状态。

    OK       全部目录页/详情页抓取成功、批次一致且复查通过，交叠结果可信
    PENDING  待核实：存在页面抓取/解析失败、批次不一致/复查变化、
             同一旧版出现两条后继改期等情况；此时不能据“无交叠”放行场地安排
    """

    OK = "ok"
    PENDING = "pending"


class ScheduleCheckRequest(BaseModel):
    """排期核查请求。"""

    bid_date: date = Field(..., description="开标日期（按场次开始时间所在本地日期过滤），如 2026-11-20")
    catalog_url: HttpUrl = Field(..., description="排期目录首页 URL，沿同站分页抓取")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class SessionRecord(BaseModel):
    """一条有效开标场次（改期归并后的当前有效版本）。"""

    notice_no: str = Field(..., description="公告编号")
    session_no: str = Field(..., description="场次编号")
    project_code: str = Field(..., description="项目编号")
    meeting_room: str = Field(..., description="会议室")
    starts_at: AwareDatetime = Field(..., description="带时区的开标开始时间")
    ends_at: AwareDatetime = Field(..., description="带时区的开标结束时间")
    batch: str = Field(..., description="发布批次标识")
    supersedes: str | None = Field(None, description="改期引用：所替换旧版的公告编号；原公告为 None")
    url: HttpUrl = Field(..., description="场次详情页来源链接")


class PendingSession(BaseModel):
    """待核实场次：同一旧版存在两条后继、悬空引用或该场次页面抓取/解析失败。"""

    notice_no: str | None = Field(None, description="公告编号；仅能从目录行得到时可能为 None")
    session_no: str | None = None
    project_code: str | None = None
    meeting_room: str | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    batch: str | None = None
    supersedes: str | None = None
    url: HttpUrl
    reason: str = Field(..., description="待核实原因")


class ScheduleConflict(BaseModel):
    """同一会议室时间交叠的一对有效场次。"""

    meeting_room: str
    project_codes: list[str] = Field(..., description="交叠双方项目编号（按公告编号排序）")
    session_nos: list[str] = Field(..., description="交叠双方场次编号")
    overlap_starts_at: AwareDatetime = Field(..., description="交叠时段开始时间（取两者较晚开始）")
    overlap_ends_at: AwareDatetime = Field(..., description="交叠时段结束时间（取两者较早结束）")
    sources: list[HttpUrl] = Field(..., description="两个场次详情页来源链接")


class ScheduleCheckResponse(BaseModel):
    """排期核查响应。"""

    bid_date: date
    catalog_url: HttpUrl
    status: ScheduleCheckStatus
    conflict_free: bool = Field(
        ..., description="是否确认无交叠；仅在 status=ok 且无交叠时为 true，待核实时恒为 false"
    )
    batch: str | None = Field(None, description="本次目录首页声明的发布批次标识")
    conflicts: list[ScheduleConflict] = Field(default_factory=list)
    valid_sessions: list[SessionRecord] = Field(default_factory=list)
    pending_sessions: list[PendingSession] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime
