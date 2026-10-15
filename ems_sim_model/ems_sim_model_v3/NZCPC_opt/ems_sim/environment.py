# -*- coding: utf-8 -*-
"""
environment.py — 园区"源-网-荷-储"仿真环境模型
实现功率平衡、储能 SOC 动态、经济/消纳/电网/碳排 四类指标测算。
"""
import numpy as np
from . import config as C


class ParkEnvironment:
    """南昌零碳园区仿真环境: 输入 24h 光出/负荷/电价 → 输出多指标。"""

    def __init__(self, scene="economic", seed=42, load_scale=1.0, pv_scale=1.0,
                 weights=None, noise=0.03):
        self.scene = scene
        self.rng = np.random.default_rng(seed)
        self.load_scale = load_scale
        self.pv_scale = pv_scale
        self.noise = noise
        w = weights or C.WEIGHTS.get(scene, C.WEIGHTS["economic"])
        self.alpha, self.beta, self.gamma = w["alpha"], w["beta"], w["gamma"]
        self.H = 24  # 时间步(小时)

    # ---------- 数据生成 ----------
    def _smooth_noise(self, n):
        raw = self.rng.normal(0, self.noise, n * 4)
        smoothed = np.convolve(raw, np.ones(4) / 4, mode="same")
        return np.clip(smoothed, -1.5 * self.noise, 1.5 * self.noise)[:n]

    def gen_pv(self):
        n = self.H
        pv = np.array(C.PV_PROFILE, dtype=float) * C.PV_TOTAL_MW
        pv = pv * self.pv_scale * (1 + self._smooth_noise(n))
        if self.scene == "pv_absorb":
            pv = pv * 1.15
        return np.clip(pv, 0, C.PV_TOTAL_MW * 1.05)

    def gen_load(self):
        n = self.H
        load = np.array(C.LOAD_PROFILE, dtype=float) * C.LOAD_PEAK_MW
        load = load * self.load_scale * (1 + self._smooth_noise(n))
        if self.scene in ("pv_absorb", "blackout"):
            load = load * 0.55
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

    def gen_dr_signal(self):
        load = self.gen_load()
        dr_cut = [h for h in C.PEAK_HOURS if load[h] > 0.85 * load.max()]
        dr_fill = [h for h in C.VALLEY_HOURS]
        if self.scene != "demand_response":
            dr_cut, dr_fill = [], []
        return dr_cut, dr_fill

    # ---------- 状态生成 ----------
    def state(self):
        pv = self.gen_pv()
        load = self.gen_load()
        price = self.gen_price()
        dr_cut, dr_fill = self.gen_dr_signal()
        return {"pv": pv, "load": load, "price": price,
                "dr_cut": dr_cut, "dr_fill": dr_fill}

    # ---------- 单日仿真步进 ----------
    def step(self, p_batt, soc0, pv, load, price, dr_cut, dr_fill,
             verbose=False, grid=None):
        """逐时仿真。p_batt: 储能功率 (+放电, -充电)。返回全量指标字典。"""
        H = self.H
        p_batt = np.array(p_batt, dtype=float)
        soc = np.zeros(H + 1)
        soc[0] = soc0
        p_grid = np.zeros(H)
        p_pvuse = np.zeros(H)
        p_pvdiscard = np.zeros(H)
        p_curtail = np.zeros(H)
        batt_out_hr = np.zeros(H)
        batt_in_hr = np.zeros(H)

        batt_energy_in = 0.0
        batt_energy_out = 0.0

        for t in range(H):
            P = pv[t]
            L = load[t]
            curtail = 0.0
            if t in dr_cut and p_batt[t] < C.DEMAND_RESP_PEAK_CUT_MW:
                curtail = min(2.0, max(0.0, C.DEMAND_RESP_PEAK_CUT_MW - p_batt[t]))

            if p_batt[t] >= 0:
                out = min(p_batt[t], soc[t] * C.STORAGE_CAP_MWH)
                out = min(out, C.STORAGE_POWER_MW)
                used_pv = min(P, L)
                p_pvuse[t] = used_pv
                resid = L - used_pv
                from_batt = min(out, max(0, resid))
                p_pvdiscard[t] = max(0, P - L)
                need_grid = max(0, resid - from_batt)
                p_grid[t] = need_grid
                soc[t + 1] = soc[t] - from_batt / (C.STORAGE_CAP_MWH) / C.ETA_DISCHARGE
                batt_energy_out += from_batt
                batt_out_hr[t] = from_batt
                p_curtail[t] = curtail
            else:
                ch = -p_batt[t]
                ch = min(ch, C.STORAGE_POWER_MW)
                cap = (C.SOC_MAX - soc[t]) * C.STORAGE_CAP_MWH * C.ETA_CHARGE
                ch = min(ch, max(0, cap))
                pv_surplus = max(0, P - L)
                p_pvuse[t] = min(P, L)
                from_pv = min(pv_surplus, ch)
                p_pvdiscard[t] = max(0, pv_surplus - from_pv)
                from_grid_charge = max(0, ch - from_pv)
                p_grid[t] = from_grid_charge
                soc[t + 1] = soc[t] + ch / C.STORAGE_CAP_MWH
                batt_energy_in += ch
                batt_in_hr[t] = ch
                p_curtail[t] = 0.0

        res = {
            "pv": pv, "load": load, "price": price,
            "p_batt": p_batt, "p_grid": p_grid, "soc": soc[:-1], "soc_end": soc[H],
            "pv_use": p_pvuse, "pv_discard": p_pvdiscard,
            "curtail": p_curtail,
            "batt_in_mwh": batt_energy_in, "batt_out_mwh": batt_energy_out,
            "batt_out_hr": batt_out_hr, "batt_in_hr": batt_in_hr,
        }
        return self._metrics(res)

    # ---------- 指标测算 ----------
    def _metrics(self, r):
        pv = r["pv"]; load = r["load"]; price = r["price"]
        p_grid = r["p_grid"]; p_batt = r["p_batt"]
        pv_use = r["pv_use"]; pv_discard = r["pv_discard"]

        cost_grid_out = np.sum(np.maximum(p_grid, 0) * price)
        batt_out_hr = r.get("batt_out_hr", np.zeros(self.H))
        batt_in_hr = r.get("batt_in_hr", np.zeros(self.H))
        charge_cost = np.sum(batt_in_hr * 1000.0 * price)
        # 放电收益按实际供负荷电量×电价计; 效率损耗已体现在 SOC 消耗(除以 ETA)中, 不重复乘
        discharge_rev = np.sum(batt_out_hr * 1000.0 * price)
        opex = (np.sum(batt_in_hr) + np.sum(batt_out_hr)) * 1000.0 * 0.03
        f_econ = discharge_rev - charge_cost - opex

        total_pv = pv.sum()
        discard_total = pv_discard.sum()
        absorb_rate = 1 - discard_total / max(total_pv, 1e-9)
        f_absorb = absorb_rate * 100

        net = load - (np.maximum(p_batt,0)) + np.maximum(-p_batt,0)
        base_pvload = load - pv.sum()/24
        peak_diff_raw = load.max() - load.min()
        peak_diff_after = np.max(load - np.maximum(p_batt,0)) - np.min(load + np.maximum(-p_batt,0))
        reduct = max(0, (peak_diff_raw - peak_diff_after) / max(peak_diff_raw,1e-9))
        f_grid = reduct * 100

        if self.scene == "blackout":
            shortage = np.maximum(0, load - pv - np.maximum(p_batt, 0)).sum()
            F = -shortage
        else:
            F = self.alpha * f_econ + self.beta * f_absorb + self.gamma * f_grid

        co2 = np.sum(np.maximum(p_grid,0) * C.EMISSION_GRID_KG_PER_KWH)

        cycles = (r["batt_in_mwh"] + r["batt_out_mwh"]) / 2 / C.ENERGY_USEFUL

        return {
            "F": F, "f_econ": f_econ, "f_absorb": f_absorb,
            "absorb_rate": absorb_rate, "discard_mwh": discard_total,
            "f_grid": f_grid, "peak_reduct": reduct,
            "grid_buy_mwh": np.maximum(p_grid,0).sum(),
            "co2_kg": co2, "cycles_day": cycles,
            "soc_end": r["soc_end"], "detail": r,
        }

    def daily_revenue_breakdown(self, res, scene_econ=True):
        d = res["detail"]; price = d["price"]; p_batt = d["p_batt"]
        dr_cut = d["dr_cut"] if "dr_cut" in d else []
        rev_arbitrage = (np.sum(np.maximum(p_batt,0)*price)
                         - np.sum(np.abs(p_batt)*0.02))
        rev_dr = sum(C.SUBSIDY_CUT * max(0, p_batt[h]) for h in range(self.H)
                     if h in getattr(d, "dr_cut", [])) + \
                 sum(C.SUBSIDY_FILL * max(0, -p_batt[h]) for h in range(self.H)
                     if h in getattr(d, "dr_fill", []))
        return {"arbitrage": rev_arbitrage, "dr": rev_dr}
