# -*- coding: utf-8 -*-
"""
run_simulation.py — 主运行入口 (v4, 支持前端填表导出的 params.json)

两种运行模式：
  1) 四场景汇总报告(默认)：
       python run_simulation.py [--params params.json]
       产出: out/仿真结果报告.txt
  2) 单场景 JSON 输出(便于脚本/前端消费)：
       python run_simulation.py --scene economic --seed 42 \
           --out results.json [--params params.json]
       产出: 指定 JSON(含基线/优化对比、约束检查、充放电计划、收敛曲线)

依据《v3_Optimized_Version_审查意见.md》修复：
  1) apply_params() 增加严格参数校验(非法配置显式报错, 不静默计算)；
  2) 基线(Baseline)与所有 IPSO 重启共享同一份输入 state, 公平对比；
  3) 多重启按"带罚项的 fitness"择优, 且优先选择通过可行性验收的重启结果；
  4) 优化策略输出前做独立可行性验收(SOC/功率/循环/停电购电), 报告明示通过或违规。
"""
import argparse
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


def _validate_config(scene_keys):
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
    if C.DT_HOURS <= 0:
        raise ConfigError("时间步长(DT_HOURS)必须 > 0")
    for k in scene_keys:
        if k == "blackout":
            continue  # 应急孤岛场景用缺供目标 F=-shortage, 权重全 0 属正常
        w = C.WEIGHTS.get(k)
        if not w:
            continue
        vals = [w["alpha"], w["beta"], w["gamma"]]
        if any(v < 0 for v in vals) or all(v == 0 for v in vals):
            raise ConfigError(f"场景 {k} 的权重须非负且不全为 0")
    for name in ("SUBSIDY_CUT", "SUBSIDY_FILL", "SUBSIDY_PEAKSHIFT"):
        if getattr(C, name) < 0:
            raise ConfigError(f"补贴标准 {name} 不能为负")
    if not scene_keys:
        raise ConfigError("场景过滤后为空列表, 请检查 scenes 参数")


def apply_params(params):
    """把前端填写的参数覆盖到 config 上, 并做合法性校验。返回 True。"""
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
    if "w_dr" in p and p["w_dr"]:
        C.WEIGHTS["demand_response"] = {**C.WEIGHTS["demand_response"], **p["w_dr"]}
    if "emission" in p:
        C.EMISSION_GRID_KG_PER_KWH = float(p["emission"])
    _validate_config(SCENES)
    return True


# ---------------------------------------------------------------------------
# 单场景运行(公平对比)
# ---------------------------------------------------------------------------
def run_scene(scene_key, params, seed=42, n_particles=50, n_iter=120, restarts=None):
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

    if restarts is None:
        restarts = int(params.get("restarts", 3))
    candidates = []
    for rs in range(restarts):
        opt = OPT.IPSOOptimizer(env, n_particles=n_particles, n_iter=n_iter,
                                seed=seed + rs * 11)
        opt.soc0 = soc0
        o = opt.optimize(state=state)
        candidates.append(o)
    # 优先选择通过可行性验收(SOC/功率/循环/停电购电)的重启结果, 再按 fitness 择优
    valid_pool = [o for o in candidates if o.get("valid")]
    pool = valid_pool if valid_pool else candidates
    best = max(pool, key=lambda o: o["fitness"])
    r_opt = best["res"]
    ok, issues = env.validate_plan(r_opt)

    return {"scene": scene_key, "meta": meta, "soc0": soc0,
            "r_base": r_base, "r_opt": r_opt, "valid": ok, "issues": issues,
            "state_used": state, "best_fitness": best["fitness"],
            "best_plan": best["x"], "best_history": best["history"],
            "restarts": restarts}


# ---------------------------------------------------------------------------
# 单场景 JSON 载荷
# ---------------------------------------------------------------------------
def single_scene_payload(scene_key, params, seed=42, n_particles=50,
                         n_iter=120, restarts=None):
    """运行单场景, 返回含基线/优化对比、约束检查、计划、收敛曲线的 dict。"""
    r = run_scene(scene_key, params, seed=seed, n_particles=n_particles,
                  n_iter=n_iter, restarts=restarts)
    rb, ro = r["r_base"], r["r_opt"]
    return {
        "version": "v4", "scene": scene_key, "seed": seed,
        "restarts": r["restarts"],
        "baseline": {
            "F": rb["F"], "f_econ": rb["f_econ"], "absorb_rate": rb["absorb_rate"],
            "peak_reduct": rb["peak_reduct"], "grid_buy_mwh": rb["grid_buy_mwh"],
            "co2_kg": rb["co2_kg"], "soc_end": rb["soc_end"],
            "constraints": _constraint_dict(rb),
        },
        "optimized": {
            "F": ro["F"], "fitness": r["best_fitness"],
            "f_econ": ro["f_econ"], "absorb_rate": ro["absorb_rate"],
            "peak_reduct": ro["peak_reduct"], "grid_buy_mwh": ro["grid_buy_mwh"],
            "co2_kg": ro["co2_kg"], "soc_end": ro["soc_end"],
            "shortage_mwh": ro["shortage_mwh"],
            "econ_norm": ro["econ_norm"], "absorb_norm": ro["absorb_norm"],
            "grid_norm": ro["grid_norm"], "cycles_day": ro["cycles_day"],
            "constraints": _constraint_dict(ro),
            "plan_mw": [round(float(x), 6) for x in r["best_plan"]],
            "convergence": [round(float(x), 4) for x in r["best_history"]],
        },
    }


