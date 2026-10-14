# -*- coding: utf-8 -*-
"""
cli.py — 命令行入口。安装本包后可直接运行:  ems-sim
自动创建 out/ 目录输出 调度对比图.png 与 仿真结果报告.txt
"""
import os
import sys

# 允许从源码目录直接运行 python -m ems_sim.cli
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ems_sim.environment import ParkEnvironment
from ems_sim.optimizer import IPSOOptimizer, HeuristicBaseline
from ems_sim.scenes import SCENE_META, SCENE_SOC0
from ems_sim import config as C


def run_simulation(out_dir=None):
    """运行完整仿真, 返回 (报告文本, 图路径)。"""
    import numpy as np
    import datetime

    out_dir = out_dir or os.path.join(os.getcwd(), "out")
    os.makedirs(out_dir, exist_ok=True)

    scenes_to_run = ["economic", "pv_absorb", "demand_response", "blackout"]
    results = []
    for s in scenes_to_run:
        meta = SCENE_META[s]
        env = ParkEnvironment(scene=s, load_scale=meta["load_scale"],
                              pv_scale=meta["pv_scale"])
        state = env.state()
        soc0 = SCENE_SOC0[s]
        # 启发式基线
        p_base, F_base, r_base = HeuristicBaseline().run(env, state)
        # IPSO 优化
        p_opt, F_opt, hist, r_opt = IPSOOptimizer(env).optimize(state=state)
        results.append({"scene": s, "meta": meta, "state": state, "p_base": p_base,
                        "F_base": F_base, "r_base": r_base,
                        "p_opt": p_opt, "F_opt": F_opt, "hist": hist,
                        "r_opt": r_opt})

    # 组装报告文本
    lines = []
    lines.append("=" * 62)
    lines.append("南昌零碳园区储能系统仿真数学模型")
    lines.append(f"时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"储能 {C.STORAGE_POWER_MW}MW/{C.STORAGE_CAP_MWH}MWh | "
                 f"光伏 {C.PV_TOTAL_MW}MW | SOC {C.SOC_MIN*100:.0f}%-{C.SOC_MAX*100:.0f}%")
    lines.append("=" * 62)
    for r in results:
        rb, ro = r["r_base"], r["r_opt"]
        lines.append(f"\n【{r['meta']['name']}】")
        lines.append(f"  综合目标F       基线 {rb['F']:9.2f} -> IPSO {ro['F']:9.2f}")
        lines.append(f"  光伏自用率       基线 {rb['absorb_rate']*100:6.1f}% -> IPSO {ro['absorb_rate']*100:6.1f}%")
        lines.append(f"  弃光量(MWh)     基线 {rb['discard_mwh']:7.2f} -> IPSO {ro['discard_mwh']:7.2f}")
        lines.append(f"  日循环次数       基线 {rb['cycles_day']:5.2f} -> IPSO {ro['cycles_day']:5.2f}")
    report = "\n".join(lines)

    # 绘图
    img_path = os.path.join(out_dir, "调度对比图.png")
    _plot(results, img_path)

    # 写报告
    rep_path = os.path.join(out_dir, "仿真结果报告.txt")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    return report, img_path, rep_path


def _plot(results, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    import numpy as np

    for fp in ["/usr/share/fonts/HarmonyFont/Harmony-Regular.ttf",
               "/usr/share/fonts/google-droid-fonts/DroidSansFallback.ttf",
               "C:/Windows/Fonts/msyh.ttc"]:
        if os.path.exists(fp):
            try:
                font_manager.fontManager.addfont(fp)
            except Exception:
                pass
    plt.rcParams["font.sans-serif"] = ["HarmonyHeiTi", "Microsoft YaHei",
                                       "Noto Sans CJK SC", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    hours = np.arange(24)
    n = len(results)
    fig, axes = plt.subplots(n, 2, figsize=(14, 3.2 * n))
    if n == 1:
        axes = axes.reshape(1, -1)
    for i, r in enumerate(results):
        ax1, ax2 = axes[i]
        st = r["p_opt"]
        # 左: 功率
        ax1.axhline(0, color="k", lw=0.6)
        ax1.bar(hours, r["p_opt"], width=0.7, color="#c0392b", alpha=0.85, label="IPSO优化")
        ax1.bar(hours, r["p_base"], width=0.35, color="#2980b9", alpha=0.5, label="启发式基线")
        ax1.set_xlabel("小时"); ax1.set_ylabel("储能功率 (MW)")
        ax1.set_title(f"{r['meta']['name']} — 储能充放电功率")
        ax1.legend(loc="upper right", fontsize=8); ax1.grid(alpha=0.3)
        # 右: SOC + 光伏/负荷
        st_ = {"pv": r["state"]["pv"], "load": r["state"]["load"]}
        ax2b = ax2.twinx()
        ax2b.plot(hours, st_["pv"], "g-", lw=1.6, alpha=0.8, label="光伏出力")
        ax2b.plot(hours, st_["load"], "k-", lw=1.4, alpha=0.7, label="负荷")
        ax2b.set_ylabel("功率 (MW)")
        ax2b.set_ylim(0, max(st_["load"].max(), st_["pv"].max()) * 1.25)
        ax2.plot(hours, r["r_opt"]["detail"]["soc"] * 100, "r-", lw=2, label="SOC (IPSO)")
        ax2.axhline(C.SOC_MIN * 100, color="orange", ls="--", lw=1)
        ax2.axhline(C.SOC_MAX * 100, color="purple", ls="--", lw=1)
        ax2.set_ylabel("SOC (%)"); ax2.set_ylim(0, 100)
        ax2.set_xlabel("小时"); ax2.set_title(f"{r['meta']['name']} — SOC与源荷")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    report, img, rep = run_simulation()
    print(report)
    print(f"\n✅ 图已生成: {img}")
    print(f"✅ 报告已生成: {rep}")


if __name__ == "__main__":
    main()
