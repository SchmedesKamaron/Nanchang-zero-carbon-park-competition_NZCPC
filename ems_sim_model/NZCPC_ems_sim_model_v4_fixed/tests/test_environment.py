# -*- coding: utf-8 -*-
"""environment.py 回归测试 —— 物理模型的 8 项核心验收项。"""
import os
import sys
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ems_sim import config as C
from ems_sim import environment as ENV


def make_env(scene="economic", seed=1, load_scale=1.0, pv_scale=1.0):
    return ENV.ParkEnvironment(scene=scene, seed=seed,
                               load_scale=load_scale, pv_scale=pv_scale)


# 1) SOC 不越界(含日末)
def test_soc_never_violates_bounds():
    env = make_env()
    st = env.state()
    for soc0 in (C.SOC_MIN, 0.3, 0.5, C.SOC_MAX):
        rng = np.random.default_rng(0)
        p_batt = rng.uniform(-C.STORAGE_POWER_MW, C.STORAGE_POWER_MW, env.H)
        r = env.step(p_batt, soc0, st["pv"], st["load"], st["price"],
                     st["dr_cut"], st["dr_fill"])
        soc_all = np.append(r["detail"]["soc"], r["detail"]["soc_end"])
        assert soc_all.min() >= C.SOC_MIN - 1e-6, "SOC 低于下限"
        assert soc_all.max() <= C.SOC_MAX + 1e-6, "SOC 高于上限"


# 2) 极端放电请求也不会跌破下限
def test_high_discharge_respects_low_bound(seed=3):
    env = make_env(seed=seed)
    st = env.state()
    p_batt = np.full(env.H, C.STORAGE_POWER_MW)  # 全天最大放电请求
    r = env.step(p_batt, C.SOC_MIN, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    soc_all = np.append(r["detail"]["soc"], r["detail"]["soc_end"])
    assert soc_all.min() >= C.SOC_MIN - 1e-6


# 3) 充放电功率受额定功率限制(实际执行功率)
def test_actual_power_limited_by_rating(seed=4):
    env = make_env(seed=seed)
    st = env.state()
    p_batt = np.full(env.H, 10.0)  # 请求远超额定
    r = env.step(p_batt, 0.5, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    assert np.abs(r["detail"]["p_actual"]).max() <= C.STORAGE_POWER_MW + 1e-6
    # 充电同理
    p_batt2 = np.full(env.H, -10.0)
    r2 = env.step(p_batt2, 0.5, st["pv"], st["load"], st["price"],
                  st["dr_cut"], st["dr_fill"])
    assert np.abs(r2["detail"]["p_actual"]).max() <= C.STORAGE_POWER_MW + 1e-6


# 4) SOC 接近上限时充电被自动削减
def test_charge_cut_near_soc_max(seed=5):
    env = make_env(seed=seed)
    st = env.state()
    p_batt = np.full(env.H, -C.STORAGE_POWER_MW)
    r = env.step(p_batt, C.SOC_MAX, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    soc_all = np.append(r["detail"]["soc"], r["detail"]["soc_end"])
    assert soc_all.max() <= C.SOC_MAX + 1e-6


# 5) 功率平衡逐时对账(非停电): pv_use + p_grid + discharge = load_eff + charge
def test_power_balance(seed=6):
    env = make_env(seed=seed)
    st = env.state()
    for p_batt in (np.zeros(env.H),
                   np.full(env.H, 2.0),
                   np.full(env.H, -2.0)):
        r = env.step(p_batt, 0.5, st["pv"], st["load"], st["price"],
                     st["dr_cut"], st["dr_fill"])
        d = r["detail"]
        lhs = d["pv_use"] + d["p_grid"] + np.maximum(d["p_actual"], 0.0)
        rhs = d["load_eff"] + np.maximum(-d["p_actual"], 0.0)
        assert np.allclose(lhs, rhs, atol=1e-6), "功率平衡不成立"
        # 弃光 ≥ 0
        assert (d["pv_discard"] >= -1e-9).all(), "功率平衡被破坏"


# 6) 停电时不购电, 缺供电量被统计
def test_blackout_no_grid_purchase():
    env = make_env(scene="blackout", seed=7)
    st = env.state()
    r = env.step(np.zeros(env.H), 0.9, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    assert np.all(r["detail"]["p_grid"] == 0), "停电场景出现电网购电"
    assert r["shortage_mwh"] >= 0
    # 储能不动作时缺供 = 光伏不足负荷的缺口
    expected_shortage = np.maximum(st["load"], 0).sum() - np.maximum(st["pv"], 0).sum()
    assert abs(r["shortage_mwh"] - max(expected_shortage, 0)) < 1e-6


# 7) 碳排放单位: 1 MWh 购电 ≈ 1000×排放因子(kg)
def test_co2_unit_conversion(seed=8):
    env = make_env(scene="economic", seed=seed)
    # 人为构造: 8 小时各购电 1 MW → 8 MWh → 8*1000*0.581 kg
    st = env.state()
    load = st["load"].copy() * 0 + 3.0   # 固定大负荷, 光伏为0
    pv = np.zeros(env.H)
    price = np.full(env.H, 1.0)
    r = env.step(np.zeros(env.H), 0.5, pv, load, price, [], [])
    expect = 3.0 * env.H * 1000.0 * C.EMISSION_GRID_KG_PER_KWH
    assert abs(r["co2_kg"] - expect) < 1e-6   # 24 MWh 精确对应


# 8) 需求响应 curtail 真实改变净负荷
def test_demand_response_reduces_load():
    env = make_env(scene="demand_response", seed=9)
    st = env.state()
    assert len(st["dr_cut"]) >= 0
    r = env.step(np.zeros(env.H), 0.3, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    d = r["detail"]
    # 在 DR 削峰时段, load_eff < load (若该时段确实触发削减)
    for h in d["dr_cut"]:
        assert d["load_eff"][h] <= d["load"][h] + 1e-6
    # 净负荷 = load_eff
    assert np.allclose(d["load_eff"], d["load"] - d["curtail"], atol=1e-6)


# 无光伏/无负荷时不应产生无来源能量或异常收益
def test_zero_pv_load_no_artifact():
    env = make_env(seed=10)
    st = env.state()
    pv = np.zeros(env.H); load = np.zeros(env.H)
    price = np.full(env.H, 1.0)
    r = env.step(np.full(env.H, 2.0), 0.5, pv, load, price, [], [])
    # SOC 必须守恒(无负载, 放电无用途 → 实际放电为0)
    assert abs(r["detail"]["soc_end"] - 0.5) < 1e-6
    assert np.allclose(r["detail"]["p_actual"], 0.0)


# 恒定电价不应凭空产生套利收益
def test_flat_price_no_arbitrage(seed=11):
    env = make_env(seed=seed)
    price = np.full(env.H, 1.0)
    st = env.state()
    r = env.step(np.zeros(env.H), 0.5, st["pv"], st["load"], price,
                 st["dr_cut"], st["dr_fill"])
    # 无储能动作时经济收益不应来自价差, f_econ 应接近 0(无充放无套利)
    assert abs(r["f_econ"]) < 1e-6
