# 南昌零碳园区储能系统仿真数学模型_ems_sim_model_v3_Optimized Version

基于《储能系统运行策略优化与源网荷储协同深化方案》构建的可运行仿真数学模型。
本目录对应仓库中的 `ems_sim_model/ems_sim_model_v3/NZCPC_opt/`（v3 优化版）。

## 模型覆盖（对应方案章节）

| 方案章节 | 模型实现 |
|---|---|
| 1. 优化目标 | 经济/消纳/电网三目标带权重加权 `F = α·经济 + β·消纳 + γ·电网` |
| 2.1 IPSO 多目标优化 | `ems_sim/optimizer.py` 改进粒子群，直接寻优 24h 储能功率，含 SOC/循环数罚约束 |
| 2.2 四类运行场景 | 日常经济 / 光伏大发消纳 / 电网需求响应 / 应急备用（孤岛） |
| 2.3 一天充放时序 | 启发式基线（谷充峰放）对照 |
| 3. 电网互动 | 需求响应削峰/填谷补贴计入经济指标 |
| 4.1 三层调度 | 日前(IPSO) → 日内(滚动) → 实时(SOC 修正) |
| 5. 效能/经济测算 | 光伏自用率、弃光、峰谷差削减、碳排放、年收益概算 |

## 目录结构

```
NZCPC_opt/
├── run_simulation.py          # 主入口：跑四场景，IPSO vs 基线，输出报告
├── ems_sim/
│   ├── __init__.py
│   ├── config.py              # 全部物理/经济参数
│   ├── environment.py         # 园区"源-网-荷-储"仿真环境（功率平衡/SOC/多指标）
│   ├── optimizer.py           # IPSO 优化器 + 启发式基线 + 三层调度
│   └── scenes.py              # 四类场景定义
├── Front-end form filling.html   # 前端填表页（导出参数 JSON）
├── NZCPC_参数配置导出工具.html    # 升级版：可直接把 JSON 写入指定文件夹
├── demo_params.json           # 默认参数示例
├── README.md                  # 本文档
└── .gitignore
```

## 安装与运行

依赖：`numpy>=1.24`、`matplotlib>=3.7`（仅需 numpy 跑仿真，matplotlib 用于画图）

```bash
pip install numpy matplotlib
python run_simulation.py                 # 运行全部四场景
python run_simulation.py --params params.json   # 使用前端导出的自定义参数
```

运行后生成 `out/仿真结果报告.txt`（含各场景指标与年收益概算）。

## 参数说明

`config.py` 与 `demo_params.json` 中的核心参数：

- 储能：4 MW / 8 MWh，SOC 20%–90%，充放电效率 95%，日循环 ≤2 次
- 光伏：屋顶 4.2 MW + 车棚 0.6 MW = 4.8 MW；负荷峰值 6 MW
- 江西分时电价：谷 0.35 / 平 0.70 / 峰 1.10 元/kWh
- 需求响应补贴：削峰 3 元/kWh、填谷 1.5 元/kWh；电网排放因子 0.581 kgCO₂/kWh

## v3 相对 v2 的关键修复（`optimizer.py`）

- 修复初始化方向 bug（谷充峰放方向错反导致 IPSO 劣于基线）
- 惯性权重线性递减 0.9→0.4 + 精英粒子注入（保证起点不劣于基线）
- 需求响应场景峰段强放电初始化 + 多随机重启取最优
- 经济账修正（放电收益不再重复计效率）

修复后全场景 IPSO 均优于启发式基线（经济、光伏、需求响应、应急）。

## 注意事项

- 收益数值为仿真标定值，与实际存在参数偏差，引用前请按江西电网电价/补贴细则校准。
- 需求响应年收益中的补贴按"削峰放电量×3 元/kWh"估算可能偏高，可调低参与系数。

## 生成安装包（可选）

若需分发给无 Python 环境的用户，可用 PyInstaller 打包 `run_simulation.py` 为 exe，
并将打包好的 exe 作为 **GitHub Releases 附件** 发布（不要把 exe 提交进仓库主目录）。
