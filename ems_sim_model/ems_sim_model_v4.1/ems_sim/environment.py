# -*- coding: utf-8 -*-
"""
environment.py — 园区"源-网-荷-储"仿真环境模型 (v4)

依据《v3_Optimized_Version_审查意见.md》重写，核心修复：
  1) 放电能量上限按"可用能量"而非"全部 SOC×容量"：
       可用能量 = (SOC_t - SOC_MIN) · E_cap · η_dis，放电后 SOC 严格 ≥ SOC_MIN；
  2) 充电 SOC 更新统一乘充电效率，功率侧与电池内部能量定义一致：
       SOC_{t+1} = SOC_t + P_ch·dt·η_ch / E_cap  (充电)
       SOC_{t+1} = SOC_t - P_dis·dt / (E_cap·η_dis)  (放电)
  3) 逐时功率平衡闭合：光伏→负荷→储能→电网购电/弃光/缺供，均以"实际执行功率"为准，
     不直接使用优化器请求功率；不对 SOC 做 clip 强裁，而是先限功率再更新 SOC；
  4) 停电(blackout)场景：电网交换功率恒为 0，缺供电量被正确统计，孤岛边界真实落实；
  5) 需求响应削峰：curtail 真实作用于净负荷(load_eff)，参与功率平衡，不再只存结果；
  6) 碳排放单位修正：MWh→kWh 后再乘排放因子(kg/kWh)，消除 1000 倍量级错误；
  7) 经济统一边界：f_econ = 基线购电成本 - 实际购电成本 - 运维 + 实际需求响应补贴，
     避免同一份电量重复计利；
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

    # ------------------------------------------------------------------
    # 数据生成
    # ------------------------------------------------------------------
    def _smooth_noise(self, n):
        raw = self.rng.normal(0, self.noise, max(1, n * 4))
        smoothed = np.convolve(raw, np.ones(4) / 4, mode="same")
        return np.clip(smoothed, -1.5 * self.noise, 1.5 * self.noise)[:n]

    def gen_pv(self):
        # 缩放仅由 self.pv_scale 控制(入口/scenes 已设好)，这里不再二次缩放。
        # 用 np.resize 支持任意时域(滚动窗口 4/6h)；裁剪上限考虑 pv_scale，避免被截断。
        n = self.H
        profile = np.resize(np.asarray(C.PV_PROFILE, dtype=float), n)
        pv = profile * C.PV_TOTAL_MW * self.pv_scale * (1 + self._smooth_noise(n))
        return np.clip(pv, 0, C.PV_TOTAL_MW * max(1.0, self.pv_scale) * 1.05)

    def gen_load(self):
        # 缩放仅由 self.load_scale 控制，删除重复的 *0.55
        n = self.H
        profile = np.resize(np.asarray(C.LOAD_PROFILE, dtype=float), n)
        load = profile * C.LOAD_PEAK_MW * self.load_scale * (1 + self._smooth_noise(n))
        return np.clip(load, 0, C.LOAD_PEAK_MW * max(1.0, self.load_scale) * 1.1)

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

    def gen_dr_signal(self, load=None):
        """需求响应信号基于"已经生成的负荷曲线"推导，不再独立重复生成负荷。"""
        if load is None:
            load = self.gen_load()
        dr_cut = [h for h in C.PEAK_HOURS
                  if (h < self.H) and load[h] > 0.85 * max(load.max(), 1e-9)]
        dr_fill = [h for h in C.VALLEY_HOURS if h < self.H]
        if self.scene != "demand_response":
            dr_cut, dr_fill = [], []
        return dr_cut, dr_fill

    # ------------------------------------------------------------------
    # 状态生成（一次性生成同一套输入曲线，供基线与所有优化重启共享）
    # ------------------------------------------------------------------
    def state(self):
        pv = self.gen_pv()
        load = self.gen_load()
        price = self.gen_price()
        dr_cut, dr_fill = self.gen_dr_signal(load)
        return {"pv": pv, "load": load, "price": price,
                "dr_cut": dr_cut, "dr_fill": dr_fill}

    # ------------------------------------------------------------------
    # 单日 / 单窗口仿真步进
    # ------------------------------------------------------------------
    def step(self, p_batt, soc0, pv, load, price, dr_cut=None, dr_fill=None,
             verbose=False, grid=None):
        """
        逐时仿真。p_batt: 储能请求功率(+放电, -充电)；grid: 电网可用性，
        默认由 scene 决定(blackout 时电网不可用，可传 grid=False/True 覆盖)。
        返回全量指标字典(detail 含逐时数组)。
        """
        pv     = np.asarray(pv,     dtype=float)
        load   = np.asarray(load,   dtype=float)
        price  = np.asarray(price,  dtype=float)
        p_batt = np.asarray(p_batt, dtype=float)
        H = len(pv)
        dt = C.DT_HOURS
        cap = C.STORAGE_CAP_MWH
        pmax = C.STORAGE_POWER_MW
        dr_cut  = list(dr_cut) if dr_cut  else []
        dr_fill = list(dr_fill) if dr_fill else []

        # ---- fail-loud 入参校验(非法输入显式报错, 不静默计算) ----
        if H == 0 or len(load) != H or len(price) != H:
            raise ValueError("pv, load, price 必须是非空且等长的数组")
        if len(p_batt) != H:
            raise ValueError(f"p_batt 长度 {len(p_batt)} 与时域 {H} 不匹配")
        if not (np.all(np.isfinite(pv)) and np.all(np.isfinite(load))
                and np.all(np.isfinite(price)) and np.all(np.isfinite(p_batt))):
            raise ValueError("输入数组只能包含有限数值")
        if np.any(pv < 0) or np.any(load < 0) or np.any(price < 0):
            raise ValueError("pv/load/price 不能为负")
        if not (C.SOC_MIN - 1e-9 <= soc0 <= C.SOC_MAX + 1e-9):
            raise ValueError(f"初始 SOC({soc0}) 必须在配置的 SOC 上下限内")

        # 停电模式：缺省跟随场景，允许显式覆盖
        blackout = (self.scene == "blackout")
        if grid is not None:
            blackout = bool(grid) is False

        soc = np.zeros(H + 1)
        soc[0] = float(soc0)
        p_actual    = np.zeros(H)   # 实际执行功率(请求经功率+SOC双重裁剪)
        p_grid      = np.zeros(H)   # 从电网购电(非负; 停电时=0)
        p_pvuse     = np.zeros(H)   # 光伏被利用功率(供负荷+充电)
        p_pvdiscard = np.zeros(H)   # 弃光功率
        p_curtail   = np.zeros(H)   # 需求响应削减的负荷功率
        p_shortage  = np.zeros(H)   # 停电时缺供功率
        batt_out_hr = np.zeros(H)   # 放电能量 MWh
        batt_in_hr  = np.zeros(H)   # 充电能量 MWh
        batt_energy_in  = 0.0
        batt_energy_out = 0.0

        for t in range(H):
            P = max(0.0, float(pv[t]))
            L0 = max(0.0, float(load[t]))

            # 1) 需求响应削峰：真实削减净负荷(仅 DR 场景申报时段)
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
                # 放电只用于满足负荷缺口，不向电网反送(自用优先)
                discharge = min(req, pmax, available_mw, residual_load)
                soc[t + 1] = soc[t] - discharge * dt / (cap * C.ETA_DISCHARGE)
                p_actual[t] = discharge
                batt_out_hr[t] = discharge * dt
                p_pvuse[t] = pv_to_load
                p_pvdiscard[t] = pv_surplus
                if blackout:
                    p_grid[t] = 0.0
                    p_shortage[t] = max(0.0, residual_load - discharge)
                else:
                    p_grid[t] = residual_load - discharge

            elif req < 0.0:                     # 充电
                # 充电上限: 剩余空间(SOC_MAX-SOC)·E_cap / (η_c·dt) 折算成 MW
                room_mw = max(0.0, (C.SOC_MAX - soc[t]) * cap
                              / (C.ETA_CHARGE * dt))
                charge = min(-req, pmax, room_mw)
                if blackout:
                    # 停电孤岛：只能充光伏余电，严禁从电网取电或加剧缺供
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

        # 先限功率再更新 SOC，物理上不应越界；此处断言兜底(而非 clip 掩盖)
        soc_all = np.append(soc[:-1], soc[H])
        assert soc_all.min() >= C.SOC_MIN - 1e-9, "SOC 跌破下限(物理约束被破坏)"
        assert soc_all.max() <= C.SOC_MAX + 1e-9, "SOC 突破上限(物理约束被破坏)"

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

    # ------------------------------------------------------------------
    # 指标测算(修复版)
    # ------------------------------------------------------------------
    def _metrics(self, r):
        pv = r["pv"]; load = r["load"]; price = r["price"]
        p_grid = r["p_grid"]; p_actual = r["p_actual"]
        pv_discard = r["pv_discard"]
        batt_out_hr = r["batt_out_hr"]; batt_in_hr = r["batt_in_hr"]
        bat_in_mwh = r["batt_in_mwh"]; bat_out_mwh = r["batt_out_mwh"]
        load_eff = r["load_eff"]
        dt = C.DT_HOURS

        # ---- 购电与碳排放(单位修正: MWh→kWh 后再乘 kg/kWh) ----
        grid_import_mwh = float(np.maximum(p_grid, 0.0).sum()) * dt
        grid_import_kwh = grid_import_mwh * 1000.0
        grid_cost = np.sum(np.maximum(p_grid, 0.0) * dt * 1000.0 * price)
        co2_kg = grid_import_kwh * C.EMISSION_GRID_KG_PER_KWH

        # ---- 基线购电成本(同状态/同分配规则、不用储能的电网需承担净负荷) ----
        base_grid_mwh = np.maximum(load_eff - pv, 0.0)
        baseline_grid_cost = np.sum(base_grid_mwh * dt * 1000.0 * price)

        # ---- 运维成本(基于充放吞吐量) ----
        opex = (bat_in_mwh + bat_out_mwh) * 1000.0 * C.OPEX_YUAN_PER_KWH

        # ---- 实际需求响应补贴(仅 DR 场景申报时段, 基于实际响应量) ----
        dr_cut = r.get("dr_cut", []); dr_fill = r.get("dr_fill", [])
        dr_revenue = 0.0
        for h in dr_cut:
            if h < len(batt_out_hr):
                dr_revenue += C.SUBSIDY_CUT * batt_out_hr[h] * 1000.0
        for h in dr_fill:
            if h < len(batt_in_hr):
                dr_revenue += C.SUBSIDY_FILL * batt_in_hr[h] * 1000.0

        # ---- 经济目标(统一边界, 不重复计利) ----
        f_econ = baseline_grid_cost - grid_cost - opex + dr_revenue

        # ---- 光伏消纳 ----
        total_pv = float(pv.sum())
        discard_total = float(pv_discard.sum())
        absorb_rate = 1.0 - discard_total / max(total_pv, 1e-9)
        f_absorb = absorb_rate * 100.0

        # ---- 峰谷差削减率(基于真实净负荷=电网需承担的功率) ----
        # 基线净负荷(无储能): 负荷削峰后 - 光伏供给负荷
        pv_for_load = np.minimum(load_eff, pv)
        baseline_net = load_eff - pv_for_load          # 无储能时电网净承担
        peak_diff_raw = float(baseline_net.max() - baseline_net.min())
        peak_diff_after = float(p_grid.max() - p_grid.min())  # 有储能+光伏后的电网交换
        reduct = max(0.0, (peak_diff_raw - peak_diff_after)
                     / max(peak_diff_raw, 1e-9))
        f_grid = reduct * 100.0

        # ---- 综合目标 / 缺供（目标归一化，权重可解释）----
        # 审查(八·P1)：经济收益为元/日(千元级)，消纳/削峰为 0-100 分，量纲不同
        # 导致经济权重实际压倒其他目标。这里统一到 [-1,1]/[0,1] 量纲再加权：
        #   econ_norm = f_econ / 基线购电成本   （储能相对基线的"省电费+补贴"率）
        #   absorb_norm = 光伏自用率 0-1
        #   grid_norm   = 峰谷差削减率 0-1
        shortage_mwh = float(np.maximum(r["shortage"], 0.0).sum()) * dt
        if r.get("blackout", False):
            F = -shortage_mwh
            econ_norm = absorb_norm = grid_norm = 0.0
        else:
            econ_ref = max(baseline_grid_cost, 1.0)
            econ_norm = float(f_econ / econ_ref)             # 约 [-1, 1]
            absorb_norm = float(min(max(absorb_rate, 0.0), 1.0))
            grid_norm = float(min(max(reduct, 0.0), 1.0))
            F = (self.alpha * econ_norm
                 + self.beta * absorb_norm
                 + self.gamma * grid_norm)

        # ---- 循环次数 ----
        cycles = (bat_in_mwh + bat_out_mwh) / 2.0 / max(C.ENERGY_USEFUL, 1e-9)

        return {
            "F": F, "f_econ": f_econ, "f_absorb": f_absorb,
            "absorb_rate": absorb_rate, "discard_mwh": discard_total,
            "f_grid": f_grid, "peak_reduct": reduct,
            "econ_norm": econ_norm, "absorb_norm": absorb_norm,
            "grid_norm": grid_norm,
            "grid_buy_mwh": grid_import_mwh, "grid_buy_kwh": grid_import_kwh,
            "grid_cost": grid_cost, "baseline_grid_cost": baseline_grid_cost,
            "opex": opex, "dr_revenue": dr_revenue,
            "co2_kg": co2_kg, "cycles_day": cycles,
            "shortage_mwh": shortage_mwh,
            "soc_end": r["soc_end"],
        }

    # ------------------------------------------------------------------
    # 收益拆分(供报告展示)
    # ------------------------------------------------------------------
    def daily_revenue_breakdown(self, res, scene_econ=True):
        d = res["detail"]
        price = d["price"]
        bat_out_hr = d["batt_out_hr"]; bat_in_hr = d["batt_in_hr"]
        dr_cut = d.get("dr_cut", []); dr_fill = d.get("dr_fill", [])
        # 套利收益 = 放电贡献负荷的电量×电价 - 充电电量×电价 - 运维
        rev_arbitrage = (np.sum(bat_out_hr * 1000.0 * price)
                         - np.sum(bat_in_hr * 1000.0 * price)
                         - res["opex"])
        rev_dr = 0.0
        for h in dr_cut:
            rev_dr += C.SUBSIDY_CUT * bat_out_hr[h] * 1000.0
        for h in dr_fill:
            rev_dr += C.SUBSIDY_FILL * bat_in_hr[h] * 1000.0
        return {"arbitrage": rev_arbitrage, "dr": rev_dr}

    # ------------------------------------------------------------------
    # 可行性验收
    # ------------------------------------------------------------------
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
        if d.get("blackout", False) and (d["p_grid"] > 1e-6).any():
            issues.append("停电场景出现电网购电")
        return (len(issues) == 0, issues)
