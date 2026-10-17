# -*- coding: utf-8 -*-
"""
environment.py — 园区"源-网-荷-储"仿真环境模型(修复版)
================================================================
依据外部技术审查意见重写，核心修复：
  1) 放电能量上限按"可用能量"而非"全部 SOC×容量"：(SOC-SOC_MIN)·E_cap·η_d，放电后 SOC 严格 ≥ SOC_MIN；
  2) 充电 SOC 更新统一乘充电效率，功率侧与电池内部能量定义一致；
  3) 功率平衡逐时闭合：光伏→负荷→储能→电网购电/弃电/缺供，均以"实际执行功率"为准；
  4) 停电(blackout)场景：电网交换功率恒为 0，缺供电量被正确统计，孤岛边界真实落实；
  5) 需求响应削峰：curtail 真实作用于净负荷(load_eff)，不再只存结果；
  6) 碳排放单位修正：MWh→kWh 后再乘排放因子(kg/kWh)，消除 1000 倍量级错误；
  7) 经济统一边界：f_econ = 基线购电成本 - 实际购电成本 - 运维 + 实际需求响应补贴，避免重复计利；
  8) 场景缩放只执行一次(scenes/入口负责)，删除 gen_pv/gen_load 内的二次缩放。

统一约定：
  p_batt>0 放电, <0 充电；p_grid>0 从电网购电(停电时为 0)；
  功率单位 MW，能量 MWh，电价 元/kWh，排放 kgCO2/kWh，dt = C.DT_HOURS。
"""
import numpy as np
from . import config as C


