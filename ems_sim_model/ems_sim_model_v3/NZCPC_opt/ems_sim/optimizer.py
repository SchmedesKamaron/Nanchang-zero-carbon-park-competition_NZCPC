# -*- coding: utf-8 -*-
"""
optimizer.py — 改进粒子群(IPSO)优化器 + 启发式基线 + 三层调度

关键修复:
  1) 初始化方向 bug: 决策变量约定 负=充电, 正=放电。
     原实现把谷段钳成"放电"(正)、峰段钳成"充电"(负), 方向完全反了,
     导致 IPSO 从错误区域起步、收敛到劣于启发式基线的解。已修正。
  2) 提升收敛: 惯性权重线性递减(0.9→0.4) + 精英粒子注入(把启发式基线
     解作为粒子0, 保证优化结果不劣于基线) + 速度边界钳制。
"""
import numpy as np
from . import config as C


class HeuristicBaseline:
    """启发式规则基线: 谷段充电、峰段放电 (『一充两放』近似)。"""

    def __init__(self, env=None, soc0=0.2):
        self.env = env
        self.soc0 = soc0

    # 纯规则功率序列 (不依赖随机状态, 可作 IPSO 精英初解)
    def plan(self, n=24):
        p = np.zeros(n)
        # 谷段前 4h 大功率充电
        for h in C.VALLEY_HOURS[:4]:
            p[h] = -C.STORAGE_POWER_MW * 0.9
        # 其余谷段小功率充电
        for h in C.VALLEY_HOURS:
            if p[h] == 0:
                p[h] = -C.STORAGE_POWER_MW * 0.25
        # 峰段前 3h 放电
        for h in C.PEAK_HOURS[:3]:
            p[h] = C.STORAGE_POWER_MW * 0.7
        return p

    def run(self, state=None, soc0=None):
        e = self.env
        sv = soc0 if soc0 is not None else self.soc0
        st = state if state is not None else e.state()
        p = self.plan(e.H)
        return e.step(p, sv, st["pv"], st["load"], st["price"],
                      st["dr_cut"], st["dr_fill"])


