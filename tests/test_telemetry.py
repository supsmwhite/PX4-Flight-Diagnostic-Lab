"""Synthetic offline telemetry tests; no real-device validation implied."""
import copy
import json
import math
from pathlib import Path

import pytest

from px4_flight_diag.analysis.telemetry import finite, load_session, quaternion_euler, series


def record(msg_type="ATTITUDE", ns=0, **fields):
    return {"schema_version": 1, "kind": "message", "packet_index": 0,
            "recv_utc": "2026-01-01T00:00:00+00:00", "recv_monotonic_ns": ns,
            "peer": ["127.0.0.1", 14550], "sysid": 1, "compid": 1, "seq": 0,
            "msg_type": msg_type, "fields": fields, "frame_hex": "",
            "time": {"sources": [], "clock_alignment": None}}


def session(tmp_path, rows):
    (tmp_path / "processed").mkdir(exist_ok=True)
    source = tmp_path / "processed/messages.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return source


@pytest.mark.parametrize("value", [True, False, "2", "nan", "inf", "-inf", None,
                                       float("nan"), float("inf"), -float("inf"), 10**400])
def test_finite_rejects(value):
    assert finite(value) is None


def test_finite_numbers():
    assert finite(0) == 0.0
    assert finite(-2.5) == -2.5


@pytest.mark.parametrize("q", [[], [1, 0, 0], [0, 0, 0, 0], [2, 0, 0, 0],
                                    [0.89, 0, 0, 0], [1.11, 0, 0, 0],
                                    ["nan", 0, 0, 0], [True, 0, 0, 0]])
def test_bad_quaternions(q):
    assert quaternion_euler(q) is None


def test_quaternion_angles_and_normalization():
    assert quaternion_euler([0.9, 0, 0, 0]) == (0, 0)
    assert quaternion_euler([1.1, 0, 0, 0]) == (0, 0)
    assert quaternion_euler([math.sqrt(0.5), math.sqrt(0.5), 0, 0]) == pytest.approx((90, 0))
    # At pitch=90 roll is singular, so only pitch has a unique expectation.
    assert quaternion_euler([math.sqrt(0.5), 0, math.sqrt(0.5), 0])[1] == pytest.approx(90)
    assert quaternion_euler([math.cos(math.pi / 12), 0, math.sin(math.pi / 12), 0]) == pytest.approx((0, 30))


def test_loader_preserves_fields_and_errors_without_raw(tmp_path, monkeypatch):
    good = record(roll="nan", time_boot_ms=123)
    error = dict(record(), kind="parse_error", sysid=None, compid=None, seq=None,
                 msg_type=None, reason="bad bytes", error_type="bad_data")
    session(tmp_path, [good, error])
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert "raw" not in path.parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    loaded = load_session(tmp_path)
    assert loaded.path == tmp_path
    assert loaded.records == [good]
    assert loaded.errors == [error]
    assert loaded.identities == [("127.0.0.1", 14550, 1, 1)]
    assert loaded.metadata == loaded.summary == {}
    assert len(loaded.warnings) == 2


def test_empty_session_and_optional_documents(tmp_path):
    session(tmp_path, [])
    (tmp_path / "metadata.json").write_text('{"schema_version":1}')
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/summary.json").write_text('{"messages":0}')
    loaded = load_session(tmp_path)
    assert not loaded.records and not loaded.identities and not loaded.warnings
    assert loaded.summary["messages"] == 0


@pytest.mark.parametrize("bad", [[], {}, {**record(), "schema_version": True},
                                     {**record(), "schema_version": 2},
                                     {**record(), "fields": []},
                                     {**record(), "peer": ["host", True]},
                                     {**record(), "recv_utc": "2026-01-01"},
                                     {**record(), "sysid": None},
                                     {**record(), "time": {}},
                                     {**record(), "frame_hex": "0x"}])
def test_processed_structure_has_line_number(tmp_path, bad):
    session(tmp_path, [record(), bad])
    with pytest.raises(ValueError, match=r"messages.jsonl:2: invalid processed record"):
        load_session(tmp_path)


