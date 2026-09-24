"""FastAPI 入口：提交翻页规划、按 ID 查询结果、排练录音时间戳定位。"""

from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .planner import Plan, PlanningError, plan_pages
from .rehearsal import (
    Location,
    RehearsalError,
    TurnCue,
    TurnSlot,
    prepare_rehearsal,
)
from .storage import PlanStore

DATA_DIR = os.environ.get("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "plans.db")

store: Optional[PlanStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    os.makedirs(DATA_DIR, exist_ok=True)
    store = PlanStore(DB_PATH)  # 启动时完成建表初始化
    yield


app = FastAPI(
    title="演出乐谱翻页点规划 API",
    version="1.0.0",
    lifespan=lifespan,
)


def _json_safe(value: Any) -> Any:
    """把经 jsonable_encoder 归一化后的结构中 NaN/Infinity 替换为字符串。

    否则请求体里夹带非有限浮点（如 NaN）时，422 响应在回显原始输入阶段
    反而无法序列化，会错误地退化成 500。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": _json_safe(jsonable_encoder(exc.errors()))},
    )


# ---------------------------------------------------------------- 数据模型


class PlanRequest(BaseModel):
    """提交规划的请求体。"""

    model_config = ConfigDict(extra="forbid")

    measure_heights: List[float] = Field(
        ..., description="依次排列的每小节高度（与页面可用高度同单位）"
    )
    silence_seconds: List[float] = Field(
        ..., description="每小节之后可供翻页的静默秒数"
    )
    page_height: float = Field(..., description="页面可用高度")
    min_turn_time: float = Field(..., description="最低翻页时间（秒）")
    required_breaks: List[int] = Field(
        default_factory=list,
        description="必翻位置：新页起始小节的 0 基序号（合法范围 1..小节数-1），"
        "这些位置必须断页；省略或为空表示无必翻约束",
    )
    forbidden_breaks: List[int] = Field(
        default_factory=list,
        description="禁翻位置：新页起始小节的 0 基序号（合法范围 1..小节数-1），"
        "这些位置不得断页；省略或为空表示无禁翻约束",
    )

    @field_validator("required_breaks", "forbidden_breaks", mode="before")
    @classmethod
    def _reject_non_integer_elements(cls, value: object) -> object:
        """序号必须是严格整数：拒绝 1.5 之类浮点（pydantic 默认会强转）与布尔。"""
        if isinstance(value, list):
            for b in value:
                if not isinstance(b, int) or isinstance(b, bool):
                    raise ValueError(f"断页序号必须为整数（收到 {b!r}）")
        return value

    @model_validator(mode="after")
    def _validate(self) -> "PlanRequest":
        if not self.measure_heights:
            raise ValueError("measure_heights 不能为空")
        if len(self.silence_seconds) != len(self.measure_heights):
            raise ValueError(
                "silence_seconds 长度必须与 measure_heights 一致（每小节一个静默值）"
            )
        if any(h <= 0 for h in self.measure_heights):
            raise ValueError("measure_heights 中每个小节高度必须为正数")
        if any(s < 0 for s in self.silence_seconds):
            raise ValueError("silence_seconds 中静默秒数不能为负数")
        if self.page_height <= 0:
            raise ValueError("page_height 必须为正数")
        if self.min_turn_time < 0:
            raise ValueError("min_turn_time 不能为负数")

        n = len(self.measure_heights)

        def _check_breaks(name: str, values: List[int]) -> set[int]:
            checked: set[int] = set()
            for b in values:
                if b < 1 or b > n - 1:
                    raise ValueError(
                        f"{name} 含越界序号 {b}：合法范围为 1..{n - 1}"
                        "（0 是首页起点、末尾序号代表文档结束，均不能作为断页位置）"
                    )
                if b in checked:
                    raise ValueError(f"{name} 含重复序号 {b}")
                checked.add(b)
            return checked

        required = _check_breaks("required_breaks", self.required_breaks)
        forbidden = _check_breaks("forbidden_breaks", self.forbidden_breaks)
        clash = required & forbidden
        if clash:
            b = sorted(clash)[0]
            raise ValueError(
                f"序号 {b} 同时出现在 required_breaks 与 forbidden_breaks 中，"
                "必翻与禁翻约束不能交叉"
            )
        return self


class PageOut(BaseModel):
    page_index: int = Field(..., description="页码（从 1 开始）")
    start_measure: int = Field(..., description="起始小节序号（0 基）")
    end_measure: int = Field(..., description="结束小节序号（0 基，闭区间）")
    measure_count: int
    used_height: float
    turn_silence: Optional[float] = Field(
        None, description="本页之后翻页点的静默秒数；最后一页为 null"
    )


class PlanResult(BaseModel):
    page_count: int
    shortest_turn_silence: Optional[float] = Field(
        None, description="所有翻页点中最短的静默秒数；单页时为 null"
    )
    breaks: List[int] = Field(..., description="断页序号序列（新页起始小节，0 基）")
    pages: List[PageOut]


class PlanResponse(BaseModel):
    id: str
    created_at: str
    request: PlanRequest
    result: PlanResult


def _plan_to_result(plan: Plan) -> PlanResult:
    return PlanResult(
        page_count=plan.page_count,
        shortest_turn_silence=plan.shortest_turn_silence,
        breaks=plan.breaks,
        pages=[PageOut(**vars(p)) for p in plan.pages],
    )


# ---------------------------------------- 排练录音时间戳定位


class RepeatSectionIn(BaseModel):
    """一个重复段：起止小节均含（0 基），演奏 2~4 遍。"""

    model_config = ConfigDict(extra="forbid")

    start_measure: int = Field(..., description="重复段起始小节（0 基，含）")
    end_measure: int = Field(..., description="重复段结束小节（0 基，含）")
    play_count: int = Field(..., description="整段演奏次数（2~4，首尾两遍均完整演奏）")

    @model_validator(mode="after")
    def _validate(self) -> "RepeatSectionIn":
        if self.start_measure < 0 or self.end_measure < 0:
            raise ValueError("重复段起止小节不能为负数")
        if self.start_measure > self.end_measure:
            raise ValueError(
                f"重复段起始小节（{self.start_measure}）不能晚于结束小节"
                f"（{self.end_measure}）"
            )
        if not 2 <= self.play_count <= 4:
            raise ValueError(
                f"重复段（小节 {self.start_measure}..{self.end_measure}）演奏次数"
                f"必须在 2~4 之间（实际 {self.play_count}）"
            )
        return self


class RehearsalParams(BaseModel):
    """排练请求共用参数：每小节拍数、速度与互不相交的重复段。"""

    model_config = ConfigDict(extra="forbid")

    beats_per_measure: List[float] = Field(
        ...,
        description="与原小节等长的每小节拍数，长度必须与规划小节数一致、元素为正",
    )
    tempo_bpm: float = Field(..., description="每分钟拍数，必须为正")
    repeats: List[RepeatSectionIn] = Field(
        default_factory=list,
        description="若干不相交且不嵌套的重复段（起止小节均含、演奏次数 2~4）",
    )

    @model_validator(mode="after")
    def _validate(self) -> "RehearsalParams":
        if any(b != b or b in (float("inf"), float("-inf")) for b in self.beats_per_measure):
            raise ValueError("beats_per_measure 中每个拍数必须是有限数值")
        if any(b <= 0 for b in self.beats_per_measure):
            raise ValueError("beats_per_measure 中每个小节拍数必须为正数")
        if self.tempo_bpm != self.tempo_bpm or self.tempo_bpm in (
            float("inf"),
            float("-inf"),
        ):
            raise ValueError("tempo_bpm 必须是有限数值")
        if self.tempo_bpm <= 0:
            raise ValueError("tempo_bpm（每分钟拍数）必须为正数")
        return self


class RehearsalLocateRequest(RehearsalParams):
    """排练录音时间戳定位请求体。"""

    timestamps: List[float] = Field(
        ..., description="排练录音时间戳数组（秒，以演奏开始为 0）"
    )

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "RehearsalLocateRequest":
        if any(t != t or t in (float("inf"), float("-inf")) for t in self.timestamps):
            raise ValueError("时间戳必须是有限数值（不接受 NaN 或 Infinity）")
        if any(t < 0 for t in self.timestamps):
            raise ValueError("时间戳不能为负数（以演奏开始为 0）")
        return self


class LocationOut(BaseModel):
    measure: int = Field(..., description="原谱小节号（0 基）")
    playthrough: int = Field(
        ..., description="所属重复段内第几遍（1 基）；非重复段小节恒为 1"
    )
    beat_offset: float = Field(..., description="该小节内的拍数偏移（自小节起点起）")
    page_index: int = Field(..., description="已存方案的页码（1 基）")


class RehearsalLocateResponse(BaseModel):
    id: str
    request: RehearsalLocateRequest
    total_duration: float = Field(
        ..., description="按重复段展开后的演奏总时长（秒）"
    )
    locations: List[LocationOut] = Field(
        ..., description="与 timestamps 一一对应的定位结果"
    )


def _location_to_out(loc: Location) -> LocationOut:
    return LocationOut(
        measure=loc.measure,
        playthrough=loc.playthrough,
        beat_offset=loc.beat_offset,
        page_index=loc.page_index,
    )


# ---------------------------------------- 排练翻页提示


class RehearsalCuesRequest(RehearsalParams):
    """排练翻页提示请求体（与定位接口共用拍数、速度、重复段参数）。"""


class TurnCueOut(BaseModel):
    """一次翻页提示：相邻演奏小节所属页码变化时生成。"""

    boundary_seconds: float = Field(
        ..., description="翻页边界时刻（秒，以演奏开始为 0）：前一小节结束处"
    )
    from_page: int = Field(..., description="翻页前页码（前一小节所在页，1 基）")
    to_page: int = Field(..., description="翻页后页码（后一小节所在页，1 基）")
    from_measure: int = Field(..., description="前一小节的原谱小节号（0 基）")
    to_measure: int = Field(..., description="后一小节的原谱小节号（0 基）")
    from_playthrough: int = Field(
        ..., description="前一小节所属重复段内第几遍（1 基）；非重复段为 1"
    )
    to_playthrough: int = Field(
        ..., description="后一小节所属重复段内第几遍（1 基）；非重复段为 1"
    )
    direction: str = Field(
        ..., description="翻页方向：forward 前进翻页，backward 回翻"
    )
    silence_seconds: float = Field(
        ..., description="前一小节之后的静默秒数（取自已存规划）"
    )
    sufficient: bool = Field(
        ..., description="该静默是否不低于方案的最低翻页时间"
    )


class RehearsalCuesResponse(BaseModel):
    id: str
    request: RehearsalCuesRequest
    min_turn_time: float = Field(
        ..., description="已存方案的最低翻页时间（秒），sufficient 的比较基准"
    )
    total_duration: float = Field(
        ..., description="按重复段展开后的演奏总时长（秒）"
    )
    cues: List[TurnCueOut] = Field(..., description="按边界时刻升序的翻页提示")


def _cue_to_out(cue: TurnCue) -> TurnCueOut:
    return TurnCueOut(
        boundary_seconds=cue.boundary_seconds,
        from_page=cue.from_page,
        to_page=cue.to_page,
        from_measure=cue.from_measure,
        to_measure=cue.to_measure,
        from_playthrough=cue.from_playthrough,
        to_playthrough=cue.to_playthrough,
        direction=cue.direction,
        silence_seconds=cue.silence_seconds,
        sufficient=cue.sufficient,
    )


# ---------------------------------------- 排练翻页动作空档


class HandsBusyIn(BaseModel):
    """左右手在各原谱小节内的忙碌区间。

    每个字段是与原谱小节等长的数组；第 i 个元素为若干
    ``[起拍, 止拍)`` 忙碌区间，允许乱序与重叠。
    """

    model_config = ConfigDict(extra="forbid")

    # 注意：内层各小节的区间数组含空数组时形如 [[], [0, 2]]，pydantic 2.10 对
    # List[List[float]] 的异构嵌套推断存在缺陷（会把空内层数组错当数值层），
    # 故此处先按 List[Any] 原样接收，结构与类型全部在下方校验器中严格检查。
    left: List[Any] = Field(
        ..., description="左手各小节的忙碌区间（[起拍, 止拍)，左闭右开）"
    )
    right: List[Any] = Field(
        ..., description="右手各小节的忙碌区间（[起拍, 止拍)，左闭右开）"
    )

    @field_validator("left", "right")
    @classmethod
    def _validate_intervals(cls, value: List[Any]) -> List[Any]:
        for measure, intervals in enumerate(value):
            if not isinstance(intervals, list):
                raise ValueError(f"第 {measure + 1} 小节必须是若干 [起拍, 止拍) 区间")
            for idx, interval in enumerate(intervals):
                if not isinstance(interval, list) or len(interval) != 2:
                    raise ValueError(
                        f"第 {measure + 1} 小节第 {idx + 1} 个区间必须是"
                        "恰好两个数 [起拍, 止拍)"
                    )
                start, end = interval
                if isinstance(start, bool) or isinstance(end, bool):
                    raise ValueError(
                        f"第 {measure + 1} 小节第 {idx + 1} 个区间的"
                        "起拍、止拍必须是数值（不接受布尔值）"
                    )
                if not isinstance(start, (int, float)) or not isinstance(
                    end, (int, float)
                ):
                    raise ValueError(
                        f"第 {measure + 1} 小节第 {idx + 1} 个区间的"
                        "起拍、止拍必须是数值"
                    )
                for label, beat in (("起拍", start), ("止拍", end)):
                    if beat != beat or beat in (float("inf"), float("-inf")):
                        raise ValueError(
                            f"第 {measure + 1} 小节第 {idx + 1} 个区间的"
                            f"{label}必须是有限数值（不接受 NaN 或 Infinity）"
                        )
        return value


class TurnSlotsRequest(RehearsalParams):
    """排练翻页动作空档请求体（沿用排练接口的拍数、速度、重复段参数）。"""

    turn_seconds: float = Field(
        ..., description="完整翻页动作所需秒数，必须为正"
    )
    hands_busy: HandsBusyIn = Field(
        ...,
        description="左右手各原谱小节的忙碌区间（[起拍, 止拍)，允许乱序与重叠）",
    )

    @model_validator(mode="after")
    def _validate_turn_seconds(self) -> "TurnSlotsRequest":
        value = self.turn_seconds
        if isinstance(value, bool):
            raise ValueError("turn_seconds 必须是数值（不接受布尔值）")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("turn_seconds 必须是有限数值（不接受 NaN 或 Infinity）")
        if value <= 0:
            raise ValueError(f"turn_seconds 必须为正数（实际 {value:g}）")
        return self


class TurnSlotOut(BaseModel):
    """一次跨页边界上的翻页动作安排。"""

    boundary_seconds: float = Field(
        ..., description="翻页边界时刻（秒，以演奏开始为 0）：前一小节结束处"
    )
    from_page: int = Field(..., description="翻页前页码（前一小节所在页，1 基）")
    to_page: int = Field(..., description="翻页后页码（后一小节所在页，1 基）")
    from_measure: int = Field(..., description="前一小节的原谱小节号（0 基）")
    to_measure: int = Field(..., description="后一小节的原谱小节号（0 基）")
    from_playthrough: int = Field(
        ..., description="前一小节所属重复段内第几遍（1 基）；非重复段为 1"
    )
    to_playthrough: int = Field(
        ..., description="后一小节所属重复段内第几遍（1 基）；非重复段为 1"
    )
    available_hands: List[str] = Field(
        ...,
        description="存在能容纳完整翻页动作的空闲窗的手（left 左手、right 右手），"
        "固定左手在前、右手在后",
    )
    hand: Optional[str] = Field(
        ...,
        description="实际选用的手（left / right）；无可用窗时为 null",
    )
    action_start_seconds: Optional[float] = Field(
        ..., description="翻页动作开始时刻（秒，以演奏开始为 0）；不可行为 null"
    )
    action_end_seconds: Optional[float] = Field(
        ..., description="翻页动作结束时刻（秒，不晚于边界时刻）；不可行为 null"
    )
    feasible: bool = Field(
        ..., description="是否存在能容纳完整翻页动作的连续空闲窗"
    )


class TurnSlotsResponse(BaseModel):
    id: str
    request: TurnSlotsRequest
    turn_seconds: float = Field(..., description="本次请求的完整翻页动作时长（秒）")
    total_duration: float = Field(
        ..., description="按重复段展开后的演奏总时长（秒）"
    )
    slots: List[TurnSlotOut] = Field(
        ..., description="按边界时刻升序的翻页动作安排（含不可行边界）"
    )


def _slot_to_out(slot: TurnSlot) -> TurnSlotOut:
    return TurnSlotOut(
        boundary_seconds=slot.boundary_seconds,
        from_page=slot.from_page,
        to_page=slot.to_page,
        from_measure=slot.from_measure,
        to_measure=slot.to_measure,
        from_playthrough=slot.from_playthrough,
        to_playthrough=slot.to_playthrough,
        available_hands=list(slot.available_hands),
        hand=slot.hand,
        action_start_seconds=slot.action_start_seconds,
        action_end_seconds=slot.action_end_seconds,
        feasible=slot.feasible,
    )


# ---------------------------------------------------------------- 接口


@app.post("/api/v1/plans", response_model=PlanResponse, status_code=201)
def create_plan(payload: PlanRequest) -> PlanResponse:
    """提交一次翻页规划，返回最优方案并保存到本地。"""
    try:
        plan = plan_pages(
            heights=payload.measure_heights,
            silences=payload.silence_seconds,
            page_height=payload.page_height,
            min_turn_time=payload.min_turn_time,
            required_breaks=payload.required_breaks,
            forbidden_breaks=payload.forbidden_breaks,
        )
    except PlanningError as exc:
        # 单小节超高 / 必翻点静默不足 / 无合法方案：语义错误，明确报错
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = _plan_to_result(plan)
    assert store is not None
    plan_id = store.save(payload.model_dump(), result.model_dump())
    record = store.get(plan_id)
    assert record is not None
    return PlanResponse(
        id=record["id"],
        created_at=record["created_at"],
        request=payload,
        result=result,
    )


@app.get("/api/v1/plans/{plan_id}", response_model=PlanResponse)
def get_plan(plan_id: str) -> PlanResponse:
    """按 ID 查询已保存的规划结果。"""
    assert store is not None
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划结果不存在：{plan_id}")
    return PlanResponse(
        id=record["id"],
        created_at=record["created_at"],
        request=PlanRequest(**record["request"]),
        result=PlanResult(**record["result"]),
    )


@app.post(
    "/api/v1/plans/{plan_id}/rehearsal/locate",
    response_model=RehearsalLocateResponse,
)
def locate_rehearsal_timestamps(
    plan_id: str, payload: RehearsalLocateRequest
) -> RehearsalLocateResponse:
    """按规划 ID 对排练录音时间戳做定位。

    依据提交的每小节拍数、速度与重复段，按谱面顺序展开实际演奏序列，
    以左闭右开区间把每个时间戳定位到原谱小节、重复遍号、拍数偏移和页码。
    """
    assert store is not None
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划结果不存在：{plan_id}")

    plan_request = PlanRequest(**record["request"])
    plan_result = PlanResult(**record["result"])
    page_starts = [p.start_measure for p in plan_result.pages]

    try:
        rehearsal = prepare_rehearsal(
            measure_count=len(plan_request.measure_heights),
            beats_per_measure=payload.beats_per_measure,
            tempo_bpm=payload.tempo_bpm,
            repeats=[
                (r.start_measure, r.end_measure, r.play_count)
                for r in payload.repeats
            ],
            page_starts=page_starts,
        )
        locations = rehearsal.locate_all(payload.timestamps)
    except RehearsalError as exc:
        # 数组长度不符 / 重复段重叠 / 时间戳越界等语义错误：明确报错
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RehearsalLocateResponse(
        id=record["id"],
        request=payload,
        total_duration=rehearsal.total_duration,
        locations=[_location_to_out(loc) for loc in locations],
    )


@app.post(
    "/api/v1/plans/{plan_id}/rehearsal/cues",
    response_model=RehearsalCuesResponse,
)
def plan_rehearsal_cues(
    plan_id: str, payload: RehearsalCuesRequest
) -> RehearsalCuesResponse:
    """为已保存方案生成排练翻页提示。

    复用定位接口的演奏顺序与时间轴：按每小节拍数、速度与互不相交的重复段
    展开实际演奏序列，仅在相邻两次发声所属页码变化时生成提示，给出边界
    秒数、前后页码、前后原谱小节与遍次、前进/回翻方向，并用前一小节后的
    静默秒数与方案的最低翻页时间比较给出是否充足；演奏结束不生成提示。
    """
    assert store is not None
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划结果不存在：{plan_id}")

    plan_request = PlanRequest(**record["request"])
    plan_result = PlanResult(**record["result"])
    page_starts = [p.start_measure for p in plan_result.pages]

    try:
        rehearsal = prepare_rehearsal(
            measure_count=len(plan_request.measure_heights),
            beats_per_measure=payload.beats_per_measure,
            tempo_bpm=payload.tempo_bpm,
            repeats=[
                (r.start_measure, r.end_measure, r.play_count)
                for r in payload.repeats
            ],
            page_starts=page_starts,
        )
        cues = rehearsal.turn_cues(
            silences=plan_request.silence_seconds,
            min_turn_time=plan_request.min_turn_time,
        )
    except RehearsalError as exc:
        # 数组长度不符 / 重复段重叠等语义错误：明确报错
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RehearsalCuesResponse(
        id=record["id"],
        request=payload,
        min_turn_time=plan_request.min_turn_time,
        total_duration=rehearsal.total_duration,
        cues=[_cue_to_out(cue) for cue in cues],
    )


@app.post(
    "/api/v1/plans/{plan_id}/rehearsal/turn-slots",
    response_model=TurnSlotsResponse,
)
def plan_rehearsal_turn_slots(
    plan_id: str, payload: TurnSlotsRequest
) -> TurnSlotsResponse:
    """为已保存方案的每个跨页边界安排具体的翻页动作空档。

    沿用排练接口的每小节拍数、速度与重复段，按实际演奏顺序展开；另传正数
    ``turn_seconds``（完整翻页动作时长）与左右手忙碌区间 ``hands_busy``
    （与原谱小节等长，每小节若干 [起拍, 止拍)，允许乱序、重叠）。仅对相邻
    演奏小节跨页的边界，在前一个演奏小节内求每只手的连续空闲窗，选能容纳
    完整翻页动作的最晚开始时刻，并列时优先左手；无可用窗的边界也保留并标记
    为不可行。
    """
    assert store is not None
    record = store.get(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划结果不存在：{plan_id}")

    plan_request = PlanRequest(**record["request"])
    plan_result = PlanResult(**record["result"])
    page_starts = [p.start_measure for p in plan_result.pages]

    try:
        rehearsal = prepare_rehearsal(
            measure_count=len(plan_request.measure_heights),
            beats_per_measure=payload.beats_per_measure,
            tempo_bpm=payload.tempo_bpm,
            repeats=[
                (r.start_measure, r.end_measure, r.play_count)
                for r in payload.repeats
            ],
            page_starts=page_starts,
        )
        slots = rehearsal.turn_slots(
            hands_busy={
                "left": payload.hands_busy.left,
                "right": payload.hands_busy.right,
            },
            turn_seconds=payload.turn_seconds,
        )
    except RehearsalError as exc:
        # 数组长度不符 / 重复段重叠 / 忙碌区间端点越界等语义错误：明确报错
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return TurnSlotsResponse(
        id=record["id"],
        request=payload,
        turn_seconds=payload.turn_seconds,
        total_duration=rehearsal.total_duration,
        slots=[_slot_to_out(slot) for slot in slots],
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
