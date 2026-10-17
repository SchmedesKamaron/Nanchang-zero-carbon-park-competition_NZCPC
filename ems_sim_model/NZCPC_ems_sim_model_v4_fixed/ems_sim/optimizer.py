# -*- coding: utf-8 -*-
"""
optimizer.py — 改进粒子群(IPSO)优化器 + 启发式基线 + 三层调度(修复版)
================================================================
依据外部技术审查意见修复：
  1) SOC 罚函数方向写反 → 修正为"实际越限幅度"正向罚, 并把 soc_end 纳入；
  2) 物理硬约束(额定功率/SOC 上下限)由 environment.step() 保证, 罚函数仅用于引导,
     优化器不再依赖罚函数兜底物理可行性；
  3) 支持可变时域(24h 日前 / 4h·6h 滚动窗口), 窗口间 SOC 状态连续传递；
  4) 优化统一返回 fitness(带罚项), 供多重启选择与公平对比。
"""
import numpy as np
from . import config as C


# ---------------------------------------------------------------------------
# 启发式规则基线
# ---------------------------------------------------------------------------
class HeuristicBaseline:
    """启发式规则基线: 谷段充电、峰段放电(『一充两放』近似)。"""

    def __init__(self, env=None, soc0=0.2):
        self.env = env
        self.soc0 = soc0

    def plan(self, n=24):
        n = int(n)
        p = np.zeros(n)
        for h in C.VALLEY_HOURS[:4]:
            if h < n:
                p[h] = -C.STORAGE_POWER_MW * 0.9
        for h in C.VALLEY_HOURS:
            if h < n and p[h] == 0:
                p[h] = -C.STORAGE_POWER_MW * 0.25
        for h in C.PEAK_HOURS[:3]:
            if h < n:
                p[h] = C.STORAGE_POWER_MW * 0.7
        return p

    def run(self, state=None, soc0=None):
        e = self.env
        sv = soc0 if soc0 is not None else self.soc0
        st = state if state is not None else e.state()
        p = self.plan(len(st["pv"]))
        return e.step(p, sv, st["pv"], st["load"], st["price"],
                      st["dr_cut"], st["dr_fill"])


# ---------------------------------------------------------------------------
# IPSO 优化器
# ---------------------------------------------------------------------------
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

    # ---------- 粒子初始化(方向修正 + 精英注入) ----------
    def _init_particles(self, n, H, P):
        X = self.rng.uniform(-P, P, (n, H))
        # 谷段偏充电(负)、峰段偏放电(正)；仅全日(24h)时按绝对小时引导, 窗口不引导
        if self.init_strategy == "valley_charge" and H >= 24:
            for i in range(n):
                for h in C.VALLEY_HOURS:
                    X[i, h] = min(X[i, h], -P * 0.4)
                for h in C.PEAK_HOURS:
                    X[i, h] = max(X[i, h], P * 0.2)
        # 多精英注入: 前几个粒子以启发式基线为心加小扰动(仅全日)
        if H >= 24:
            try:
                base_plan = HeuristicBaseline(self.env).plan(H)
                k = min(6, n)
                for i in range(k):
                    if i == 0:
                        X[i] = base_plan.copy()
                    else:
                        X[i] = np.clip(base_plan + self.rng.uniform(-P * 0.15, P * 0.15, H),
                                       -P, P)
            except Exception:
                pass
        # 需求响应: 峰段强放电削峰
        if self.env.scene == "demand_response":
            for h in C.PEAK_HOURS[:3]:
                if h < H:
                    X[:, h] = np.maximum(X[:, h], P * 0.6)
        return X

    # ---------- 适应度(罚函数方向已修正) ----------
    def _evaluate_pso(self, x):
        state = self._env_state
        r = self._eval(np.clip(x, -C.STORAGE_POWER_MW, C.STORAGE_POWER_MW), state)
        soc_all = np.append(r["detail"]["soc"], r["detail"]["soc_end"])
        low_violation = np.maximum(C.SOC_MIN - soc_all, 0.0)
        high_violation = np.maximum(soc_all - C.SOC_MAX, 0.0)
        pen_soc = C.PEN_SOC_FACTOR * np.sum(low_violation ** 2 + high_violation ** 2)
        pen_cycle = C.PEN_CYCLE_FACTOR * max(0.0, r["cycles_day"] - C.MAX_CYCLES_DAY)
        fitness = r["F"] - pen_soc - pen_cycle
        return fitness, r

    # ---------- 主优化 ----------
    def optimize(self, state=None):
        e = self.env
        if state is None:
            self._env_state = e.state()
        else:
            self._env_state = state
        H = len(self._env_state["pv"])
        P = C.STORAGE_POWER_MW

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

        max_iter = max(1, self.iter)
        for it in range(self.iter):
            w = self.w_max - (self.w_max - self.w_min) * it / (max_iter - 1)
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
            history.append(float(gbest_f))

        # 最终策略(物理约束已由 step 保证) + 独立可行性验收
        r = self._eval(gbest, self._env_state)
        ok, issues = self.env.validate_plan(r)
        return {"x": gbest, "F": r["F"], "fitness": gbest_f,
                "res": r, "history": history, "valid": ok, "issues": issues}

    # ---------- 实时修正(保留接口; 物理约束已硬保证, 此处仅复评) ----------
    def real_time_correct(self, x, state):
        r = self._eval(x, state)
        return x, r


