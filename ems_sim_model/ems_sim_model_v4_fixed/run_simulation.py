# -*- coding: utf-8 -*-
"""
run_simulation.py — 主运行入口(修复版, 支持前端填表导出的 params.json)
运行:  python run_simulation.py [--params params.json]
产出:  out/仿真结果报告.txt  +  终端文字化分析

【修复版变更】依据外部技术审查：
  1) apply_params() 增加严格参数校验(非法配置显式报错, 不静默计算)；
  2) 基线(Baseline)与所有 IPSO 重启共享同一份输入 state(负荷/光伏/电价/DR 信号), 公平对比；
  3) 多重启按"带罚项的 fitness"择优, 而非原始 F；
  4) 优化策略输出前做独立可行性验收(SOC/功率/循环), 报告明示通过或违规。
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


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def _validate_config(screen=()):
    """校验当前生效配置, 非法则抛 ConfigError。"""
    if C.STORAGE_CAP_MWH <= 0:
        raise ConfigError("储能容量(storage_cap_mwh)必须 > 0")
    if C.STORAGE_POWER_MW <= 0:
        raise ConfigError("储能额定功率(storage_power_mw)必须 > 0")
    if C.SOC_MIN >= C.SOC_MAX:
        raise ConfigError(f"SOC_MIN({C.SOC_MIN}) 必须小于 SOC_MAX({C.SOC_MAX})")
    if not (0 < C.ETA_CHARGE <= 1):
        raise ConfigError(f"充电效率({C.ETA_CHARGE})须在 (0,1]")
    if not (0 < C.ETA_DISCHARGE <= 1):
        raise ConfigError(f"放电效率({C.ETA_DISCHARGE})须在 (0,1]")
    if WORK_DAYS < 0:
        raise ConfigError("年生产天数(work_days)不能为负")
    if C.LOAD_PEAK_MW <= 0:
        raise ConfigError("负荷峰值(load_peak_mw)必须 > 0")
    for k, w in C.WEIGHTS.items():
        if k == "blackout":
            # 应急孤岛场景用缺供目标 F=-shortage, 非加权, 权重全 0 属正常
            continue
        alpha, beta, gamma = w["alpha"], w["beta"], w["gamma"]
        vals = [alpha, beta, gamma]
        if any(v < 0 for v in vals) or all(v == 0 for v in vals):
            raise ConfigError(f"场景 {k} 的权重须非负且不全为 0")
    for name in ("SUBSIDY_CUT", "SUBSIDY_FILL", "SUBSIDY_PEAKSHIFT"):
        if getattr(C, name) < 0:
            raise ConfigError(f"补贴标准 {name} 不能为负")
    if not SCENES:
        raise ConfigError("场景过滤后为空列表, 请检查 scenes 参数")


def apply_params(params):
    """把前端填写的参数覆盖到 config 上, 并做合法性校验。返回生效配置。"""
    global WORK_DAYS, SCENES
    p = params or {}
    if "work_days" in p:
        WORK_DAYS = int(p["work_days"])
    if "scenes" in p and isinstance(p["scenes"], list):
        SCENES = [s for s in p["scenes"] if s in SCENE_META]

    def setnum(k, f):
        if k in p:
            setattr(C, f, float(p[k]))

    setnum("storage_cap_mwh", "STORAGE_CAP_MWH")
    setnum("storage_power_mw", "STORAGE_POWER_MW")
    setnum("soc_min", "SOC_MIN")
    setnum("soc_max", "SOC_MAX")
    setnum("eta_charge", "ETA_CHARGE")
    setnum("eta_discharge", "ETA_DISCHARGE")
    setnum("max_cycles_day", "MAX_CYCLES_DAY")
    C.ENERGY_USEFUL = C.STORAGE_CAP_MWH * (C.SOC_MAX - C.SOC_MIN)
    setnum("pv_roof_mw", "PV_ROOF_MW")
    setnum("pv_canopy_mw", "PV_CANOPY_MW")
    C.PV_TOTAL_MW = C.PV_ROOF_MW + C.PV_CANOPY_MW
    setnum("load_peak_mw", "LOAD_PEAK_MW")
    if "price_valley" in p: C.PRICE["valley"] = float(p["price_valley"])
    if "price_flat" in p: C.PRICE["flat"] = float(p["price_flat"])
    if "price_peak" in p: C.PRICE["peak"] = float(p["price_peak"])
    setnum("subsidy_cut", "SUBSIDY_CUT")
    setnum("subsidy_fill", "SUBSIDY_FILL")
    setnum("subsidy_peakshift", "SUBSIDY_PEAKSHIFT")
    if "w_econ" in p and p["w_econ"]:
        C.WEIGHTS["economic"] = {**C.WEIGHTS["economic"], **p["w_econ"]}
    if "w_pv" in p and p["w_pv"]:
        C.WEIGHTS["pv_absorb"] = {**C.WEIGHTS["pv_absorb"], **p["w_pv"]}
    _validate_config(screen=SCENES)
    return True


# ---------------------------------------------------------------------------
# 单场景运行(公平对比)
# ---------------------------------------------------------------------------
def run_scene(scene_key, params):
    meta = SCENE_META[scene_key]
    ls = float(meta.get("load_scale", 1.0))
    ps = float(meta.get("pv_scale", 1.0))
    soc0 = float(meta.get("soc0", 0.2))
    if scene_key == "economic" and params.get("load_scale_econ") is not None:
        ls = float(params["load_scale_econ"])
    if scene_key == "pv_absorb" and params.get("pv_scale_pva") is not None:
        ps = float(params["pv_scale_pva"])

    env = ENV.ParkEnvironment(scene=scene_key, load_scale=ls, pv_scale=ps)

    # 生成一份共享输入, 基线与所有重启使用完全相同曲线(公平)
    state = env.state()

    base = OPT.HeuristicBaseline(env, soc0=soc0)
    r_base = base.run(state=state, soc0=soc0)

    restarts = int(params.get("restarts", 3))
    candidates = []
    for rs in range(restarts):
        opt = OPT.IPSOOptimizer(env, n_particles=50, n_iter=120, seed=7 + rs * 11)
        opt.soc0 = soc0
        o = opt.optimize(state=state)
        candidates.append(o)
    # 优先选择通过可行性验收(SOC/功率/循环)的重启结果, 再按 fitness 择优
    valid_pool = [o for o in candidates if o.get("valid")]
    pool = valid_pool if valid_pool else candidates
    best = max(pool, key=lambda o: o["fitness"])
    r_opt = best["res"]
    ok, issues = env.validate_plan(r_opt)

    return {"scene": scene_key, "meta": meta, "soc0": soc0,
            "r_base": r_base, "r_opt": r_opt, "valid": ok, "issues": issues,
            "state_used": state}


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------
def build_report(results):
    L = []
    L.append("=" * 66)
    L.append("南昌零碳园区储能系统仿真数学模型 · 运行报告(修复版)")
    L.append(f"生成时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"储能: {C.STORAGE_POWER_MW}MW/{C.STORAGE_CAP_MWH}MWh | "
             f"SOC {C.SOC_MIN*100:.0f}%~{C.SOC_MAX*100:.0f}% | "
             f"光伏 {C.PV_TOTAL_MW}MW | 负荷峰值 {C.LOAD_PEAK_MW}MW")
    L.append(f"电价: 谷{C.PRICE['valley']:.2f}/平{C.PRICE['flat']:.2f}/峰{C.PRICE['peak']:.2f}"
             f"元/kWh | 年生产天数 {WORK_DAYS}")
    L.append("=" * 66)

    for r in results:
        rb, ro = r["r_base"], r["r_opt"]
        L.append("")
        L.append(f"■ {r['meta']['name']} (SOC初值 {r['soc0']})")
        L.append(f"  · 综合目标 F:      基线 {rb['F']:.1f} → 优化 {ro['F']:.1f}"
                 f"   (Δ {ro['F']-rb['F']:+.1f})")
        L.append(f"  · 光伏自用率:      基线 {rb['absorb_rate']*100:.1f}% → 优化 {ro['absorb_rate']*100:.1f}%"
                 f"   (弃光 {ro['discard_mwh']:.2f} MWh)")
        L.append(f"  · 峰谷差削减率:    基线 {rb['peak_reduct']*100:.1f}% → 优化 {ro['peak_reduct']*100:.1f}%")
        L.append(f"  · 外购电量(日):   基线 {rb['grid_buy_mwh']:.2f} → 优化 {ro['grid_buy_mwh']:.2f} MWh")
        L.append(f"  · 碳排放(日):     基线 {rb['co2_kg']:.1f} → 优化 {ro['co2_kg']:.1f} kg")
        L.append(f"  · 日净经济收益:   优化 {ro['f_econ']:.0f} 元/日"
                 f"   (含 DR 补贴 {ro['dr_revenue']:.0f} 元/日)")
        L.append(f"  · 日循环次数:      优化 {ro['cycles_day']:.2f}  缺供 {ro['shortage_mwh']:.2f} MWh")
        L.append(f"  · 可行性验收:      {'通过 ✓' if r['valid'] else '违规 ✗ ' + '; '.join(r['issues'])}")
    return "\n".join(L)


def yearly_business(results):
    lines = []
    lines.append("")
    lines.append("-" * 66)
    lines.append(f"【年经济效益概算】(优化策略, 年生产天数 {WORK_DAYS})")
    for r in results:
        ro = r["r_opt"]; name = r["meta"]["name"]
        net_daily = ro["f_econ"]
        annual = net_daily * WORK_DAYS / 1e4   # 万元/年
        dr = f"  (含需求响应补贴 {ro['dr_revenue']:.0f}元/日)" if ro["dr_revenue"] else ""
        lines.append(f"  · {name}: 日净收益 {net_daily:.0f}元/日 → 年约 {annual:.2f} 万元{dr}")
    return "\n".join(lines)


def main(params=None, verbose=True):
    params = params or {}
    _validate_config()
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