@pytest.mark.parametrize("bad", ['{"schema_version":', "\n", '{"x":NaN}\n'])
def test_bad_json_has_line_number(tmp_path, bad):
    source = session(tmp_path, [record()])
    with source.open("a") as handle:
        handle.write(bad)
    with pytest.raises(ValueError, match=r"messages.jsonl:2:"):
        load_session(tmp_path)


def test_missing_processed_and_bad_optional(tmp_path):
    with pytest.raises(ValueError, match="missing processed"):
        load_session(tmp_path)
    session(tmp_path, [])
    (tmp_path / "metadata.json").write_text("[]")
    with pytest.raises(ValueError, match="metadata.json"):
        load_session(tmp_path)


def test_series_gap_nan_scale_and_original_unchanged():
    rows = [record(ns=1_000_000_000, roll=1), record(ns=1_100_000_000, roll="nan"),
            record(ns=2_000_000_000, roll=0)]
    before = copy.deepcopy(rows)
    result = series(rows, "ATTITUDE", "roll", scale=180 / math.pi, origin=1)
    assert result["x"] == pytest.approx([0, 0.1, None, 1], nan_ok=True)
    assert result["y"] == [180 / math.pi, None, None, 0]
    assert result["valid_count"] == 2 and result["invalid_count"] == 1
    assert result["gap_count"] == 1 and result["connectgaps"] is False
    assert rows == before


@pytest.mark.parametrize("mask", [None, 128, True, "0", -1])
def test_target_invalid_mask(mask):
    assert series([record("ATTITUDE_TARGET", q=[1, 0, 0, 0], type_mask=mask)],
                  "ATTITUDE_TARGET", "roll_deg")["y"] == [None]


def test_target_and_missing_mask():
    rows = [record("ATTITUDE_TARGET", q=[1, 0, 0, 0], type_mask=7),
            record("ATTITUDE_TARGET", q=[1, 0, 0, 0])]
    assert series(rows, "ATTITUDE_TARGET", "pitch_deg")["y"] == [0, None]


def test_identity_selection_and_sorting(tmp_path):
    first = record(roll=1)
    second = dict(record(roll=2), peer=["other", 1], sysid=2)
    session(tmp_path, [second, first])
    assert load_session(tmp_path).identities == [("127.0.0.1", 14550, 1, 1), ("other", 1, 2, 1)]
    with pytest.raises(ValueError, match="multiple identities"):
        series([first, second], "ATTITUDE", "roll")
    assert series([first, second], "ATTITUDE", "roll", identity=("other", 1, 2, 1))["y"] == [2]


def test_distance_units_range_sentinel_and_sensor_selection():
    rows = [record("DISTANCE_SENSOR", id=1, orientation=25, min_distance=10,
                   max_distance=500, current_distance=v) for v in (10, 123, 501, 65535, "nan")]
    assert series(rows, "DISTANCE_SENSOR", "current_distance")["y"] == [0.1, 1.23, None, None, None]
    assert series(rows, "DISTANCE_SENSOR", "min_distance")["y"] == [0.1] * 5
    assert series(rows, "DISTANCE_SENSOR", "max_distance")["y"] == [5] * 5
    other = record("DISTANCE_SENSOR", id=1, orientation=0, min_distance=1, max_distance=100, current_distance=50)
    with pytest.raises(ValueError, match="multiple sensors"):
        series(rows + [other], "DISTANCE_SENSOR", "current_distance")
    assert series(rows + [other], "DISTANCE_SENSOR", "current_distance", sensor=(1, 0))["y"] == [0.5]


def test_flow_integrals_are_raw_and_isolated():
    rows = [record("OPTICAL_FLOW_RAD", sensor_id=1, quality=100, integration_time_us=v, integrated_x=0.2)
            for v in (10000, 0, -1, "inf")]
    assert series(rows, "OPTICAL_FLOW_RAD", "integrated_x")["y"] == [0.2, None, None, None]
    other = record("OPTICAL_FLOW_RAD", sensor_id=2, quality=100, integration_time_us=100, integrated_x=0.3)
    with pytest.raises(ValueError, match="multiple sensors"):
        series(rows + [other], "OPTICAL_FLOW_RAD", "integrated_x")
    assert series(rows + [other], "OPTICAL_FLOW_RAD", "integrated_x", sensor=(2, None))["y"] == [0.3]


