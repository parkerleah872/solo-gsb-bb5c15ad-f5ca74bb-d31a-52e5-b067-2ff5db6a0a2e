"""排练录音时间戳定位核心。

已保存的翻页规划描述的是原谱排版；排练录音则按实际演奏顺序进行：重复段会被
完整演奏若干遍。本模块负责：

1. 校验每小节拍数、速度与重复段（不相交、不嵌套、起止含端点、演奏 2~4 遍）；
2. 按谱面顺序展开重复段，得到实际演奏序列——每次完整演奏该段后再进入后续小节；
3. 以演奏开始为 0，按 ``每小节时长 = 60 × 拍数 / 速度`` 求前缀时间；
4. 用左闭右开区间（恰在边界归下一小节）把时间戳定位到：
   原谱 0 基小节号、重复段内第几遍（非重复段为 1）、小节内拍数偏移，
   并借助已存方案的分页结果给出 1 基页码。

本模块仅依赖标准库，便于脱离 Web 层直接单测。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple


def _eps(magnitude: float) -> float:
    """随数量级缩放的浮点容差，抹平前缀和累加与 JSON 浮点解析误差。"""
    return 1e-9 * max(1.0, abs(magnitude))


class RehearsalError(ValueError):
    """排练定位输入不合法：数组长度不符、重复段重叠、时间戳越界等。"""


@dataclass(frozen=True)
class PlayedMeasure:
    """演奏序列中的一次小节发声。

    :param measure: 原谱小节号（0 基）
    :param playthrough: 所属重复段内的第几遍（1 基）；非重复段固定为 1
    """

    measure: int
    playthrough: int


@dataclass(frozen=True)
class Location:
    """单个时间戳的定位结果。"""

    measure: int  # 原谱 0 基小节号
    playthrough: int  # 重复段内第几遍；非重复段为 1
    beat_offset: float  # 该小节内的拍数偏移（自小节起点起）
    page_index: int  # 已存方案中的 1 基页码


@dataclass(frozen=True)
class TurnCue:
    """一次排练翻页提示。

    仅当演奏序列中相邻两次发声所属页码不同时生成；演奏结束不生成提示。

    :param boundary_seconds: 两次发声之间的边界时刻（秒，以演奏开始为 0），
        即前一小节结束、后一小节开始的瞬间
    :param from_page: 翻页前的页码（前一小节所在页，1 基）
    :param to_page: 翻页后的页码（后一小节所在页，1 基）
    :param from_measure: 前一小节的原谱 0 基小节号
    :param to_measure: 后一小节的原谱 0 基小节号
    :param from_playthrough: 前一小节所属重复段内第几遍（1 基）
    :param to_playthrough: 后一小节所属重复段内第几遍（1 基）
    :param direction: ``"forward"`` 前进翻页（页码增大）或
        ``"backward"`` 回翻（页码减小，通常由重复段回跳引起）
    :param silence_seconds: 前一小节之后的静默秒数（取自已存规划）
    :param sufficient: 该静默是否不低于方案的最低翻页时间
    """

    boundary_seconds: float
    from_page: int
    to_page: int
    from_measure: int
    to_measure: int
    from_playthrough: int
    to_playthrough: int
    direction: str
    silence_seconds: float
    sufficient: bool


@dataclass(frozen=True)
class TurnSlot:
    """一次跨页边界上的翻页空档安排。

    仅当演奏序列中相邻两次发声所属页码不同时生成；翻页动作必须完整落在
    **前一个演奏小节内**某只手的一段连续空闲窗中。每只手先各自选出能容纳
    完整翻页动作（``turn_seconds`` 秒）的最晚开始时刻；再在所有可行的手之间
    取最晚开始时刻，并列时优先左手。

    :param boundary_seconds: 两次发声之间的边界时刻（秒，以演奏开始为 0），
        即前一小节结束、后一小节开始的瞬间
    :param from_page: 翻页前的页码（前一小节所在页，1 基）
    :param to_page: 翻页后的页码（后一小节所在页，1 基）
    :param from_measure: 前一小节的原谱 0 基小节号
    :param to_measure: 后一小节的原谱 0 基小节号
    :param from_playthrough: 前一小节所属重复段内第几遍（1 基）
    :param to_playthrough: 后一小节所属重复段内第几遍（1 基）
    :param available_hands: 存在能容纳完整翻页动作的空闲窗的手，
        固定按左手优先、右手其后的顺序排列（如 ``["left", "right"]``）
    :param hand: 实际选中的手（``"left"`` 左手或 ``"right"`` 右手）；
        无可用窗时为 ``None``
    :param action_start_seconds: 翻页动作开始时刻（秒，以演奏开始为 0）；
        不可行为 ``None``
    :param action_end_seconds: 翻页动作结束时刻（秒，不晚于边界时刻）；
        不可行为 ``None``
    """

    boundary_seconds: float
    from_page: int
    to_page: int
    from_measure: int
    to_measure: int
    from_playthrough: int
    to_playthrough: int
    available_hands: Tuple[str, ...]
    hand: Optional[str]
    action_start_seconds: Optional[float]
    action_end_seconds: Optional[float]

    @property
    def feasible(self) -> bool:
        """是否至少有一只手存在能容纳完整翻页动作的空闲窗。"""
        return self.hand is not None


# (起始拍, 结束拍) 的忙碌区间，左闭右开
BusyInterval = Tuple[float, float]
# 一只手（"left" / "right"）在各原谱小节内的忙碌区间序列
HandsBusy = Mapping[str, Sequence[Sequence[float]]]


# (起始小节, 结束小节, 演奏次数)，小节均为 0 基、两端皆含
Repeat = Tuple[int, int, int]


def _normalize_repeats(
    n: int, repeats: Sequence[Repeat]
) -> List[Tuple[int, int, int]]:
    """校验重复段并按起始小节排序。"""
    checked: List[Tuple[int, int, int]] = []
    for idx, (start, end, times) in enumerate(repeats):
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(times, int)
            or isinstance(start, bool)
            or isinstance(end, bool)
            or isinstance(times, bool)
        ):
            raise RehearsalError(f"第 {idx + 1} 个重复段的字段必须是整数")
        if start < 0 or end < 0 or start > n - 1 or end > n - 1:
            raise RehearsalError(
                f"第 {idx + 1} 个重复段不合法：起止小节必须落在 0..{n - 1}"
                f" 内（实际 {start}..{end}）"
            )
        if start > end:
            raise RehearsalError(
                f"第 {idx + 1} 个重复段起始小节（{start}）不能晚于"
                f"结束小节（{end}）"
            )
        if not 2 <= times <= 4:
            raise RehearsalError(
                f"第 {idx + 1} 个重复段（小节 {start}..{end}）的演奏次数"
                f"必须在 2~4 之间（实际 {times}）"
            )
        checked.append((start, end, times))

    checked.sort(key=lambda r: r[0])
    for prev, nxt in zip(checked, checked[1:]):
        # 端点均含：下一段起点落入上一段区间即为相交（含嵌套/首尾相接重叠）
        if nxt[0] <= prev[1]:
            raise RehearsalError(
                f"重复段 [{prev[0]}..{prev[1]}] 与 [{nxt[0]}..{nxt[1]}] "
                "重叠或嵌套：重复段必须互不相交且不嵌套（首尾相邻允许）"
            )
    return checked


def expand_repeats(
    n: int, repeats: Sequence[Repeat]
) -> List[PlayedMeasure]:
    """按谱面顺序展开重复段，返回实际演奏的小节序列。

    遇到重复段时完整演奏该段 ``times`` 遍（每遍都覆盖起止间的全部小节，
    遍号 1..times），随后再进入该段之后的小节；非重复段小节遍号为 1。
    """
    ordered = _normalize_repeats(n, repeats)
    by_start = {start: (end, times) for start, end, times in ordered}

    events: List[PlayedMeasure] = []
    i = 0
    while i < n:
        if i in by_start:
            end, times = by_start[i]
            for playthrough in range(1, times + 1):
                for measure in range(i, end + 1):
                    events.append(PlayedMeasure(measure, playthrough))
            i = end + 1
        else:
            events.append(PlayedMeasure(i, 1))
            i += 1
    return events


@dataclass(frozen=True)
class Rehearsal:
    """一次排练定位的已校验上下文：演奏序列、前缀时间与分页映射。"""

    measure_count: int
    tempo_bpm: float
    beats_per_measure: Tuple[float, ...]  # 各原谱小节拍数（与小节号对应）
    events: List[PlayedMeasure]
    start_times: List[float]  # start_times[k]：第 k 个演奏事件的开始时间
    total_duration: float
    page_starts: Tuple[int, ...]  # 每页起始原谱小节（0 基，升序，首个为 0）

    def page_of(self, measure: int) -> int:
        """原谱小节号 -> 已存方案的 1 基页码。"""
        return bisect_right(self.page_starts, measure)

    def locate(self, timestamp: float) -> Location:
        """定位单个时间戳；越界/为负时抛 RehearsalError。"""
        total = self.total_duration
        if timestamp < -_eps(total):
            raise RehearsalError(
                f"时间戳 {timestamp:g} 秒不能为负数（时间戳以演奏开始为 0）"
            )
        # 左闭右开：总时长处是最后一小节的右端点，不属于任何小节
        if timestamp >= total - _eps(total):
            raise RehearsalError(
                f"时间戳 {timestamp:g} 秒超出演奏总时长 {total:g} 秒"
                "（定位采用左闭右开区间，端点总时长不可定位）"
            )

        t = max(0.0, timestamp)  # 0 附近的负向舍入误差视为起点
        # 恰在边界归下一小节：先按严格小于取段，再用容差吸收浮点累加误差
        idx = bisect_right(self.start_times, t) - 1
        while (
            idx + 1 < len(self.events)
            and self.start_times[idx + 1] <= t + _eps(self.start_times[idx + 1])
        ):
            idx += 1

        event = self.events[idx]
        seconds_per_beat = 60.0 / self.tempo_bpm
        beat_offset = (t - self.start_times[idx]) / seconds_per_beat
        # 消除浮点噪声与可能的 -0.0，拍数偏移保留 9 位小数
        beat_offset = round(beat_offset, 9)
        if beat_offset == 0:
            beat_offset = 0.0
        return Location(
            measure=event.measure,
            playthrough=event.playthrough,
            beat_offset=beat_offset,
            page_index=self.page_of(event.measure),
        )

    def locate_all(self, timestamps: Sequence[float]) -> List[Location]:
        """批量定位；错误信息附带序号，便于定位具体时间戳。"""
        results: List[Location] = []
        for idx, ts in enumerate(timestamps):
            try:
                results.append(self.locate(ts))
            except RehearsalError as exc:
                raise RehearsalError(f"第 {idx + 1} 个时间戳：{exc}") from exc
        return results

    def turn_cues(
        self,
        silences: Sequence[float],
        min_turn_time: float,
    ) -> List[TurnCue]:
        """按实际演奏序列生成排练翻页提示。

        逐对考察相邻的两次发声：仅当二者所属页码不同时生成一条提示，
        因此重复段回跳是否翻页也按回跳两端实际相邻小节判定，与原谱分页
        位置无关。演奏结束（最后一次发声之后）不生成提示。

        :param silences: 已存规划中每个原谱小节之后的静默秒数，长度必须
            等于规划小节数
        :param min_turn_time: 方案的最低翻页时间（秒），用于判断每次提示
            前的静默是否充足
        """
        if len(silences) != self.measure_count:
            raise RehearsalError(
                f"静默秒数数组长度（{len(silences)}）"
                f"与规划小节数（{self.measure_count}）不符"
            )

        cues: List[TurnCue] = []
        for k in range(len(self.events) - 1):
            prev, nxt = self.events[k], self.events[k + 1]
            from_page = self.page_of(prev.measure)
            to_page = self.page_of(nxt.measure)
            if from_page == to_page:
                continue
            silence = float(silences[prev.measure])
            cues.append(
                TurnCue(
                    boundary_seconds=self.start_times[k + 1],
                    from_page=from_page,
                    to_page=to_page,
                    from_measure=prev.measure,
                    to_measure=nxt.measure,
                    from_playthrough=prev.playthrough,
                    to_playthrough=nxt.playthrough,
                    direction="forward" if to_page > from_page else "backward",
                    silence_seconds=silence,
                    sufficient=silence + _eps(min_turn_time) >= min_turn_time,
                )
            )
        return cues

    def turn_slots(
        self,
        hands_busy: HandsBusy,
        turn_seconds: float,
    ) -> List[TurnSlot]:
        """按实际演奏序列为每个跨页边界安排翻页空档。

        逐对考察相邻的两次发声：仅当二者所属页码不同时生成一条安排，边界
        时刻取后一小节的开始时刻（与 :meth:`turn_cues` 一致）。忙碌区间按
        **原谱小节号**给出，重复段的每一遍都复用同一小节的忙碌模式；翻页
        动作只允许安排在前一个演奏小节内。

        :param hands_busy: 含 ``"left"`` / ``"right"`` 两个键，各自是长度
            等于规划小节数的数组；第 i 个元素是若干 ``[起拍, 止拍)`` 忙碌
            区间，允许乱序与重叠，端点有限且满足
            ``0 ≤ 起拍 < 止拍 ≤ 该小节拍数``
        :param turn_seconds: 完整翻页动作所需秒数，必须为正
        :raises RehearsalError: ``turn_seconds`` 非法，或 ``hands_busy``
            结构/端点不合法
        """
        _validate_turn_inputs(
            measure_count=self.measure_count,
            beats_per_measure=self.beats_per_measure,
            hands_busy=hands_busy,
            turn_seconds=turn_seconds,
        )

        seconds_per_beat = 60.0 / self.tempo_bpm
        # 每个原谱小节各只算一次：空闲窗只与原谱小节及其拍数、忙碌区间有关，
        # 与该小节在重复段中出现几遍无关；各遍边界的时刻在使用时换算。
        windows: dict[tuple[int, str], List[Tuple[float, float]]] = {}
        for hand in ("left", "right"):
            for measure in range(self.measure_count):
                windows[(measure, hand)] = _free_windows(
                    busy=hands_busy[hand][measure],
                    beats=self.beats_per_measure[measure],
                )

        slots: List[TurnSlot] = []
        for k in range(len(self.events) - 1):
            prev, nxt = self.events[k], self.events[k + 1]
            from_page = self.page_of(prev.measure)
            to_page = self.page_of(nxt.measure)
            if from_page == to_page:
                continue

            measure_start = self.start_times[k]
            boundary = self.start_times[k + 1]

            # 每只手各自取能容纳完整动作的最晚开始时刻（空闲窗右端 - 动作时长），
            # 再在所有可行手之间选最晚者；左手先遍历且仅在严格更晚时替换，
            # 故并列时优先左手。开始拍统一保留 9 位小数以吸收浮点噪声。
            available: List[str] = []
            best_start: Optional[float] = None
            best_hand: Optional[str] = None
            turn_beats = turn_seconds / seconds_per_beat
            for hand in ("left", "right"):
                latest = _latest_free_start(
                    windows[(prev.measure, hand)],
                    turn_beats=turn_beats,
                )
                if latest is None:
                    continue
                available.append(hand)
                latest = round(latest, 9)
                if best_start is None or latest > best_start:
                    best_start, best_hand = latest, hand

            action_start: Optional[float] = None
            action_end: Optional[float] = None
            if best_hand is not None and best_start is not None:
                action_start = round(
                    measure_start + best_start * seconds_per_beat, 9
                )
                action_end = round(action_start + turn_seconds, 9)
                if action_start == 0:
                    action_start = 0.0

            slots.append(
                TurnSlot(
                    boundary_seconds=boundary,
                    from_page=from_page,
                    to_page=to_page,
                    from_measure=prev.measure,
                    to_measure=nxt.measure,
                    from_playthrough=prev.playthrough,
                    to_playthrough=nxt.playthrough,
                    available_hands=tuple(available),
                    hand=best_hand,
                    action_start_seconds=action_start,
                    action_end_seconds=action_end,
                )
            )
        return slots


def _validate_turn_inputs(
    measure_count: int,
    beats_per_measure: Sequence[float],
    hands_busy: HandsBusy,
    turn_seconds: float,
) -> None:
    """校验翻页空档参数：动作时长为正有限，忙碌数组结构与端点合法。"""
    if not isinstance(turn_seconds, (int, float)) or isinstance(turn_seconds, bool):
        raise RehearsalError("turn_seconds（翻页动作时长）必须是数值")
    turn = float(turn_seconds)
    if turn != turn or turn in (float("inf"), float("-inf")):
        raise RehearsalError("turn_seconds（翻页动作时长）必须是有限数值")
    if turn <= 0:
        raise RehearsalError(
            f"turn_seconds（翻页动作时长）必须为正数（实际 {turn:g}）"
        )

    if not isinstance(hands_busy, Mapping):
        raise RehearsalError(
            "hands_busy 必须是含 left、right 两只手忙碌区间的对象"
        )
    missing = [h for h in ("left", "right") if h not in hands_busy]
    if missing:
        raise RehearsalError(
            f"hands_busy 缺少 {'、'.join(missing)} 的忙碌区间；"
            "必须同时给出 left（左手）与 right（右手）"
        )

    for hand in ("left", "right"):
        per_measure = hands_busy[hand]
        if not isinstance(per_measure, (list, tuple)):
            raise RehearsalError(
                f"hands_busy.{hand} 必须是与原谱小节等长的区间数组"
            )
        if len(per_measure) != measure_count:
            raise RehearsalError(
                f"hands_busy.{hand} 数组长度（{len(per_measure)}）"
                f"与规划小节数（{measure_count}）不符"
            )
        for measure, intervals in enumerate(per_measure):
            if not isinstance(intervals, (list, tuple)):
                raise RehearsalError(
                    f"hands_busy.{hand}[{measure}] 必须是若干 [起拍, 止拍) 区间"
                )
            for idx, interval in enumerate(intervals):
                if (
                    not isinstance(interval, (list, tuple))
                    or len(interval) != 2
                ):
                    raise RehearsalError(
                        f"hands_busy.{hand}[{measure}] 第 {idx + 1} 个区间必须是"
                        "恰好两个数 [起拍, 止拍)"
                    )
                start, end = interval
                if (
                    not isinstance(start, (int, float))
                    or isinstance(start, bool)
                    or not isinstance(end, (int, float))
                    or isinstance(end, bool)
                ):
                    raise RehearsalError(
                        f"hands_busy.{hand}[{measure}] 第 {idx + 1} 个区间的"
                        "起拍、止拍必须是数值"
                    )
                start_f, end_f = float(start), float(end)
                if start_f != start_f or end_f != end_f or start_f in (
                    float("inf"),
                    float("-inf"),
                ) or end_f in (float("inf"), float("-inf")):
                    raise RehearsalError(
                        f"hands_busy.{hand}[{measure}] 第 {idx + 1} 个区间的"
                        "起拍、止拍必须是有限数值"
                    )
                beats = float(beats_per_measure[measure])
                if not (0 <= start_f < end_f <= beats):
                    raise RehearsalError(
                        f"hands_busy.{hand}[{measure}] 第 {idx + 1} 个区间"
                        f" [{start_f:g}, {end_f:g}) 不合法：端点须满足"
                        f" 0 ≤ 起拍 < 止拍 ≤ 该小节拍数（{beats:g}）"
                    )


def _free_windows(
    busy: Sequence[Sequence[float]], beats: float
) -> List[Tuple[float, float]]:
    """把允许乱序、重叠的忙碌区间合并，返回小节内的连续空闲窗（拍）。

    返回若干左闭右开区间 ``[a, b)``（``0 ≤ a < b ≤ beats``），按起点升序；
    忙碌覆盖整小节时返回空列表。
    """
    intervals = sorted((float(a), float(b)) for a, b in busy)
    merged: List[List[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])

    windows: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            windows.append((cursor, start))
        if end > cursor:
            cursor = end
    if cursor < beats:
        windows.append((cursor, beats))
    return windows


def _latest_free_start(
    windows: Sequence[Tuple[float, float]], turn_beats: float
) -> Optional[float]:
    """在空闲窗（拍）中选能容纳完整翻页动作的最晚开始时刻（拍偏移）。"""
    best: Optional[float] = None
    for start, end in windows:
        candidate = end - turn_beats
        if candidate + _eps(end) >= start:
            if candidate < start:
                candidate = start
            if best is None or candidate > best:
                best = candidate
    return best



def prepare_rehearsal(
    measure_count: int,
    beats_per_measure: Sequence[float],
    tempo_bpm: float,
    repeats: Sequence[Repeat],
    page_starts: Sequence[int],
) -> Rehearsal:
    """校验输入并构建排练定位上下文。

    :param measure_count: 已存规划的原谱小节数
    :param beats_per_measure: 每小节拍数，长度必须等于 ``measure_count``、元素为正
    :param tempo_bpm: 每分钟拍数，必须为正
    :param repeats: 重复段序列，彼此不相交、不嵌套，起止含、演奏 2~4 遍
    :param page_starts: 已存方案每页起始小节（0 基升序，首个为 0）
    :raises RehearsalError: 任一语义约束不满足
    """
    if measure_count <= 0:
        raise RehearsalError("规划小节数必须为正数")
    if len(beats_per_measure) != measure_count:
        raise RehearsalError(
            f"beats_per_measure 数组长度（{len(beats_per_measure)}）"
            f"与规划小节数（{measure_count}）不符"
        )
    for i, beats in enumerate(beats_per_measure):
        if not isinstance(beats, (int, float)) or isinstance(beats, bool):
            raise RehearsalError(f"第 {i + 1} 小节拍数必须是数值")
        beats_f = float(beats)
        if beats_f != beats_f or beats_f in (float("inf"), float("-inf")):
            raise RehearsalError(f"第 {i + 1} 小节拍数必须是有限数值")
        if beats_f <= 0:
            raise RehearsalError(
                f"第 {i + 1} 小节拍数必须为正数（实际 {beats_f:g}）"
            )
    if not isinstance(tempo_bpm, (int, float)) or isinstance(tempo_bpm, bool):
        raise RehearsalError("每分钟拍数必须是数值")
    tempo = float(tempo_bpm)
    if tempo != tempo or tempo in (float("inf"), float("-inf")):
        raise RehearsalError("每分钟拍数必须是有限数值")
    if tempo <= 0:
        raise RehearsalError(f"每分钟拍数必须为正数（实际 {tempo:g}）")

    page_tuple = tuple(sorted(page_starts))
    if not page_tuple or page_tuple[0] != 0:
        raise RehearsalError("已存方案分页信息不合法：首页必须从第 0 小节开始")

    # _normalize_repeats 在 expand_repeats 内完成；此处提前调用以先报错
    events = expand_repeats(measure_count, repeats)

    seconds_per_beat = 60.0 / tempo
    start_times: List[float] = []
    elapsed = 0.0
    for event in events:
        start_times.append(elapsed)
        elapsed += float(beats_per_measure[event.measure]) * seconds_per_beat

    return Rehearsal(
        measure_count=measure_count,
        tempo_bpm=tempo,
        beats_per_measure=tuple(float(b) for b in beats_per_measure),
        events=events,
        start_times=start_times,
        total_duration=elapsed,
        page_starts=page_tuple,
    )
