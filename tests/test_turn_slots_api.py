"""排练翻页动作空档接口的端到端测试（FastAPI TestClient + 临时 SQLite）。"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """以临时 DATA_DIR 重新加载 app.main，得到隔离的服务实例。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c


def _create_plan(client) -> str:
    # 分页结果：页 1 = 小节 0..1（断页在小节 2，静默 5），页 2 = 小节 2..3
    resp = client.post(
        "/api/v1/plans",
        json={
            "measure_heights": [3, 3, 4, 2],
            "silence_seconds": [2, 5, 1, 0],
            "page_height": 6,
            "min_turn_time": 2,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _base_payload(**overrides):
    # 每小节 4 拍、120 BPM -> 2 秒/小节；重复小节 1..2 两遍
    payload = {
        "beats_per_measure": [4, 4, 4, 4],
        "tempo_bpm": 120,
        "repeats": [{"start_measure": 1, "end_measure": 2, "play_count": 2}],
        "turn_seconds": 0.5,
        "hands_busy": {
            "left": [[], [[0, 2]], [[0, 4]], []],
            "right": [[], [[2, 4]], [], []],
        },
    }
    payload.update(overrides)
    return payload


def _slots(client, plan_id, **overrides):
    return client.post(
        f"/api/v1/plans/{plan_id}/rehearsal/turn-slots",
        json=_base_payload(**overrides),
    )


def test_slots_happy_path(client):
    plan_id = _create_plan(client)
    resp = _slots(client, plan_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == plan_id
    assert body["turn_seconds"] == 0.5
    assert body["total_duration"] == pytest.approx(12.0)
    assert body["request"]["turn_seconds"] == 0.5
    assert body["request"]["hands_busy"]["left"][1] == [[0, 2]]

    # 演奏序列：0, 1, 2, 1(遍2), 2(遍2), 3 -> 三个跨页边界
    assert body["slots"] == [
        {
            "boundary_seconds": 4.0,
            "from_page": 1,
            "to_page": 2,
            "from_measure": 1,
            "to_measure": 2,
            "from_playthrough": 1,
            "to_playthrough": 1,
            "available_hands": ["left", "right"],
            "hand": "left",
            "action_start_seconds": 3.5,
            "action_end_seconds": 4.0,
            "feasible": True,
        },
        {
            "boundary_seconds": 6.0,
            "from_page": 2,
            "to_page": 1,
            "from_measure": 2,
            "to_measure": 1,
            "from_playthrough": 1,
            "to_playthrough": 2,
            "available_hands": ["right"],
            "hand": "right",
            "action_start_seconds": 5.5,
            "action_end_seconds": 6.0,
            "feasible": True,
        },
        {
            "boundary_seconds": 8.0,
            "from_page": 1,
            "to_page": 2,
            "from_measure": 1,
            "to_measure": 2,
            "from_playthrough": 2,
            "to_playthrough": 2,
            "available_hands": ["left", "right"],
            "hand": "left",
            "action_start_seconds": 7.5,
            "action_end_seconds": 8.0,
            "feasible": True,
        },
    ]


def test_slots_no_repeats_and_no_slot_at_end(client):
    plan_id = _create_plan(client)
    resp = _slots(client, plan_id, repeats=[])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_duration"] == pytest.approx(8.0)
    # 仅 1->2 一次跨页；末尾小节 3 之后不生成
    assert len(body["slots"]) == 1
    slot = body["slots"][0]
    assert slot["from_measure"] == 1 and slot["to_measure"] == 2
    assert slot["boundary_seconds"] == pytest.approx(4.0)


def test_slots_infeasible_boundary_is_kept(client):
    plan_id = _create_plan(client)
    # 两个跨页边界的前一小节（1、2）双手均全程忙碌
    resp = _slots(
        client,
        plan_id,
        turn_seconds=0.5,
        hands_busy={
            "left": [[], [[0, 4]], [[0, 4]], []],
            "right": [[], [[0, 2], [2, 4]], [[0, 4]], []],
        },
    )
    assert resp.status_code == 200, resp.text
    slots = resp.json()["slots"]
    assert len(slots) == 3
    for slot in slots:
        assert slot["feasible"] is False
        assert slot["available_hands"] == []
        assert slot["hand"] is None
        assert slot["action_start_seconds"] is None
        assert slot["action_end_seconds"] is None


def test_slots_unordered_overlapping_and_empty_measure_arrays(client):
    plan_id = _create_plan(client)
    # 乱序 + 重叠区间 + 多个空小节数组（回归 pydantic 嵌套空数组推断）
    resp = _slots(
        client,
        plan_id,
        repeats=[],
        hands_busy={
            "left": [[], [[3, 4], [0, 2.5], [2, 3.5]], [], []],
            "right": [[], [], [], []],
        },
    )
    assert resp.status_code == 200, resp.text
    slot = resp.json()["slots"][0]
    # 左手小节 1 被忙碌完全覆盖，只有右手可用
    assert slot["available_hands"] == ["right"]
    assert slot["hand"] == "right"


def test_slots_tie_prefers_left_hand(client):
    plan_id = _create_plan(client)
    resp = _slots(
        client,
        plan_id,
        repeats=[],
        turn_seconds=1.0,
        hands_busy={"left": [[], [], [], []], "right": [[], [], [], []]},
    )
    assert resp.status_code == 200, resp.text
    slot = resp.json()["slots"][0]
    assert slot["available_hands"] == ["left", "right"]
    assert slot["hand"] == "left"
    assert slot["action_start_seconds"] == pytest.approx(3.0)
    assert slot["action_end_seconds"] == pytest.approx(4.0)


def test_slots_single_page_plan_returns_empty(client):
    resp = client.post(
        "/api/v1/plans",
        json={
            "measure_heights": [3, 3],
            "silence_seconds": [1, 0],
            "page_height": 10,
            "min_turn_time": 1,
        },
    )
    plan_id = resp.json()["id"]
    resp = _slots(
        client,
        plan_id,
        beats_per_measure=[4, 4],
        repeats=[],
        hands_busy={"left": [[], []], "right": [[], []]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["slots"] == []


def test_slots_plan_not_found(client):
    resp = _slots(client, "does-not-exist")
    assert resp.status_code == 404
    assert "规划结果不存在" in resp.json()["detail"]


# --------------------------------------------------------------- 422 用例


def test_slots_beats_length_mismatch(client):
    plan_id = _create_plan(client)
    resp = _slots(client, plan_id, beats_per_measure=[4, 4, 4])
    assert resp.status_code == 422
    assert "数组长度" in resp.json()["detail"]


def test_slots_hands_busy_length_mismatch(client):
    plan_id = _create_plan(client)
    resp = _slots(
        client,
        plan_id,
        hands_busy={
            "left": [[], [], []],
            "right": [[], [], [], []],
        },
    )
    assert resp.status_code == 422
    assert "hands_busy.left 数组长度（3）" in resp.json()["detail"]


def test_slots_overlapping_repeats_rejected(client):
    plan_id = _create_plan(client)
    resp = _slots(
        client,
        plan_id,
        repeats=[
            {"start_measure": 0, "end_measure": 2, "play_count": 2},
            {"start_measure": 1, "end_measure": 3, "play_count": 2},
        ],
    )
    assert resp.status_code == 422
    assert "重叠或嵌套" in resp.json()["detail"]


@pytest.mark.parametrize(
    "bad",
    [
        0,
        -1,
    ],
)
def test_slots_turn_seconds_must_be_positive(client, bad):
    plan_id = _create_plan(client)
    resp = _slots(client, plan_id, turn_seconds=bad)
    assert resp.status_code == 422


def test_slots_turn_seconds_nan_rejected(client):
    import json

    plan_id = _create_plan(client)
    # httpx 不允许发送 NaN，直接构造原始 JSON 报文（服务端解析后由模型拒绝）
    resp = client.post(
        f"/api/v1/plans/{plan_id}/rehearsal/turn-slots",
        content=json.dumps(_base_payload(turn_seconds=float("nan"))),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_slots_bad_interval_endpoints(client):
    plan_id = _create_plan(client)
    free = {"left": [[], [], [], []], "right": [[], [], [], []]}
    bad_payloads = [
        {"left": [[], [[2, 1]], [], []]},     # 起拍 > 止拍
        {"left": [[], [[0, 5]], [], []]},     # 止拍 > 小节拍数
        {"left": [[], [[-1, 2]], [], []]},    # 起拍为负
        {"left": [[], [[2, 2]], [], []]},     # 起拍 = 止拍
        {"left": [[], [[1, 2, 3]], [], []]},  # 区间不是两个数
    ]
    for busy in bad_payloads:
        hands = dict(free)
        hands.update(busy)
        resp = _slots(client, plan_id, hands_busy=hands)
        assert resp.status_code == 422, busy
    # 缺 left
    resp = _slots(
        client, plan_id, hands_busy={"right": [[], [[0, 2]], [], []]}
    )
    assert resp.status_code == 422


def test_slots_rejects_missing_hand_and_extra_fields(client):
    plan_id = _create_plan(client)
    # 缺 right
    resp = _slots(client, plan_id, hands_busy={"left": [[], [], [], []]})
    assert resp.status_code == 422
    # hands_busy 内多余字段
    resp = _slots(
        client,
        plan_id,
        hands_busy={
            "left": [[], [], [], []],
            "right": [[], [], [], []],
            "pedal": [],
        },
    )
    assert resp.status_code == 422
    # 顶层多余字段
    resp = client.post(
        f"/api/v1/plans/{plan_id}/rehearsal/turn-slots",
        json=_base_payload(unexpected=1),
    )
    assert resp.status_code == 422


def test_slots_rehearsal_params_still_validated(client):
    plan_id = _create_plan(client)
    for bad in (
        {"tempo_bpm": 0},
        {"beats_per_measure": [4, 4, 4, 0]},
    ):
        resp = _slots(client, plan_id, **bad)
        assert resp.status_code == 422, bad


def test_slots_do_not_modify_saved_plan(client):
    plan_id = _create_plan(client)
    before = client.get(f"/api/v1/plans/{plan_id}").json()
    assert _slots(client, plan_id).status_code == 200
    after = client.get(f"/api/v1/plans/{plan_id}").json()
    assert before == after
