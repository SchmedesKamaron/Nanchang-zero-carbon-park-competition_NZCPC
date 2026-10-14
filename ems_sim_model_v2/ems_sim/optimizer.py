# -*- coding: utf-8 -*-
"""
optimizer.py — 多目标优化求解器
  · IPSO 粒子群优化: 直接对 24h 储能功率序列寻优
  · 启发式基线: 谷充峰放("一充两放")
  · 三层调度: 日前(24h/1h) → 日内(15min MPC) → 实时(功率修正)
对应文章: 2.2 多目标优化函数与IPSO优化算法; 4.1 三层调度体系
"""
import numpy as np
from . import config as C
from .environment import ParkEnvironment


class IPSOOptimizer:
    """改进粒子群算法求解 24h 储能充放电策略。
    决策变量 x ∈ [-Pmax, Pmax]^24, 负=充电, 正=放电。
    """

    def __init__(self, env, n_particles=60, max_iter=80, w=0.7, c1=1.1, c2=1.1,
                 seed=7):
        self.env = env
        self.n = n_particles
        self.iters = max_iter
        self.w, self.c1, self.c2 = w, c1, c2
        self.rng = np.random.default_rng(seed)
        self.H = env.H

    def _power_balance_sanity(self, x):
        x = np.clip(x, -C.STORAGE_POWER_MW, C.STORAGE_POWER_MW)
        return x

    def _evaluate(self, x):
        env = self.env
        st = self._env_state
        r = env.step(x, 0.5, st["pv"], st["load"], st["price"],
                     st["dr_cut"], st["dr_fill"])
        return r["F"], r

    def _init_state(self):
        self._env_state = self._sample_state()

    def _sample_state(self):
        e = self.env
        return {
            "pv": e.gen_pv(), "load": e.gen_load(), "price": e.gen_price(),
            "dr_cut": e.gen_dr_signal()[0], "dr_fill": e.gen_dr_signal()[1],
        }

    def optimize(self, state=None, init_strategy="valley_charge"):
        """返回 (最优功率序列, 最优目标值, 收敛历史)"""
        if state is None:
            self._init_state()
        else:
            self._env_state = state

        # 随机初始化粒子 (偏向谷段充电的可行初解有助于收敛)
        X = self.rng.uniform(-C.STORAGE_POWER_MW, C.STORAGE_POWER_MW,
                             (self.n, self.H))
        if init_strategy == "valley_charge":
            for i in range(self.n):
                for h in C.VALLEY_HOURS:
                    X[i, h] = max(X[i, h], C.STORAGE_POWER_MW * 0.4)  # 谷段偏充电
                for h in C.PEAK_HOURS:
                    X[i, h] = min(X[i, h], -C.STORAGE_POWER_MW * 0.2)  # 峰段偏放电

        X = self._power_balance_sanity(X)
        V = self.rng.uniform(-0.5, 0.5, X.shape)
        pbest = X.copy()
        pbest_f = np.full(self.n, -np.inf)
        gbest_f = -np.inf
        gbest = X[0].copy()
        history = []

        # 逐粒子初始评估 + 罚函数(超SOC约束罚)
        for i in range(self.n):
            # SOC 可行性罚: 累加超出
            f, _ = self._evaluate_pso(X[i])
            pbest_f[i] = f
            if f > gbest_f:
                gbest_f, gbest = f, X[i].copy()

        hist_best = gbest_f
        for it in range(self.iters):
            for i in range(self.n):
                r1 = self.rng.random(self.H)
                r2 = self.rng.random(self.H)
                V[i] = (self.w * V[i] + self.c1 * r1 * (pbest[i] - X[i])
                        + self.c2 * r2 * (gbest - X[i]))
                V[i] = np.clip(V[i], -C.STORAGE_POWER_MW, C.STORAGE_POWER_MW)
                Xnew = self._power_balance_sanity(X[i] + V[i])
                fn, _ = self._evaluate_pso(Xnew)
                if fn > pbest_f[i]:
                    pbest[i], pbest_f[i] = Xnew.copy(), fn
                    if fn > gbest_f:
                        gbest_f, gbest = fn, Xnew.copy()
                X[i] = Xnew
            history.append(gbest_f)

        # 用最优解再评估一次返回完整指标
        r = self._evaluate_pso_full(gbest)
        return gbest, r["F"], history, r

    def _evaluate_pso(self, x):
        e = self.env
        st = self._env_state
        r = e.step(x, 0.5, st["pv"], st["load"], st["price"],
                   st["dr_cut"], st["dr_fill"])
        # SOC 约束罚项
        soc = r["detail"]["soc"]
        pen_soc = 8.0 * (max(0, (soc.min() - C.SOC_MIN) * C.STORAGE_CAP_MWH)
                         + max(0, (C.SOC_MAX - soc.max()) * C.STORAGE_CAP_MWH))
        # 循环次数惩罚: 罚到 ≤2 次/日 (对应文章“一充两放”)
        # 用充放电符号翻转次数近似估计循环数
        sign = np.sign(x)
        flips = int(np.sum(np.abs(np.diff(sign)) > 0))
        est_cycles = flips / 2.0
        pen_cycles = max(0, est_cycles - C.MAX_CYCLES_DAY) * 60.0
        return r["F"] - pen_soc - pen_cycles, r

    def _evaluate_pso_full(self, x):
        e = self.env
        st = self._env_state
        return e.step(x, 0.5, st["pv"], st["load"], st["price"],
                      st["dr_cut"], st["dr_fill"])


