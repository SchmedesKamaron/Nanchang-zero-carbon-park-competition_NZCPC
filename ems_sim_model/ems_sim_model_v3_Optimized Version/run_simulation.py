# -*- coding: utf-8 -*-
"""
run_simulation.py — 主运行入口 (支持前端填表导出的 params.json)
运行:  python run_simulation.py [--params params.json]
产出:  out/仿真结果报告.txt  +  终端文字化分析
"""
import os
import sys
import json
import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ems_sim import config as C
from ems_sim import environment as ENV
from ems_sim import optimizer as OPT
from ems_sim.scenes import SCENE_META

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUTDIR, exist_ok=True)

WORK_DAYS = 250  # 年生产天数
SCENES = list(SCENE_META.keys())


def apply_params(params):
    """把前端填写的参数覆盖到 config 上, 返回生效配置。"""
    global WORK_DAYS, SCENES
    p = params or {}
    if "work_days" in p: WORK_DAYS = int(p["work_days"])
    if "scenes" in p and isinstance(p["scenes"], list) and p["scenes"]:
        SCENES = [s for s in p["scenes"] if s in SCENE_META]

    setnum = lambda k, f, dv=None: (setattr(C, f, float(p[k])) if k in p else None)
    # 储能
    setnum("storage_cap_mwh", "STORAGE_CAP_MWH")
    setnum("storage_power_mw", "STORAGE_POWER_MW")
    setnum("soc_min", "SOC_MIN")
    setnum("soc_max", "SOC_MAX")
    setnum("eta_charge", "ETA_CHARGE")
    setnum("eta_discharge", "ETA_DISCHARGE")
    setnum("max_cycles_day", "MAX_CYCLES_DAY")
    C.ENERGY_USEFUL = C.STORAGE_CAP_MWH * (C.SOC_MAX - C.SOC_MIN)
    # 光伏
    setnum("pv_roof_mw", "PV_ROOF_MW")
    setnum("pv_canopy_mw", "PV_CANOPY_MW")
    C.PV_TOTAL_MW = C.PV_ROOF_MW + C.PV_CANOPY_MW
    # 负荷
    setnum("load_peak_mw", "LOAD_PEAK_MW")
    # 电价
    if "price_valley" in p: C.PRICE["valley"] = float(p["price_valley"])
    if "price_flat" in p: C.PRICE["flat"] = float(p["price_flat"])
    if "price_peak" in p: C.PRICE["peak"] = float(p["price_peak"])
    # 补贴
    setnum("subsidy_cut", "SUBSIDY_CUT")
    setnum("subsidy_fill", "SUBSIDY_FILL")
    setnum("subsidy_peakshift", "SUBSIDY_PEAKSHIFT")
    # 权重
    if "w_econ" in p and p["w_econ"]: C.WEIGHTS["economic"] = p["w_econ"]
    if "w_pv" in p and p["w_pv"]: C.WEIGHTS["pv_absorb"] = p["w_pv"]
    if "w_dr" in p and p["w_dr"]: C.WEIGHTS["demand_response"] = p["w_dr"]
    # 排放
    setnum("emission", "EMISSION_GRID_KG_PER_KWH")


def run_scene(scene_key, params):
    meta = SCENE_META[scene_key]
    ls = meta["load_scale"]
    ps = meta["pv_scale"]
    soc0 = meta["soc0"]
    if scene_key == "economic" and params.get("load_scale_econ") is not None:
        ls = float(params["load_scale_econ"])
    if scene_key == "pv_absorb" and params.get("pv_scale_pva") is not None:
        ps = float(params["pv_scale_pva"])

    env = ENV.ParkEnvironment(scene=scene_key, load_scale=ls, pv_scale=ps)

    base = OPT.HeuristicBaseline(env, soc0=soc0)
    r_base = base.run()

    # IPSO 多次随机重启取最优, 消除单次运行的随机性波动, 保证收敛质量
    restarts = int(params.get("restarts", 3))
    best = None
    for rs in range(restarts):
        opt = OPT.IPSOOptimizer(env, n_particles=50, n_iter=120, seed=7 + rs * 11)
        opt.soc0 = soc0
        o = opt.optimize(state=env.state())
        if best is None or o["res"]["F"] > best[1]:
            best = (o["res"], o["res"]["F"])
    r_opt = best[0]

    return {"scene": scene_key, "meta": meta, "soc0": soc0,
            "r_base": r_base, "r_opt": r_opt}