class IPSOOptimizer:
    def __init__(self, env, n_particles=40, n_iter=80, init_strategy="valley_charge",
                 seed=42, w_range=(0.9, 0.4)):
        self.env = env
        self.n = n_particles
        self.iter = n_iter
        self.init_strategy = init_strategy
        self.rng = np.random.default_rng(seed)
        self.w_max, self.w_min = w_range
        self.c1, self.c2 = 1.5, 1.5
        self.soc0 = 0.3
        self._env_state = None

    # ---------- 基础 ----------
    def _eval(self, x, state):
        return self.env.step(x, self.soc0, state["pv"], state["load"],
                             state["price"], state["dr_cut"], state["dr_fill"])

    def _init_state(self):
        self._env_state = self.env.state()

    # ---------- 粒子初始化 (含方向修复 + 多精英解注入) ----------
    def _init_particles(self, n, H, P):
        X = self.rng.uniform(-P, P, (n, H))
        if self.init_strategy == "valley_charge":
            for i in range(n):
                for h in C.VALLEY_HOURS:
                    X[i, h] = min(X[i, h], -P * 0.4)   # 谷段偏充电(负)
                for h in C.PEAK_HOURS:
                    X[i, h] = max(X[i, h], P * 0.2)    # 峰段偏放电(正)
        # 多精英注入: 前几个粒子以启发式基线为心加小扰动, 保证起点不劣于基线
        try:
            base_plan = HeuristicBaseline(self.env).plan(H)
            k = min(6, n)
            for i in range(k):
                if i == 0:
                    X[i] = base_plan.copy()
                else:
                    X[i] = np.clip(base_plan + self.rng.uniform(-P*0.15, P*0.15, H),
                                   -P, P)
        except Exception:
            pass
        # 需求响应: 峰段强放电削峰 (网格权重高, 让削峰策略成为可竞争优解)
        if self.env.scene == "demand_response":
            for h in C.PEAK_HOURS[:3]:
                X[:, h] = np.maximum(X[:, h], P * 0.6)
        return X

    # ---------- 适应度 (含罚函数) ----------
    def _evaluate_pso(self, x):
        state = self._env_state
        r = self._eval(np.clip(x, -C.STORAGE_POWER_MW, C.STORAGE_POWER_MW), state)
        # SOC 越限罚 (step 内已 clamp, 此处兜底)
        soc = r["detail"]["soc"]
        cap = C.STORAGE_CAP_MWH
        pen_soc = 8.0 * (max(0, (soc.min() - C.SOC_MIN) * cap) +
                         max(0, (C.SOC_MAX - soc.max()) * cap))
        pen_cycle = max(0, r["cycles_day"] - C.MAX_CYCLES_DAY) * 1500
        return r["F"] - pen_soc - pen_cycle, r

    # ---------- 主优化 ----------
    def optimize(self, state=None):
        e = self.env
        if state is None:
            self._init_state()
        else:
            self._env_state = state
        H, P = e.H, C.STORAGE_POWER_MW

        X = self._init_particles(self.n, H, P)
        V = self.rng.uniform(-P, P, (self.n, H))
        pbest = X.copy()
        pbest_f = np.full(self.n, -np.inf)
        gbest = X[0].copy()
        gbest_f = -np.inf
        history = []

        for i in range(self.n):
            f, _ = self._evaluate_pso(X[i])
            pbest_f[i] = f
            if f > gbest_f:
                gbest_f, gbest = f, X[i].copy()

        for it in range(self.iter):
            w = self.w_max - (self.w_max - self.w_min) * it / max(1, self.iter - 1)
            for i in range(self.n):
                r1, r2 = self.rng.random(H), self.rng.random(H)
                V[i] = (w * V[i]
                        + self.c1 * r1 * (pbest[i] - X[i])
                        + self.c2 * r2 * (gbest - X[i]))
                V[i] = np.clip(V[i], -P * 0.25, P * 0.25)
                Xnew = np.clip(X[i] + V[i], -P, P)
                fn, _ = self._evaluate_pso(Xnew)
                if fn > pbest_f[i]:
                    pbest[i], pbest_f[i] = Xnew.copy(), fn
                    if fn > gbest_f:
                        gbest_f, gbest = fn, Xnew.copy()
                X[i] = Xnew
            history.append(gbest_f)

        # 实时修正 + 复评
        out, r = self.real_time_correct(gbest, self._env_state)
        return {"x": out, "F": r["F"], "res": r, "history": history,
                "gbest_f_raw": gbest_f}

    # ---------- 实时修正 ----------
    def real_time_correct(self, x, state, soc_limit=0.02):
        out = x.copy()
        e, sv = self.env, self.soc0
        r = e.step(out, sv, state["pv"], state["load"], state["price"],
                   state["dr_cut"], state["dr_fill"])
        soc = r["detail"]["soc"]
        for t in range(e.H):
            if soc[t] < C.SOC_MIN + soc_limit and out[t] > 0:
                out[t] = 0
            if soc[t] > C.SOC_MAX - soc_limit and out[t] < 0:
                out[t] = 0
        r2 = e.step(out, sv, state["pv"], state["load"], state["price"],
                    state["dr_cut"], state["dr_fill"])
        return out, r2


class ThreeLayerScheduler:
    """三层能量调度: 日前(IPSO) + 日内(滚动) + 实时修正。"""

    def __init__(self, env, horizon=4, **opt_kwargs):
        self.env = env
        self.horizon = horizon
        self.opt = IPSOOptimizer(env, **opt_kwargs)

    def day_ahead(self):
        self.opt.soc0 = 0.2
        return self.opt.optimize()

    def intraday(self):
        state = self.env.state()
        n = self.horizon
        x = np.zeros(self.env.H)
        for s in range(0, self.env.H, n):
            sub = {"pv": state["pv"][s:s + n], "load": state["load"][s:s + n],
                   "price": state["price"][s:s + n],
                   "dr_cut": [h - s for h in state["dr_cut"] if s <= h < s + n],
                   "dr_fill": [h - s for h in state["dr_fill"] if s <= h < s + n]}
            self.opt._env_state = sub
            out = self.opt.optimize(state=sub)["x"]
            x[s:s + n] = out
        r = self.env.step(x, self.opt.soc0, state["pv"], state["load"],
                          state["price"], state["dr_cut"], state["dr_fill"])
        return {"x": x, "res": r}
