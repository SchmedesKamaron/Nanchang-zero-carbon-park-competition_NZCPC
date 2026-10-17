# -*- coding: utf-8 -*-
"""run_simulation.py 参数校验回归测试。"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import run_simulation as RS


def test_valid_params_ok():
    assert RS.apply_params({}) is True


def test_empty_scenes_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"scenes": ["bogus_unknown_scene"]})


def test_negative_capacity_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"storage_cap_mwh": -5})


def test_negative_work_days_rejected():
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"work_days": -1})


def test_soc_min_ge_max_rejected():
    from ems_sim import config as C
    C.SOC_MIN, C.SOC_MAX = 0.8, 0.5
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"soc_min": 0.8, "soc_max": 0.5})
    C.SOC_MIN, C.SOC_MAX = 0.2, 0.9  # 还原


def test_efficiency_out_of_range_rejected():
    from ems_sim import config as C
    C.ETA_CHARGE = 1.2
    with pytest.raises(RS.ConfigError):
        RS.apply_params({"eta_charge": 1.2})
    C.ETA_CHARGE = 0.95
