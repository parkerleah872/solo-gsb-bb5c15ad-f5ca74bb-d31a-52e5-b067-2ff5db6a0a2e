"""排练翻页动作空档核心的单元测试。运行：python -m pytest -q"""

from __future__ import annotations

import pytest

from app.rehearsal import RehearsalError, prepare_rehearsal

# n=4，每小节 4 拍，120 BPM -> 每拍 0.5 秒、每小节 2 秒
# 页 1：小节 0..1，页 2：小节 2..3
_BEATS = [4, 4, 4, 4]


def _rehearsal(repeats=((1, 2, 2),), page_starts=(0, 2), tempo=120):
    return prepare_rehearsal(
        measure_count=4,
        beats_per_measure=_BEATS,
        tempo_bpm=tempo,
        repeats=list(repeats),
        page_starts=page_starts,
    )


def _free(left=None, right=None, n=4):
    """双手在指定小节（默认全部）均无忙碌。"""
    return {
        "left": [list(x) for x in (left if left is not None else [[]] * n)],
        "right": [list(x) for x in (right if right is not None else [[]] * n)],
    }


def test_boundaries_match_turn_cue_boundaries():
    # 演奏序列：0(页1) 1(页1) 2(页2) 1遍2(页1) 2遍2(页2) 3(页2)
    r = _rehearsal()
    slots = r.turn_slots(hands_busy=_free(), turn_seconds=1.0)
    assert [
        (s.boundary_seconds, s.from_measure, s.to_measure) for s in slots
    ] == [(4.0, 1, 2), (6.0, 2, 1), (8.0, 1, 2)]
    # 遍次随演奏序列给出
    assert [
        (s.from_playthrough, s.to_playthrough) for s in slots
    ] == [(1, 1), (1, 2), (2, 2)]
    assert [(s.from_page, s.to_page) for s in slots] == [(1, 2), (2, 1), (1, 2)]