def build_report(results):
    L = []
    L.append("=" * 62)
    L.append("南昌零碳园区储能系统仿真数学模型 · 运行报告")
    L.append(f"生成时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"储能: {C.STORAGE_POWER_MW}MW/{C.STORAGE_CAP_MWH}MWh | "
             f"SOC {C.SOC_MIN*100:.0f}%~{C.SOC_MAX*100:.0f}% | "
             f"光伏 {C.PV_TOTAL_MW}MW | 负荷峰值 {C.LOAD_PEAK_MW}MW")
    L.append(f"电价: 谷{C.PRICE['valley']:.2f}/平{C.PRICE['flat']:.2f}/峰{C.PRICE['peak']:.2f}"
             f" | 年生产天数 {WORK_DAYS}")
    L.append("=" * 62)

    for r in results:
        rb, ro = r["r_base"], r["r_opt"]
        L.append("")
        L.append(f"■ {r['meta']['name']} (SOC初值 {r['soc0']})")
        L.append(f"  · 综合目标值 F:  基线 {rb['F']:.1f} → 优化 {ro['F']:.1f}")
        L.append(f"  · 光伏自用率:    基线 {rb['absorb_rate']*100:.1f}% → 优化 {ro['absorb_rate']*100:.1f}%"
                 f"   (弃光 {ro['discard_mwh']:.2f} MWh)")
        L.append(f"  · 峰谷差削减率:  基线 {rb['peak_reduct']*100:.1f}% → 优化 {ro['peak_reduct']*100:.1f}%")
        L.append(f"  · 外购电量:      基线 {rb['grid_buy_mwh']:.2f} → 优化 {ro['grid_buy_mwh']:.2f} MWh/日")
        L.append(f"  · 日碳排放:      基线 {rb['co2_kg']:.1f} → 优化 {ro['co2_kg']:.1f} kg")
        L.append(f"  · 日循环次数:    优化 {ro['cycles_day']:.2f}")
    return "\n".join(L)


def yearly_business(results):
    """年经济效益概算: 日净收益(f_econ)+需求响应补贴, 乘年生产天数。"""
    lines = []
    lines.append("")
    lines.append("-" * 62)
    lines.append(f"【年经济效益概算】(基于优化策略, 年生产天数 {WORK_DAYS})")
    for r in results:
        ro = r["r_opt"]; d = ro["detail"]; name = r["meta"]["name"]
        rev_arb = ro["f_econ"]          # 元/日: 放电收益-充电成本-运维
        dr_inc = 0.0
        if r["scene"] == "demand_response":
            cut_q = sum(d["batt_out_hr"][h] * 1000.0 for h in C.PEAK_HOURS)
            fill_q = sum(d["batt_in_hr"][h] * 1000.0 for h in C.VALLEY_HOURS)
            dr_inc = C.SUBSIDY_CUT * cut_q + C.SUBSIDY_FILL * fill_q
        annual = (rev_arb + dr_inc) * WORK_DAYS / 1e4   # 万元/年
        extra = f"  (含需求响应补贴 {dr_inc:.0f}元/日)" if dr_inc else ""
        lines.append(f"  · {name}: 日净收益 {rev_arb:.0f}元/日 → 年约 {annual:.2f} 万元{extra}")
    return "\n".join(lines)


def main(params=None, verbose=True):
    params = params or {}
    apply_params(params)
    results = [run_scene(s, params) for s in SCENES]

    report = build_report(results) + "\n" + yearly_business(results)
    if verbose:
        print(report)
    rep_path = os.path.join(OUTDIR, "仿真结果报告.txt")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    return report, results


if __name__ == "__main__":
    params = None
    if len(sys.argv) > 2 and sys.argv[1] == "--params":
        with open(sys.argv[2], encoding="utf-8") as f:
            params = json.load(f)
    main(params)
