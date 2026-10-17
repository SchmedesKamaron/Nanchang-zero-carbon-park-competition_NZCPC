# -*- coding: utf-8 -*-
"""optimizer.py 回归测试 —— 罚函数方向、可变时域滚动窗口、公平对比、可行性。"""
import os
import sys
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ems_sim import config as C
from ems_sim import environment as ENV
from ems_sim import optimizer as OPT


def make_env(scene="economic", seed=1, load_scale=1.0, pv_scale=1.0):
    return ENV.ParkEnvironment(scene=scene, seed=seed,
                               load_scale=load_scale, pv_scale=pv_scale)


# SOC 罚函数方向正确: 越限越大罚函数应更大 (审查 P0: 罚函数方向写反)
def test_soc_penalty_direction():
    env = make_env(seed=1)
    opt = OPT.IPSOOptimizer(env, seed=1)
    st = env.state()
    opt._env_state = st

    def forced_result(soc_arr):
        return {"F": 100.0, "cycles_day": 1.0,
                "detail": {"soc": soc_arr[:-1], "soc_end": float(soc_arr[-1])}}

    lo = forced_result(np.full(25, 0.10))   # 低于 SOC_MIN=0.2
    hi = forced_result(np.full(25, 0.99))   # 高于 SOC_MAX=0.9
    ok = forced_result(np.full(25, 0.5))    # 界内

    def penalty(r):
        soc_all = np.append(r["detail"]["soc"], r["detail"]["soc_end"])
        low = np.maximum(C.SOC_MIN - soc_all, 0.0)
        high = np.maximum(soc_all - C.SOC_MAX, 0.0)
        return C.PEN_SOC_FACTOR * np.sum(low ** 2 + high ** 2)

    assert penalty(lo) > 0
    assert penalty(hi) > 0
    assert penalty(ok) == 0


# IPSO 优化结果 SOC 全程在界内且通过可行性验收
def test_optimo_soc_feasible():
    env = make_env(seed=2)
    opt = OPT.IPSOOptimizer(env, n_particles=12, n_iter=12, seed=2)
    opt.soc0 = 0.2
    o = opt.optimize()
    assert o["res"]["detail"]["soc_end"] >= C.SOC_MIN - 1e-6
    assert o["valid"] is True, o["issues"]


# 滚动窗口(4/6/24h)可运行且窗口间 SOC 连续拼接(审查 P0: 日内滚动 IndexError)
@pytest.mark.parametrize("horizon", [4, 6, 24])
def test_intraday_rolling_window(horizon):
    env = make_env(scene="economic", seed=3)
    sched = OPT.ThreeLayerScheduler(env, horizon=horizon,
                                    n_particles=10, n_iter=8)
    sched.opt.soc0 = 0.2
    res = sched.intraday()
    assert len(res["x"]) == env.H
    d = res["res"]["detail"]
    soc_all = np.append(d["soc"], d["soc_end"])
    assert soc_all.min() >= C.SOC_MIN - 1e-6
    assert soc_all.max() <= C.SOC_MAX + 1e-6


# 基线与优化共享同一输入 => 公平对比 (审查: 基线与优化用不同曲线)
def test_baseline_and_opt_share_state():
    env = make_env(seed=4)
    state = env.state()
    base = OPT.HeuristicBaseline(env, soc0=0.2)
    rb = base.run(state=state, soc0=0.2)
    opt = OPT.IPSOOptimizer(env, n_particles=10, n_iter=8, seed=4)
    opt.soc0 = 0.2
    ro = opt.optimize(state=state)
    assert np.all(rb["detail"]["pv"] == ro["res"]["detail"]["pv"])
    assert np.all(rb["detail"]["load"] == ro["res"]["detail"]["load"])
    assert np.all(rb["detail"]["price"] == ro["res"]["detail"]["price"])


# 停电场景优化: 电网不购电, 有缺供统计
def test_blackout_opt_no_grid():
    env = make_env(scene="blackout", seed=5)
    opt = OPT.IPSOOptimizer(env, n_particles=10, n_iter=8, seed=5)
    opt.soc0 = 0.9
    o = opt.optimize()
    assert np.all(o["res"]["detail"]["p_grid"] == 0)
    assert o["res"]["shortage_mwh"] >= 0
    assert o["valid"] is True, o["issues"]