class HeuristicBaseline:
    """启发式基线: 谷段满充 / 峰段放 / 两充两放规则"""
    def run(self, env, state):
        H = env.H
        p = np.zeros(H)
        # 策略: 谷段(0-7)充电到上限, 早峰(16-18)放电, 晚谷(0-4)再小充
        for h in C.VALLEY_HOURS[:4]:
            p[h] = -C.STORAGE_POWER_MW * 0.9      # 深夜谷段 大功率充电
        for h in C.VALLEY_HOURS:
            if p[h] == 0:
                p[h] = -C.STORAGE_POWER_MW * 0.25 # 谷段其余小充
        for h in C.PEAK_HOURS[:3]:
            p[h] = C.STORAGE_POWER_MW * 0.7        # 峰段放电
        r = env.step(p, 0.5, state["pv"], state["load"], state["price"],
                     state["dr_cut"], state["dr_fill"])
        return p, r["F"], r


# ---------- 三层调度 (日前-日内-实时) ----------
class ThreeLayerScheduler:
    """对应 4.1 节:日前(逐时MPC优化) → 日内(15min滚动修正) → 实时(秒级功率修正)。

    为仿真清晰起见,这里合并为: 日前(IPSO)给出基准计划, 日内做滚动重新优化,
    实时用一个简单功率跟踪器修正SOC越限。
    """

    def __init__(self, env):
        self.env = env

    def day_ahead(self, state):
        """日前调度: IPSO 逐时优化 (24×1h)"""
        opt = IPSOOptimizer(self.env)
        x, F, hist, r = opt.optimize(state=state)
        return x, r

    def intraday(self, state, horizon=4, step=1):
        """日内滚动: 每 step 小时用未来 horizon 小时内做局部修正"""
        x = np.zeros(self.env.H)
        for start in range(0, self.env.H, step):
            sub_state = {k: (v[start:start + horizon] if isinstance(v, np.ndarray)
                             else v) for k, v in state.items()}
            # 在子窗口上用 IPSO 微调(迭代次数少)
            e_sub = ParkEnvironment(self.env.scene)
            sub = {k: (np.array(v, dtype=float) if isinstance(v, np.ndarray) else v)
                   for k, v in sub_state.items()}
            opt = IPSOOptimizer(e_sub, n_particles=25, max_iter=20)
            opt._env_state = sub
            xs, _, _, _ = opt.optimize(state=sub)
            end = min(start + horizon, self.env.H)
            x[start:start + 1] = xs[0]   # 只采用首步
        r = self.env.step(x, 0.5, state["pv"], state["load"], state["price"],
                          state["dr_cut"], state["dr_fill"])
        return x, r

    def real_time_correct(self, x, state, soc_limit=0.02):
        """实时修正: 检测SOC越限, 对越界时段的功率做钳制"""
        out = x.copy()
        e = self.env
        r = e.step(out, 0.5, state["pv"], state["load"], state["price"],
                   state["dr_cut"], state["dr_fill"])
        soc = r["detail"]["soc"]
        for t in range(self.env.H):
            if soc[t] < C.SOC_MIN + soc_limit and out[t] > 0:
                out[t] = 0           # SOC过低禁止放电
            if soc[t] > C.SOC_MAX - soc_limit and out[t] < 0:
                out[t] = 0           # SOC过高禁止充电
        r2 = e.step(out, 0.5, state["pv"], state["load"], state["price"],
                    state["dr_cut"], state["dr_fill"])
        return out, r2