class ParkEnvironment:
    """南昌零碳园区仿真环境: 输入 24h 光出/负荷/电价 → 输出多指标。"""

    def __init__(self, scene="economic", seed=42, load_scale=1.0, pv_scale=1.0,
                 weights=None, noise=0.03, H=24):
        self.scene = scene
        self.rng = np.random.default_rng(seed)
        self.load_scale = load_scale
        self.pv_scale = pv_scale
        self.noise = noise
        w = weights or C.WEIGHTS.get(scene, C.WEIGHTS["economic"])
        self.alpha, self.beta, self.gamma = w["alpha"], w["beta"], w["gamma"]
        self.H = int(H)     # 时间步数(默认 24h; 滚动窗口可传 4/6)

    # ---------- 数据生成 ----------
    def _smooth_noise(self, n):
        raw = self.rng.normal(0, self.noise, max(1, n * 4))
        smoothed = np.convolve(raw, np.ones(4) / 4, mode="same")
        return np.clip(smoothed, -1.5 * self.noise, 1.5 * self.noise)[:n]

    def gen_pv(self):
        # 缩放仅由 self.pv_scale 控制(入口/scenes 已设好), 这里不再二次缩放
        n = self.H
        pv = np.array(C.PV_PROFILE, dtype=float) * C.PV_TOTAL_MW
        pv = pv * self.pv_scale * (1 + self._smooth_noise(n))
        return np.clip(pv, 0, C.PV_TOTAL_MW * 1.05)

    def gen_load(self):
        # 缩放仅由 self.load_scale 控制, 删除重复的 *0.55
        n = self.H
        load = np.array(C.LOAD_PROFILE, dtype=float) * C.LOAD_PEAK_MW
        load = load * self.load_scale * (1 + self._smooth_noise(n))
        return np.clip(load, 0, C.LOAD_PEAK_MW * 1.1)

    def gen_price(self):
        price = np.zeros(self.H)
        for h in range(self.H):
            if h in C.VALLEY_HOURS:
                price[h] = C.PRICE["valley"]
            elif h in C.PEAK_HOURS:
                price[h] = C.PRICE["peak"]
            else:
                price[h] = C.PRICE["flat"]
        return price

    # ---------- 状态生成 ----------
    def state(self):
        """一次性生成同一套输入曲线, 供基线与所有优化重启共享(公平对比)。"""
        pv = self.gen_pv()
        load = self.gen_load()
        price = self.gen_price()
        dr_cut, dr_fill = self.gen_dr_signal(load)
        return {"pv": pv, "load": load, "price": price,
                "dr_cut": dr_cut, "dr_fill": dr_fill}

    def gen_dr_signal(self, load=None):
        """需求响应信号基于"已经生成的负荷曲线"推导, 不再独立重复生成负荷。"""
        if load is None:
            load = self.gen_load()
        dr_cut = [h for h in C.PEAK_HOURS
                  if (h < self.H) and load[h] > 0.85 * max(load.max(), 1e-9)]
        dr_fill = [h for h in C.VALLEY_HOURS if h < self.H]
        if self.scene != "demand_response":
            dr_cut, dr_fill = [], []
        return dr_cut, dr_fill

    # ---------- 单日/单窗口仿真步进 ----------
    def step(self, p_batt, soc0, pv, load, price, dr_cut=None, dr_fill=None,
             verbose=False, grid=None):
        """
        逐时仿真。p_batt: 储能请求功率(+放电,-充电)；grid: 电网可用性,
        默认由 scene 决定(blackout 时电网不可用, 可传 grid=False/True 覆盖)。
        返回全量指标字典(detail 含逐时数组)。
        """
        pv    = np.asarray(pv,    dtype=float)
        load  = np.asarray(load,  dtype=float)
        price = np.asarray(price, dtype=float)
        p_batt = np.asarray(p_batt, dtype=float)
        H = len(pv)
        dt = C.DT_HOURS
        cap = C.STORAGE_CAP_MWH
        pmax = C.STORAGE_POWER_MW
        dr_cut  = list(dr_cut) if dr_cut  else []
        dr_fill = list(dr_fill) if dr_fill else []

        # 停电模式：缺省跟随场景，允许显式覆盖
        blackout = self.scene == "blackout"
        if grid is not None:
            blackout = bool(grid) is False

        soc = np.zeros(H + 1)
        soc[0] = soc0
        p_actual   = np.zeros(H)   # 实际执行功率(请求经功率+SOC双重裁剪)
        p_grid     = np.zeros(H)   # 从电网购电(非负; 停电时=0)
        p_pvuse    = np.zeros(H)   # 光伏被利用功率(供负荷+充电)
        p_pvdiscard= np.zeros(H)   # 弃光功率
        p_curtail  = np.zeros(H)   # 需求响应削减的负荷功率
        p_shortage = np.zeros(H)   # 停电时缺供功率
        batt_out_hr= np.zeros(H)   # 放电能量 MWh
        batt_in_hr = np.zeros(H)   # 充电能量 MWh
        batt_energy_in = 0.0
        batt_energy_out = 0.0

        for t in range(H):
            P = max(0.0, float(pv[t]))
            L0 = max(0.0, float(load[t]))

            # 1) 需求响应削峰：真实削减净负荷(仅 DR 场景的申报时段)
            curtail = 0.0
            if t in dr_cut:
                curtail = min(C.DEMAND_RESP_PEAK_CUT_MW,
                              C.DR_CURTAIL_FRAC * L0)
            L_eff = max(0.0, L0 - curtail)
            p_curtail[t] = curtail

            # 2) 光伏优先供负荷
            pv_to_load = min(P, L_eff)
            residual_load = L_eff - pv_to_load
            pv_surplus = P - pv_to_load

            # 3) 储能实际执行功率(受额定功率 + SOC 可用能量双重约束)
            req = float(np.clip(p_batt[t], -pmax, pmax))

            if req > 0.0:                       # 放电(补负荷缺口为主)
                # 放电上限: 可用能量(SOC-SOC_MIN)·E_cap·η_d / dt 折算成 MW
                available_mw = max(0.0, (soc[t] - C.SOC_MIN) * cap
                                   * C.ETA_DISCHARGE / dt)
                discharge = min(req, pmax, available_mw, residual_load)
                soc[t + 1] = soc[t] - discharge * dt / (cap * C.ETA_DISCHARGE)
                p_actual[t] = discharge
                batt_out_hr[t] = discharge * dt
                p_pvuse[t] = pv_to_load
                p_pvdiscard[t] = pv_surplus
                need_grid = residual_load - discharge
                if blackout:
                    p_grid[t] = 0.0
                    p_shortage[t] = max(0.0, residual_load - discharge)
                else:
                    p_grid[t] = need_grid

            elif req < 0.0:                     # 充电
                # 充电上限: 剩余空间(SOC_MAX-SOC)·E_cap / (η_c·dt) 折算成 MW
                room_mw = max(0.0, (C.SOC_MAX - soc[t]) * cap
                              / (C.ETA_CHARGE * dt))
                charge = min(-req, pmax, room_mw)
                if blackout:
                    # 停电孤岛：只能充光伏余电, 严禁从电网取电或加剧缺供
                    charge = min(charge, pv_surplus)
                pv_charge = min(charge, pv_surplus)
                grid_charge = charge - pv_charge
                soc[t + 1] = soc[t] + charge * dt * C.ETA_CHARGE / cap
                p_actual[t] = -charge
                batt_in_hr[t] = charge * dt
                p_pvuse[t] = pv_to_load + pv_charge
                p_pvdiscard[t] = pv_surplus - pv_charge
                if blackout:
                    p_grid[t] = 0.0
                    p_shortage[t] = max(0.0, residual_load)
                else:
                    # 购电 = 光伏不足负荷缺口 + 电网充电
                    p_grid[t] = residual_load + grid_charge

            else:                               # 待机
                soc[t + 1] = soc[t]
                p_actual[t] = 0.0
                p_pvuse[t] = pv_to_load
                p_pvdiscard[t] = pv_surplus
                if blackout:
                    p_grid[t] = 0.0
                    p_shortage[t] = max(0.0, residual_load)
                else:
                    p_grid[t] = residual_load

            batt_energy_in  += batt_in_hr[t]
            batt_energy_out += batt_out_hr[t]

        res = {
            "pv": pv, "load": load, "load_eff": load - p_curtail,
            "price": price, "p_batt": p_batt, "p_actual": p_actual,
            "p_grid": p_grid, "soc": soc[:-1], "soc_end": soc[H],
            "pv_use": p_pvuse, "pv_discard": p_pvdiscard,
            "curtail": p_curtail, "shortage": p_shortage,
            "batt_in_mwh": batt_energy_in, "batt_out_mwh": batt_energy_out,
            "batt_out_hr": batt_out_hr, "batt_in_hr": batt_in_hr,
            "dr_cut": dr_cut, "dr_fill": dr_fill,
            "blackout": blackout,
        }
        m = self._metrics(res)
        m["detail"] = res
        return m

    # ---------- 指标测算(修复版) ----------
    def _metrics(self, r):
        pv = r["pv"]; load = r["load"]; price = r["price"]
        p_grid = r["p_grid"]; p_actual = r["p_actual"]
        pv_use = r["pv_use"]; pv_discard = r["pv_discard"]
        batt_out_hr = r["batt_out_hr"]; batt_in_hr = r["batt_in_hr"]
        bat_in_mwh = r["batt_in_mwh"]; bat_out_mwh = r["batt_out_mwh"]
        dt = C.DT_HOURS

        # ---- 购电与碳排放(单位修正: MWh→kWh 后再乘 kg/kWh) ----
        grid_import_mwh = float(np.maximum(p_grid, 0.0).sum()) * dt
        grid_import_kwh = grid_import_mwh * 1000.0
        grid_cost = np.sum(np.maximum(p_grid, 0.0) * dt * 1000.0 * price)
        co2_kg = grid_import_kwh * C.EMISSION_GRID_KG_PER_KWH

        # ---- 基线购电成本(不用储能的同状态/同分配规则) ----
        baseline_load = np.asarray(r.get("load_eff", load))
        base_grid_mwh = np.maximum(baseline_load - pv, 0.0)
        baseline_grid_cost = np.sum(base_grid_mwh * dt * 1000.0 * price)

        # ---- 运维成本(基于充放吞吐量) ----
        opex = (bat_in_mwh + bat_out_mwh) * 1000.0 * C.OPEX_YUAN_PER_KWH

        # ---- 实际需求响应补贴(仅 DR 场景申报时段, 基于实际响应量) ----
        dr_cut = r.get("dr_cut", []); dr_fill = r.get("dr_fill", [])
        dr_revenue = 0.0
        for h in dr_cut:
            dr_revenue += C.SUBSIDY_CUT * batt_out_hr[h] * 1000.0
        for h in dr_fill:
            dr_revenue += C.SUBSIDY_FILL * batt_in_hr[h] * 1000.0

        # ---- 经济目标(统一边界, 不重复计利) ----
        f_econ = baseline_grid_cost - grid_cost - opex + dr_revenue

        # ---- 光伏消纳 ----
        total_pv = float(pv.sum())
        discard_total = float(pv_discard.sum())
        absorb_rate = 1.0 - discard_total / max(total_pv, 1e-9)
        f_absorb = absorb_rate * 100.0

        # ---- 峰谷差削减率(基于实际执行功率) ----
        net = load - p_actual            # 净负荷 = 负荷 + 充电 - 放电(=负荷 - p_actual)
        peak_diff_raw = float(load.max() - load.min())
        peak_diff_after = float(net.max() - net.min())
        reduct = max(0.0, (peak_diff_raw - peak_diff_after)
                     / max(peak_diff_raw, 1e-9))
        f_grid = reduct * 100.0

        # ---- 综合目标/缺供 ----
        shortage_mwh = float(np.maximum(r["shortage"], 0.0).sum()) * dt
        if r.get("blackout", False):
            F = -shortage_mwh
        else:
            F = self.alpha * f_econ + self.beta * f_absorb + self.gamma * f_grid

        # ---- 循环次数 ----
        cycles = (bat_in_mwh + bat_out_mwh) / 2.0 / C.ENERGY_USEFUL

        return {
            "F": F, "f_econ": f_econ, "f_absorb": f_absorb,
            "absorb_rate": absorb_rate, "discard_mwh": discard_total,
            "f_grid": f_grid, "peak_reduct": reduct,
            "grid_buy_mwh": grid_import_mwh, "grid_buy_kwh": grid_import_kwh,
            "grid_cost": grid_cost, "baseline_grid_cost": baseline_grid_cost,
            "opex": opex, "dr_revenue": dr_revenue,
            "co2_kg": co2_kg, "cycles_day": cycles,
            "shortage_mwh": shortage_mwh,
            "soc_end": r["soc_end"],
        }

    # ---------- 收益拆分(供报告展示) ----------
    def daily_revenue_breakdown(self, res, scene_econ=True):
        d = res["detail"]; price = d["price"]
        bat_out_hr = d["batt_out_hr"]; bat_in_hr = d["batt_in_hr"]
        dr_cut = d.get("dr_cut", []); dr_fill = d.get("dr_fill", [])
        # 售电/自用收益 = 储能放电贡献的负荷电量×电价(近似套利收益)
        rev_arbitrage = (np.sum(bat_out_hr * 1000.0 * price)
                         - np.sum(bat_in_hr * 1000.0 * price)
                         - res["opex"])
        rev_dr = 0.0
        for h in dr_cut:
            rev_dr += C.SUBSIDY_CUT * bat_out_hr[h] * 1000.0
        for h in dr_fill:
            rev_dr += C.SUBSIDY_FILL * bat_in_hr[h] * 1000.0
        return {"arbitrage": rev_arbitrage, "dr": rev_dr}

    # ---------- 可行性验收 ----------
    def validate_plan(self, res):
        """独立验收最终策略是否满足硬约束。返回 (ok, 问题列表)。"""
        issues = []
        d = res["detail"]
        soc_all = np.concatenate([d["soc"], [d["soc_end"]]])
        if (soc_all < C.SOC_MIN - 1e-6).any():
            issues.append("SOC 低于下限 %.1f%%" % (C.SOC_MIN * 100))
        if (soc_all > C.SOC_MAX + 1e-6).any():
            issues.append("SOC 高于上限 %.1f%%" % (C.SOC_MAX * 100))
        if (np.abs(d["p_actual"]) > C.STORAGE_POWER_MW + 1e-6).any():
            issues.append("实际充放电功率超过额定值")
        if res["cycles_day"] > C.MAX_CYCLES_DAY + 1e-6:
            issues.append("日循环次数超限 %.2f" % res["cycles_day"])
        return (len(issues) == 0, issues)