# ---------------------------------------------------------------------------
# 三层能量调度: 日前(IPSO) + 日内(滚动) + 实时修正
# ---------------------------------------------------------------------------
class ThreeLayerScheduler:
    """三层能量调度。intraday 用可变时域子环境滚动优化, 窗口间 SOC 连续传递。"""

    def __init__(self, env, horizon=4, **opt_kwargs):
        self.env = env
        self.horizon = int(horizon)
        self.opt = IPSOOptimizer(env, **opt_kwargs)

    def day_ahead(self):
        self.opt.soc0 = C.SOC_MIN
        o = self.opt.optimize()
        return {"x": o["x"], "res": o["res"]}

    def intraday(self):
        """日内滚动: 按 horizon 切片, 每窗口独立优化并传递 SOC, 最后整日重仿真。"""
        from . import environment as ENV  # noqa
        state = self.env.state()
        Hf = len(state["pv"]); n = self.horizon
        x = np.zeros(Hf)
        soc_prev = self.opt.soc0
        for s in range(0, Hf, n):
            seg = slice(s, min(s + n, Hf))
            sub = {"pv": state["pv"][seg], "load": state["load"][seg],
                   "price": state["price"][seg],
                   "dr_cut": [h - s for h in state["dr_cut"] if s <= h < s + n],
                   "dr_fill": [h - s for h in state["dr_fill"] if s <= h < s + n]}
            sub_env = ENV.ParkEnvironment(scene=self.env.scene, seed=1000 + s + n,
                                          load_scale=self.env.load_scale,
                                          pv_scale=self.env.pv_scale,
                                          weights=None,
                                          noise=self.env.noise, H=len(sub["pv"]))
            sub_opt = IPSOOptimizer(sub_env, n_particles=30, n_iter=60,
                                    seed=7 + s + n)
            sub_opt.soc0 = soc_prev
            o = sub_opt.optimize(state=sub)
            x[seg] = o["x"]
            soc_prev = o["res"]["detail"]["soc_end"]
        # 整日重仿真, 校验全日 SOC/功率/循环/缺供
        r = self.env.step(x, self.opt.soc0, state["pv"], state["load"],
                          state["price"], state["dr_cut"], state["dr_fill"])
        ok, issues = self.env.validate_plan(r)
        return {"x": x, "res": r, "valid": ok, "issues": issues}
