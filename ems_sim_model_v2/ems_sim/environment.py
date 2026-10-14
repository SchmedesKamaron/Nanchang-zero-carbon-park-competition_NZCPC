# -*- coding: utf-8 -*-
"""
environment.py — 园区"源-网-荷-储"仿真环境模型
实现功率平衡、储能 SOC 动态、经济/消纳/电网/碳排 四类指标测算。
对应文章: 2.1 多目标优化函数 与 4.2 多主体协同调度逻辑
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
        # 生成平滑的时序随机噪声 (模拟气象/生产波动)
        raw = self.rng.normal(0, self.noise, n * 4)
        smoothed = np.convolve(raw, np.ones(4) / 4, mode="same")
        return np.clip(smoothed, -1.5 * self.noise, 1.5 * self.noise)[:n]

    def gen_pv(self):
        """光伏出力曲线 (MW) — 归一化模板 × 装机容量 × 噪声"""
        n = self.H
        pv = np.array(C.PV_PROFILE, dtype=float) * C.PV_TOTAL_MW
        pv = pv * self.pv_scale * (1 + self._smooth_noise(n))
        # 光伏出力场景修正
        if self.scene == "pv_absorb":
            pv = pv * 1.15   # 节假日光伏大发
        return np.clip(pv, 0, C.PV_TOTAL_MW * 1.05)

    def gen_load(self):
        """负荷曲线 (MW)"""
        n = self.H
        load = np.array(C.LOAD_PROFILE, dtype=float) * C.LOAD_PEAK_MW
        load = load * self.load_scale * (1 + self._smooth_noise(n))
        if self.scene in ("pv_absorb", "blackout"):
            load = load * 0.55   # 节假日/停电场景负荷降低
        return np.clip(load, 0, C.LOAD_PEAK_MW * 1.1)

    def gen_price(self):
        """分时电价序列 (元/kWh)"""
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
        """需求响应信号: 削峰时段(峰段且负荷>0.85峰值), 填谷时段(谷段)"""
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
        """
        逐时仿真。p_batt: 储能功率 (+放电→供给负荷, -充电→吸收),
        grid: 可调外部电网出力数组(若无则由功率平衡反解, 优先光伏/储能)
        返回全量指标字典。
        """
        H = self.H
        p_batt = np.array(p_batt, dtype=float)
        soc = np.zeros(H + 1)
        soc[0] = soc0
        p_grid = np.zeros(H)
        p_pvuse = np.zeros(H)   # 光伏被本地利用
        p_pvdiscard = np.zeros(H)  # 弃光
        p_curtail = np.zeros(H)  # 可调负荷削减(需求响应/削峰)
        batt_out_hr = np.zeros(H)   # 逐时实际放电量(MWh)
        batt_in_hr = np.zeros(H)    # 逐时实际充电量(MWh)

        batt_energy_in = 0.0   # MWh
        batt_energy_out = 0.0

        for t in range(H):
            P = pv[t]          # 光伏
            L = load[t]        # 负荷
            # 需求响应削峰: 电网高峰,储能放电外,可调负荷削减部分
            curtail = 0.0
            if t in dr_cut and p_batt[t] < C.DEMAND_RESP_PEAK_CUT_MW:
                curtail = min(2.0, max(0.0, C.DEMAND_RESP_PEAK_CUT_MW - p_batt[t]))
            # 填谷: 谷段储能尽可能充电
            if t in dr_fill:
                # 充电功率提升由 p_batt 负数体现, 此处记录填谷电量
                pass

            # 功率平衡: P_pv + P_sto + P_grid = P_load (+ P_charge)
            # 储能充电时: 充电功率为负, 需额外从光伏或电网吸收
            if p_batt[t] >= 0:
                # 放电
                out = min(p_batt[t], soc[t] * C.STORAGE_CAP_MWH)
                out = min(out, C.STORAGE_POWER_MW)
                # 光伏先供负荷
                used_pv = min(P, L)
                p_pvuse[t] = used_pv
                resid = L - used_pv   # 仍缺的负荷
                from_batt = min(out, max(0, resid))
                # 光伏富余部分 → 弃光或不外送假设
                p_pvdiscard[t] = max(0, P - L)
                need_grid = max(0, resid - from_batt)
                p_grid[t] = need_grid
                soc[t + 1] = soc[t] - from_batt / (C.STORAGE_CAP_MWH) / C.ETA_DISCHARGE
                batt_energy_out += from_batt
                batt_out_hr[t] = from_batt
                p_curtail[t] = curtail
            else:
                # 充电
                ch = -p_batt[t]
                ch = min(ch, C.STORAGE_POWER_MW)
                cap = (C.SOC_MAX - soc[t]) * C.STORAGE_CAP_MWH * C.ETA_CHARGE
                ch = min(ch, max(0, cap))
                # 光伏富余优先给储能充, 不足从电网取
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

        # 结果封装
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

        # 经济: (1)峰谷套利/充放成本 (2)需求响应 (3)调峰
        cost_grid_out = np.sum(np.maximum(p_grid, 0) * price)   # 购电成本(元)
        # 经济账: 逐时按实际电价结算 放电收益(当时电价) - 充电成本(当时电价) - 运维损耗
        batt_out_hr = r.get("batt_out_hr", np.zeros(self.H))
        batt_in_hr = r.get("batt_in_hr", np.zeros(self.H))
        charge_cost = np.sum(batt_in_hr * 1000.0 * price)
        discharge_rev = np.sum(batt_out_hr * 1000.0 * C.ETA_DISCHARGE * price)
        opex = (np.sum(batt_in_hr) + np.sum(batt_out_hr)) * 1000.0 * 0.03   # 运维成本0.03元/kWh
        f_econ = discharge_rev - charge_cost - opex

        # 消纳: 弃光率→消纳增益
        total_pv = pv.sum()
        discard_total = pv_discard.sum()
        absorb_rate = 1 - discard_total / max(total_pv, 1e-9)
        f_absorb = absorb_rate * 100   # 映射到收益尺度

        # 电网: 峰谷差削减率
        net = load - (np.maximum(p_batt,0)) + np.maximum(-p_batt,0)
        base_pvload = load - pv.sum()/24  # 近似
        peak_diff_raw = load.max() - load.min()
        peak_diff_after = np.max(load - np.maximum(p_batt,0)) - np.min(load + np.maximum(-p_batt,0))
        reduct = max(0, (peak_diff_raw - peak_diff_after) / max(peak_diff_raw,1e-9))
        f_grid = reduct * 100

        # 综合目标
        if self.scene == "blackout":
            # 应急模式: 只衡量不断电(负荷缺口)与备用时长
            shortage = np.maximum(0, load - pv - np.maximum(p_batt, 0)).sum()
            F = -shortage
        else:
            F = self.alpha * f_econ + self.beta * f_absorb + self.gamma * f_grid

        # 碳排: 电网购电 × 排放因子
        co2 = np.sum(np.maximum(p_grid,0) * C.EMISSION_GRID_KG_PER_KWH)

        # 日均循环次数(简化)
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
        """经济收益分项测算(元/日的量纲, 供汇总)"""
        d = res["detail"]; price = d["price"]; p_batt = d["p_batt"]
        dr_cut = d["dr_cut"] if "dr_cut" in d else []
        rev_arbitrage = (np.sum(np.maximum(p_batt,0)*price)
                         - np.sum(np.abs(p_batt)*0.02))   # 峰谷套利净收益
        # 需求响应: 峰段放电视为削峰补贴单位换算
        rev_dr = sum(C.SUBSIDY_CUT * max(0, p_batt[h]) for h in range(self.H)
                     if h in getattr(d, "dr_cut", [])) + \
                 sum(C.SUBSIDY_FILL * max(0, -p_batt[h]) for h in range(self.H)
                     if h in getattr(d, "dr_fill", []))
        return {"arbitrage": rev_arbitrage, "dr": rev_dr}