def test_latest_start_within_previous_measure():
    # 无重复、单次跨页 1->2，边界 4.0 秒；动作 0.5 秒 = 1 拍
    r = _rehearsal(repeats=[])
    # 左手在小节 1 忙 [0,2)，空闲 [2,4)：最晚开始 = 第 3 拍 -> 2.0+1.5=3.5
    # 右手忙 [2,4)，空闲 [0,2)：最晚开始 = 第 1 拍 -> 2.0+0.5=2.5
    hands = _free(
        left=[[], [[0, 2]], [], []],
        right=[[], [[2, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.5)
    assert len(slots) == 1
    s = slots[0]
    assert s.feasible is True
    assert tuple(s.available_hands) == ("left", "right")
    assert s.hand == "left"  # 左手 3.5 晚于右手 2.5
    assert s.action_start_seconds == pytest.approx(3.5)
    assert s.action_end_seconds == pytest.approx(4.0)  # 结束不晚于边界


def test_tie_prefers_left_hand():
    r = _rehearsal(repeats=[])
    # 双手全空闲：最晚开始同为第 2 拍（动作 2 拍），并列选左手
    slots = r.turn_slots(hands_busy=_free(), turn_seconds=1.0)
    s = slots[0]
    assert tuple(s.available_hands) == ("left", "right")
    assert s.hand == "left"
    assert s.action_start_seconds == pytest.approx(3.0)
    assert s.action_end_seconds == pytest.approx(4.0)


def test_right_hand_only_when_later():
    r = _rehearsal(repeats=[])
    # 左手忙 [0,4) 全程不可用；右手空闲窗 [0,2) 可容纳 1 拍动作
    hands = _free(
        left=[[], [[0, 4]], [], []],
        right=[[], [[2, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.5)
    s = slots[0]
    assert tuple(s.available_hands) == ("right",)
    assert s.hand == "right"
    assert s.action_start_seconds == pytest.approx(2.5)
    assert s.action_end_seconds == pytest.approx(3.0)


def test_infeasible_boundary_is_kept_and_marked():
    r = _rehearsal(repeats=[])
    # 小节 1 双手全程忙碌：边界保留但不可行，动作字段为 None
    hands = _free(
        left=[[], [[0, 4]], [], []],
        right=[[], [[0, 2], [2, 4]], [], []],  # 两段合并覆盖整小节
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.5)
    assert len(slots) == 1
    s = slots[0]
    assert s.feasible is False
    assert tuple(s.available_hands) == ()
    assert s.hand is None
    assert s.action_start_seconds is None
    assert s.action_end_seconds is None
    # 边界身份信息仍完整给出
    assert s.boundary_seconds == 4.0
    assert (s.from_measure, s.to_measure) == (1, 2)
    assert (s.from_page, s.to_page) == (1, 2)


def test_unordered_and_overlapping_busy_intervals_merge():
    r = _rehearsal(repeats=[])
    # 乱序 + 重叠：[[3,4), [0,2.5), [2,3.5)] 合并为 [0,4)，无窗
    hands = _free(left=[[], [[3, 4], [0, 2.5], [2, 3.5]], [], []])
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.5)
    assert tuple(slots[0].available_hands) == ("right",)  # 右手空闲
    assert slots[0].hand == "right"

    # 合并后仅余 [2.5,3) 半拍窗：容不下 1 拍动作、容得下 0.25 秒(=0.5 拍)
    hands = _free(
        left=[[], [[3, 4], [0, 2.5]], [], []],
        right=[[], [[0, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.5)
    assert slots[0].feasible is False
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.25)
    assert slots[0].hand == "left"
    assert slots[0].action_end_seconds == pytest.approx(3.5)  # 窗右端 3 拍


def test_action_exactly_filling_window_is_feasible():
    r = _rehearsal(repeats=[])
    # 空闲窗恰为 [2,4) 两拍，动作恰 2 拍（1 秒）：恰好填满可行，结束在边界
    hands = _free(
        left=[[], [[0, 2]], [], []],
        right=[[], [[0, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=1.0)
    s = slots[0]
    assert s.feasible is True
    assert s.hand == "left"
    assert s.action_start_seconds == pytest.approx(3.0)
    assert s.action_end_seconds == pytest.approx(4.0)


def test_window_shorter_than_action_is_infeasible():
    r = _rehearsal(repeats=[])
    # 最大空闲窗 1 拍，动作需 1.5 拍（0.75 秒），容不下
    hands = _free(
        left=[[], [[0, 3]], [], []],
        right=[[], [[0, 2], [3, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.75)
    assert slots[0].feasible is False
    assert tuple(slots[0].available_hands) == ()


def test_repeated_measure_reuses_its_busy_pattern():
    # 重复小节 1..2：边界 1遍1->2遍1、2遍1->1遍2(回跳)、1遍2->2遍2
    r = _rehearsal()
    hands = _free(
        left=[[], [[0, 2]], [[0, 4]], []],
        right=[[], [[2, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=0.5)
    assert len(slots) == 3
    # 前向边界（小节 1，两次）：左手晚于右手 -> 左手 3.5/7.5
    fwd = [s for s in slots if s.to_page > s.from_page]
    assert [s.action_start_seconds for s in fwd] == [pytest.approx(3.5),
                                                      pytest.approx(7.5)]
    assert all(s.hand == "left" for s in fwd)
    # 回跳边界前一小节是小节 2：左手全程忙，只有右手可用
    back = [s for s in slots if s.to_page < s.from_page][0]
    assert back.hand == "right"
    assert tuple(back.available_hands) == ("right",)
    assert (back.action_start_seconds, back.action_end_seconds) == (
        pytest.approx(5.5), pytest.approx(6.0)
    )


def test_mixed_tempo_and_beats_uses_beat_to_seconds_conversion():
    # 60 BPM、每小节 3 拍 -> 每拍 1 秒；页 1：小节 0，页 2：小节 1..2
    # 跨页边界前一小节是小节 0，边界时刻 3.0 秒
    r = prepare_rehearsal(3, [3, 3, 3], 60, [], [0, 1])
    hands = {
        "left": [[[0, 1]], [], []],
        "right": [[[2, 3]], [], []],
    }
    # 动作 1 秒 = 1 拍：左手窗 [1,3) 最晚第 2 拍开始；右手窗 [0,2) 最晚第 1 拍
    slots = r.turn_slots(hands_busy=hands, turn_seconds=1.0)
    s = slots[0]
    assert s.boundary_seconds == pytest.approx(3.0)
    assert s.hand == "left"
    assert s.action_start_seconds == pytest.approx(2.0)
    assert s.action_end_seconds == pytest.approx(3.0)


def test_single_page_yields_no_slots():
    r = prepare_rehearsal(4, _BEATS, 120, [(0, 3, 2)], [0])
    assert r.turn_slots(hands_busy=_free(), turn_seconds=1.0) == []


def test_bound_endpoints_zero_and_full_beats_are_valid():
    r = _rehearsal(repeats=[])
    # 0 ≤ 起拍 < 止拍 ≤ 拍数：起拍 0、止拍恰为 4 都合法（整小节忙碌，无空闲窗）
    hands = _free(
        left=[[], [[0, 4]], [], []],
        right=[[], [[0, 4]], [], []],
    )
    slots = r.turn_slots(hands_busy=hands, turn_seconds=1)
    assert slots[0].feasible is False
    assert tuple(slots[0].available_hands) == ()


def test_zero_length_interval_rejected():
    r = _rehearsal(repeats=[])
    hands = _free(right=[[], [[0, 0.0]], [], []])
    with pytest.raises(RehearsalError, match="不合法"):
        r.turn_slots(hands_busy=hands, turn_seconds=1)


# ------------------------------------------------------------- 非法参数


def test_turn_seconds_must_be_positive_finite_number():
    r = _rehearsal(repeats=[])
    for bad in (0, -1.5):
        with pytest.raises(RehearsalError, match="必须为正数"):
            r.turn_slots(hands_busy=_free(), turn_seconds=bad)
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(RehearsalError, match="有限数值"):
            r.turn_slots(hands_busy=_free(), turn_seconds=bad)
    with pytest.raises(RehearsalError, match="必须是数值"):
        r.turn_slots(hands_busy=_free(), turn_seconds=True)


def test_hands_busy_requires_both_keys():
    r = _rehearsal(repeats=[])
    with pytest.raises(RehearsalError, match="缺少 left"):
        r.turn_slots(hands_busy={"right": [[]] * 4}, turn_seconds=1)
    with pytest.raises(RehearsalError, match="left、right"):
        r.turn_slots(hands_busy={}, turn_seconds=1)


def test_hands_busy_length_must_match_measures():
    r = _rehearsal(repeats=[])
    with pytest.raises(RehearsalError, match="数组长度（3）"):
        r.turn_slots(hands_busy=_free(left=[[], [], []]), turn_seconds=1)


@pytest.mark.parametrize(
    "interval",
    [
        [1, 2, 3],      # 不是恰好两个数
        [],             # 空区间
        [1],            # 缺止拍
        [-0.1, 2],      # 起拍为负
        [2, 2],         # 起拍等于止拍
        [3, 2],         # 起拍晚于止拍
        [0, 4.0001],    # 止拍超过小节拍数
        [float("nan"), 2],
        [0, float("inf")],
    ],
)
def test_invalid_busy_intervals_rejected(interval):
    r = _rehearsal(repeats=[])
    hands = _free(left=[[], [interval], [], []])
    with pytest.raises(RehearsalError):
        r.turn_slots(hands_busy=hands, turn_seconds=1)


def test_non_numeric_interval_endpoints_rejected():
    r = _rehearsal(repeats=[])
    hands = _free(left=[[], [["a", 2]], [], []])
    with pytest.raises(RehearsalError, match="数值"):
        r.turn_slots(hands_busy=hands, turn_seconds=1)
    hands = _free(left=[[], [[True, 2]], [], []])
    with pytest.raises(RehearsalError, match="数值"):
        r.turn_slots(hands_busy=hands, turn_seconds=1)