def test_boot_missing_reversal_and_time_usec_not_guessed():
    rows = [record(time_boot_ms=1000, roll=1), record(time_boot_ms=1100, roll=2),
            record(time_usec=1_200_000, roll=3), record(time_boot_ms=1300, roll=4),
            record(time_boot_ms=100, roll=5)]
    result = series(rows, "ATTITUDE", "roll", time_axis="boot", origin=1)
    assert result["x"] == pytest.approx([0, 0.1, None, 0.3, None, -0.9])
    assert result["y"] == [1, 2, None, 4, None, 5]
    assert result["valid_count"] == 4 and result["invalid_count"] == 1
    assert result["gap_count"] == 1


@pytest.mark.parametrize("kwargs", [{"time_axis": "unknown"}, {"gap_seconds": 0},
                                       {"origin": "0"}, {"scale": float("inf")}])
def test_series_rejects_bad_configuration(kwargs):
    with pytest.raises(ValueError):
        series([], "ATTITUDE", "roll", **kwargs)


@pytest.mark.parametrize("key", ["packet_index", "recv_monotonic_ns"])
def test_loader_rejects_regressions_but_allows_equal(tmp_path, key):
    first = dict(record(), **{key: 2})
    session(tmp_path, [first, first])
    assert len(load_session(tmp_path).records) == 2
    session(tmp_path, [first, dict(record(), **{key: 1})])
    with pytest.raises(ValueError, match=rf"messages.jsonl:2:.*{key}"):
        load_session(tmp_path)


@pytest.mark.parametrize("msg_type,key", [("DISTANCE_SENSOR", "id"),
                                           ("DISTANCE_SENSOR", "orientation"),
                                           ("OPTICAL_FLOW_RAD", "sensor_id")])
@pytest.mark.parametrize("value", [[], {}, "1", True, -1, 256])
def test_bad_sensor_keys_fail_with_location(tmp_path, msg_type, key, value):
    row = record(msg_type, **{key: value})
    session(tmp_path, [row])
    with pytest.raises(ValueError, match=rf"messages.jsonl:1:.*{key}"):
        load_session(tmp_path)
    with pytest.raises(ValueError, match=key):
        series([row], msg_type, "quality")


def test_missing_sensor_keys_are_independent_unknown(tmp_path):
    rows = [record("DISTANCE_SENSOR", min_distance=1, max_distance=100, current_distance=50)]
    session(tmp_path, rows)
    assert load_session(tmp_path).records == rows
    assert series(rows, "DISTANCE_SENSOR", "current_distance")["y"] == [0.5]
    rows.append(record("DISTANCE_SENSOR", id=1, orientation=0))
    with pytest.raises(ValueError, match="multiple sensors"):
        series(rows, "DISTANCE_SENSOR", "current_distance")


@pytest.mark.parametrize("quality", [None, -1, 256, True, "100", float("nan")])
def test_invalid_flow_quality_is_missing(quality):
    row = record("OPTICAL_FLOW_RAD", quality=quality, integration_time_us=100, integrated_x=0.3)
    assert series([row], "OPTICAL_FLOW_RAD", "quality")["y"] == [None]
    assert series([row], "OPTICAL_FLOW_RAD", "integrated_x")["y"] == [None]


def test_zero_quality_and_integration_window_independence():
    rows = [record("OPTICAL_FLOW_RAD", quality=0, integration_time_us=100, integrated_x=0.3),
            record("OPTICAL_FLOW_RAD", quality=255, integration_time_us=0, integrated_x=0.3),
            record("OPTICAL_FLOW_RAD", quality=100, integrated_x=0.3)]
    assert series(rows, "OPTICAL_FLOW_RAD", "quality")["y"] == [0, 255, 100]
    assert series(rows, "OPTICAL_FLOW_RAD", "integrated_x")["y"] == [None, None, None]
