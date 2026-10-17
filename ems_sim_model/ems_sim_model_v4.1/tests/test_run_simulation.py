# -*- coding: utf-8 -*-
"""run_simulation.py 参数校验与主流程回归测试。"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import run_simulation as RS
from ems_sim import config as C


def test_valid_params_ok():
    assert RS.apply_params({}) is True


def test_empty_scenes_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"scenes": ["bogus_unknown_scene"]})


def test_negative_capacity_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"storage_cap_mwh": -5})
    C.STORAGE_CAP_MWH = 8.0  # 还原


def test_zero_power_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"storage_power_mw": 0})
    C.STORAGE_POWER_MW = 4.0  # 还原


def test_negative_work_days_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"work_days": -1})


def test_soc_min_ge_max_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"soc_min": 0.8, "soc_max": 0.5})
    C.SOC_MIN, C.SOC_MAX = 0.2, 0.9  # 还原


def test_efficiency_out_of_range_rejected():
    C.ETA_CHARGE = 0.95  # 还原
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"eta_charge": 1.2})
    C.ETA_CHARGE = 0.95


def test_all_scenes_run_and_valid(monkeypatch):
    """全场景主流程: 四场景优化结果均通过可行性验收。"""
    monkeypatch.setattr(RS, "SCENES", ["economic", "pv_absorb",
                                       "demand_response", "blackout"])
    params = {"restarts": 1, "work_days": 250}
    report, results = RS.main(params, verbose=False)
    assert len(results) == 4
    for r in results:
        assert r["valid"] is True, f"{r['scene']}: {r['issues']}"
    assert "可行性验收" in report


def test_single_scene_json_payload(monkeypatch):
    """单场景 JSON 载荷: 基线/优化约束检查齐全且优化可行。"""
    monkeypatch.setattr(RS, "SCENES", ["economic"])
    payload = RS.single_scene_payload("economic", {"restarts": 1}, seed=1,
                                      n_particles=10, n_iter=8)
    assert payload["scene"] == "economic"
    assert payload["optimized"]["constraints"]["valid"] is True
    assert "plan_mw" in payload["optimized"]
    assert "convergence" in payload["optimized"]
    assert payload["baseline"]["constraints"]["valid"] is True