def _constraint_dict(res):
    d = res["detail"]
    return {
        "valid": bool(d["soc"].min() >= C.SOC_MIN - 1e-6
                      and d["soc_end"] >= C.SOC_MIN - 1e-6
                      and d["soc"].max() <= C.SOC_MAX + 1e-6
                      and d["soc_end"] <= C.SOC_MAX + 1e-6
                      and np.abs(d["p_actual"]).max() <= C.STORAGE_POWER_MW + 1e-6
                      and res["cycles_day"] <= C.MAX_CYCLES_DAY + 1e-6
                      and ((not d.get("blackout", False))
                           or np.all(np.abs(d["p_grid"]) <= 1e-6))),
        "soc_within_bounds": bool(
            d["soc"].min() >= C.SOC_MIN - 1e-6 and d["soc_end"] >= C.SOC_MIN - 1e-6
            and d["soc"].max() <= C.SOC_MAX + 1e-6 and d["soc_end"] <= C.SOC_MAX + 1e-6),
        "power_within_bounds": bool(np.abs(d["p_actual"]).max() <= C.STORAGE_POWER_MW + 1e-6),
        "grid_zero_when_unavailable": bool(
            (not d.get("blackout", False)) or np.all(np.abs(d["p_grid"]) <= 1e-6)),
        "cycles_within_limit": bool(res["cycles_day"] <= C.MAX_CYCLES_DAY + 1e-6),
        "shortage_mwh": res["shortage_mwh"],
    }


# ---------------------------------------------------------------------------
# 四场景汇总报告
# ---------------------------------------------------------------------------
def build_report(results):
    L = []
    L.append("=" * 66)
    L.append("南昌零碳园区储能系统仿真数学模型 · 运行报告 (v4)")
    L.append(f"生成时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"储能: {C.STORAGE_POWER_MW}MW/{C.STORAGE_CAP_MWH}MWh | "
             f"SOC {C.SOC_MIN*100:.0f}%~{C.SOC_MAX*100:.0f}% | "
             f"光伏 {C.PV_TOTAL_MW}MW | 负荷峰值 {C.LOAD_PEAK_MW}MW")
    L.append(f"电价: 谷{C.PRICE['valley']:.2f}/平{C.PRICE['flat']:.2f}/峰{C.PRICE['peak']:.2f}"
             f"元/kWh | 排放因子 {C.EMISSION_GRID_KG_PER_KWH} kgCO2/kWh | "
             f"年生产天数 {WORK_DAYS}")
    L.append("=" * 66)

    for r in results:
        rb, ro = r["r_base"], r["r_opt"]
        L.append("")
        L.append(f"■ {r['meta']['name']} (SOC初值 {r['soc0']}, 重启 {r['restarts']} 次)")
        L.append(f"  · 综合目标 F(归一化): 基线 {rb['F']:.3f} → 优化 {ro['F']:.3f}"
                 f"   (Δ {ro['F']-rb['F']:+.3f})"
                 f"   [经济{ro['econ_norm']:+.2f}/消纳{ro['absorb_norm']:.2f}/削峰{ro['grid_norm']:.2f}]")
        L.append(f"  · 光伏自用率:      基线 {rb['absorb_rate']*100:.1f}% → 优化 {ro['absorb_rate']*100:.1f}%"
                 f"   (弃光 {ro['discard_mwh']:.2f} MWh)")
        L.append(f"  · 峰谷差削减率:    基线 {rb['peak_reduct']*100:.1f}% → 优化 {ro['peak_reduct']*100:.1f}%")
        L.append(f"  · 外购电量(日):   基线 {rb['grid_buy_mwh']:.2f} → 优化 {ro['grid_buy_mwh']:.2f} MWh")
        L.append(f"  · 碳排放(日):     基线 {rb['co2_kg']:.1f} → 优化 {ro['co2_kg']:.1f} kg")
        L.append(f"  · 日净经济收益:   优化 {ro['f_econ']:.0f} 元/日"
                 f"   (含 DR 补贴 {ro['dr_revenue']:.0f} 元/日)")
        L.append(f"  · 日循环次数:      优化 {ro['cycles_day']:.2f}"
                 f"   | 缺供 {ro['shortage_mwh']:.2f} MWh")
        L.append(f"  · SOC 区间:        {ro['detail']['soc'].min()*100:.1f}% ~ "
                 f"{ro['detail']['soc'].max()*100:.1f}% (末值 {ro['detail']['soc_end']*100:.1f}%)")
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
    ap = argparse.ArgumentParser(description="南昌零碳园区储能仿真 v4")
    ap.add_argument("--scene", choices=list(SCENE_META.keys()),
                    help="单场景模式: 指定场景并输出 JSON")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--particles", type=int, default=50)
    ap.add_argument("--iterations", type=int, default=120)
    ap.add_argument("--restarts", type=int, default=None,
                    help="优化重启次数(默认取 params 的 restarts, 否则 3)")
    ap.add_argument("--params", type=str, default=None,
                    help="前端填表导出的 params.json")
    ap.add_argument("--out", type=str, default=None,
                    help="单场景模式下输出 JSON 的路径")
    args = ap.parse_args()

    _params = None
    if args.params:
        with open(args.params, encoding="utf-8") as f:
            _params = json.load(f)

    if args.scene:
        payload = single_scene_payload(args.scene, _params or {},
                                       seed=args.seed,
                                       n_particles=args.particles,
                                       n_iter=args.iterations,
                                       restarts=args.restarts)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text)
        if args.out:
            Path_out = os.path.abspath(args.out)
            with open(Path_out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"[已写入 {Path_out}]")
    else:
        main(_params)
