# -*- coding: utf-8 -*-
"""environment.py 回归测试 —— 物理模型的验收项(对照审查报告第四、五节)。"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ems_sim import config as C
from ems_sim import environment as ENV


def make_env(scene="economic", seed=1, load_scale=1.0, pv_scale=1.0):
    return ENV.ParkEnvironment(scene=scene, seed=seed,
                               load_scale=load_scale, pv_scale=pv_scale)


def _soc_all(r):
    return np.append(r["detail"]["soc"], r["detail"]["soc_end"])


# 1) SOC 不越界(含日末)
def test_soc_never_violates_bounds():
    env = make_env()
    st = env.state()
    for soc0 in (C.SOC_MIN, 0.3, 0.5, C.SOC_MAX):
        rng = np.random.default_rng(0)
        p_batt = rng.uniform(-C.STORAGE_POWER_MW, C.STORAGE_POWER_MW, env.H)
        r = env.step(p_batt, soc0, st["pv"], st["load"], st["price"],
                     st["dr_cut"], st["dr_fill"])
        soc_all = _soc_all(r)
        assert soc_all.min() >= C.SOC_MIN - 1e-6, "SOC 低于下限"
        assert soc_all.max() <= C.SOC_MAX + 1e-6, "SOC 高于上限"


# 2) 极端放电请求也不会跌破下限(审查 P0: SOC 可跌破下限)
def test_high_discharge_respects_low_bound(seed=3):
    env = make_env(seed=seed)
    st = env.state()
    p_batt = np.full(env.H, C.STORAGE_POWER_MW)  # 全天最大放电请求
    r = env.step(p_batt, C.SOC_MIN, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    soc_all = _soc_all(r)
    assert soc_all.min() >= C.SOC_MIN - 1e-6


# 3) 从 SOC_MIN 起放电, 实际放电功率应为 0(无可用能量)
def test_no_discharge_at_soc_min(seed=3):
    env = make_env(seed=seed)
    st = env.state()
    # 构造无光伏、大负荷，确保有可放空间请求
    pv = np.zeros(env.H)
    load = np.full(env.H, 3.0)
    r = env.step(np.full(env.H, 4.0), C.SOC_MIN, pv, load,
                 st["price"], [], [])
    # 放电可用能量 = (SOC_MIN-SOC_MIN)*... = 0
    assert np.allclose(r["detail"]["p_actual"], 0.0, atol=1e-6)
    assert abs(r["detail"]["soc_end"] - C.SOC_MIN) < 1e-6


# 4) 充放电实际执行功率受额定功率限制
def test_actual_power_limited_by_rating(seed=4):
    env = make_env(seed=seed)
    st = env.state()
    for req in (10.0, -10.0):
        r = env.step(np.full(env.H, req), 0.5, st["pv"], st["load"],
                     st["price"], st["dr_cut"], st["dr_fill"])
        assert np.abs(r["detail"]["p_actual"]).max() <= C.STORAGE_POWER_MW + 1e-6


# 5) SOC 接近上限时充电被自动削减(审查: SOC 达上限不再超限充电)
def test_charge_cut_near_soc_max(seed=5):
    env = make_env(seed=seed)
    st = env.state()
    p_batt = np.full(env.H, -C.STORAGE_POWER_MW)
    r = env.step(p_batt, C.SOC_MAX, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    soc_all = _soc_all(r)
    assert soc_all.max() <= C.SOC_MAX + 1e-6


# 6) 功率平衡逐时对账(非停电): pv_use + p_grid + 放电 = load_eff + 充电
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
        assert (d["pv_discard"] >= -1e-9).all()
        assert (d["p_grid"] >= -1e-9).all()


# 7) 停电时不购电, 缺供电量被统计(审查 P0: 停电场景仍从电网购电)
def test_blackout_no_grid_purchase():
    env = make_env(scene="blackout", seed=7)
    st = env.state()
    r = env.step(np.zeros(env.H), 0.9, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    assert np.all(r["detail"]["p_grid"] == 0), "停电场景出现电网购电"
    assert r["shortage_mwh"] >= 0
    # 储能不动作时缺供 = 光伏不足负荷的缺口
    expected_shortage = np.maximum(st["load"] - st["pv"], 0.0).sum()
    assert abs(r["shortage_mwh"] - expected_shortage) < 1e-6


# 8) 停电时储能放电可降低缺供
def test_blackout_storage_reduces_shortage(seed=7):
    env = make_env(scene="blackout", seed=seed)
    st = env.state()
    r0 = env.step(np.zeros(env.H), 0.9, st["pv"], st["load"], st["price"],
                  st["dr_cut"], st["dr_fill"])
    # 峰段放电帮助供关键负荷
    p = np.zeros(env.H)
    for h in C.PEAK_HOURS:
        p[h] = C.STORAGE_POWER_MW
    r1 = env.step(p, 0.9, st["pv"], st["load"], st["price"],
                  st["dr_cut"], st["dr_fill"])
    assert r1["shortage_mwh"] <= r0["shortage_mwh"] + 1e-6


# 9) 碳排放单位: 1 MWh 购电 ≈ 1000×排放因子(kg) (审查: 少算 1000 倍)
def test_co2_unit_conversion(seed=8):
    env = make_env(scene="economic", seed=seed)
    load = np.full(env.H, 3.0)   # 固定大负荷, 光伏为 0
    pv = np.zeros(env.H)
    price = np.full(env.H, 1.0)
    r = env.step(np.zeros(env.H), 0.5, pv, load, price, [], [])
    expect = 3.0 * env.H * 1000.0 * C.EMISSION_GRID_KG_PER_KWH
    assert abs(r["co2_kg"] - expect) < 1e-6   # 72 MWh 精确对应


# 10) 需求响应 curtail 真实改变净负荷(审查 P1: curtail 未落地)
def test_demand_response_reduces_load():
    env = make_env(scene="demand_response", seed=9)
    st = env.state()
    r = env.step(np.zeros(env.H), 0.3, st["pv"], st["load"], st["price"],
                 st["dr_cut"], st["dr_fill"])
    d = r["detail"]
    for h in d["dr_cut"]:
        assert d["load_eff"][h] <= d["load"][h] + 1e-6
    assert np.allclose(d["load_eff"], d["load"] - d["curtail"], atol=1e-6)
    assert (d["curtail"] >= 0).all()


# 11) 无光伏/无负荷时不应产生无来源能量或异常收益
def test_zero_pv_load_no_artifact():
    env = make_env(seed=10)
    pv = np.zeros(env.H); load = np.zeros(env.H)
    price = np.full(env.H, 1.0)
    r = env.step(np.full(env.H, 2.0), 0.5, pv, load, price, [], [])
    assert abs(r["detail"]["soc_end"] - 0.5) < 1e-6   # 无负载可放, SOC 守恒
    assert np.allclose(r["detail"]["p_actual"], 0.0)
    assert r["grid_buy_mwh"] == 0


# 12) 恒定电价不应凭空产生套利收益 (审查: 分时电价恒定无价差收益)
def test_flat_price_no_arbitrage(seed=11):
    env = make_env(seed=seed)
    price = np.full(env.H, 1.0)
    st = env.state()
    r = env.step(np.zeros(env.H), 0.5, st["pv"], st["load"], price,
                 st["dr_cut"], st["dr_fill"])
    # 无储能动作: f_econ = 基线购电成本 - 实际购电成本(相同) - 0 - 0 = 0
    assert abs(r["f_econ"]) < 1e-6


# 13) 充电 SOC 更新公式: SOC_{t+1}=SOC_t + P·dt·η_ch/E_cap (审查 P0: 公式不一致)
def test_charge_soc_formula(seed=12):
    env = make_env(seed=seed)
    st = env.state()
    pv = np.zeros(env.H); load = np.zeros(env.H)  # 无负荷, 充电可全额执行
    price = np.full(env.H, 1.0)
    soc0 = 0.3
    Pch = 2.0   # 2 MW 充电
    p_batt = np.zeros(env.H); p_batt[0] = -Pch
    r = env.step(p_batt, soc0, pv, load, price, [], [])
    expect = soc0 + Pch * C.DT_HOURS * C.ETA_CHARGE / C.STORAGE_CAP_MWH
    assert abs(r["detail"]["soc"][1] - expect) < 1e-9


# 14) 放电 SOC 更新公式: SOC_{t+1}=SOC_t - P·dt/(E_cap·η_dis)
def test_discharge_soc_formula(seed=13):
    env = make_env(seed=seed)
    st = env.state()
    pv = np.zeros(env.H); load = np.full(env.H, 3.0)  # 大负荷, 可全额放电
    price = np.full(env.H, 1.0)
    soc0 = 0.5
    Pdis = 2.0
    p_batt = np.zeros(env.H); p_batt[0] = Pdis
    r = env.step(p_batt, soc0, pv, load, price, [], [])
    expect = soc0 - Pdis * C.DT_HOURS / (C.STORAGE_CAP_MWH * C.ETA_DISCHARGE)
    assert abs(r["detail"]["soc"][1] - expect) < 1e-9


# 15) fail-loud 入参校验: 非法输入显式报错(不静默计算)
def test_input_validation_raises():
    import pytest
    env = make_env(seed=14)
    st = env.state()
    pv, load, price = st["pv"], st["load"], st["price"]
    # 长度不匹配
    with pytest.raises(ValueError):
        env.step(np.zeros(2), 0.5, pv, load, price, [], [])
    # p_batt 长度不匹配
    with pytest.raises(ValueError):
        env.step(np.zeros(env.H - 1), 0.5, pv, load, price, [], [])
    # 含负数输入
    with pytest.raises(ValueError):
        env.step(np.zeros(env.H), 0.5, pv, -load, price, [], [])
    # 初始 SOC 越界
    with pytest.raises(ValueError):
        env.step(np.zeros(env.H), 0.05, pv, load, price, [], [])
    with pytest.raises(ValueError):
        env.step(np.zeros(env.H), 0.99, pv, load, price, [], [])
    # 含 NaN
    bad_pv = pv.copy(); bad_pv[0] = np.nan
    with pytest.raises(ValueError):
        env.step(np.zeros(env.H), 0.5, bad_pv, load, price, [], [])


# 16) 短路滚动窗口: 非 24 整数倍时域也能运行且 SOC 连续
def test_short_horizon_rolling_window():
    from ems_sim import optimizer as OPT
    env = make_env(seed=15, load_scale=1.0, pv_scale=1.0)
    env.H = 10  # 非 24 的时域
    st = env.state()
    sched = OPT.ThreeLayerScheduler(env, horizon=4, n_particles=10, n_iter=8)
    sched.opt.soc0 = 0.3
    out = sched.intraday()
    assert len(out["x"]) == 10
    soc_all = np.append(out["res"]["detail"]["soc"], out["res"]["detail"]["soc_end"])
    assert soc_all.min() >= C.SOC_MIN - 1e-6
    assert soc_all.max() <= C.SOC_MAX + 1e-6
