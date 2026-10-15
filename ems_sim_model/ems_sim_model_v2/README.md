# 南昌零碳园区储能系统 · 仿真数学模型

依据《储能系统运行策略优化与源网荷储协同深化方案》(Nanchang ZCPC `Mainly.md`)
构建的可运行仿真数学模型工程。
## 模型覆盖（对应文章章节）

| 文章章节 | 模型实现 |
|---|---|
| 1. 优化目标 | 经济/消纳/电网三目标带权重加权 (`F=αF经+βF消+γF网`) |
| 2.1 IPSO 多目标优化 | `optimizer.IPSOOptimizer`, 直接寻优24h储能功率, 含SOC/功率/循环数罚约束 |
| 2.2 四类运行场景 | `scenes.py`: 日常经济/光伏大发消纳/电网需求响应/应急备用 |
| 2.3 一天充放时序(谷充峰放) | `HeuristicBaseline` 启发式基线对照 |
| 3. 电网互动(需求响应/补贴) | `environment` 经济指标含削峰/填谷补贴 |
| 4.1 日前-日内-实时三层调度 | `ThreeLayerScheduler` (日前IPSO + 日内滚动 + 实时修正) |
| 5. 效能/经济测算 | 光伏自用率、弃光、峰谷差削减、碳排放、年收益 |

## 目录

```
ems_sim_train/
├── run_simulation.py      # 主入口: 跑四场景 + IPSO vs 基线 + 出图 + 报告
├── ems_sim/
│   ├── config.py          # 全部物理/经济参数
│   ├── environment.py     # 园区"源-网-荷-储"仿真环境(功率平衡/SOC/多指标)
│   ├── optimizer.py       # IPSO / 启发式基线 / 三层调度
│   └── scenes.py          # 四类场景定义
└── out/                   # 输出: 调度对比图.png, 仿真结果报告.txt
```

## 安装与运行

### 方式一：pip 直接安装


```bash
# 直接从你的 GitHub 仓库安装（自动装好 numpy/matplotlib）
pip install git+https://github.com/你的用户名/你的仓库名.git

# 安装后，在任意位置输入命令即可运行，自动生成调度图与报告
ems-sim
```

输出文件生成在当前目录的 `out/` 文件夹：`调度对比图.png`、`仿真结果报告.txt`。

> 也可以在代码里使用：`from ems_sim import ParkEnvironment, IPSOOptimizer` 二次开发。

### 方式二：本地源码运行（开发/调试用）

```bash
pip install -r requirements.txt     # 首次先装依赖
python run_simulation.py            # 或 python -m ems_sim.cli
```

### 方式二：打包成 EXE（无需 Python，双击即用）

仅在 **Windows** 电脑上打包（需先装 Python 并勾选 `Add Python to PATH`）：

```bat
pip install pyinstaller
pyinstaller -F -w --name 零碳储能仿真模型 run_simulation.py
```

打包完成后，把 `ems_sim/` 和 `out/` 文件夹复制到 dist 目录下 exe 旁边：

```
dist/
├── 零碳储能仿真模型.exe
├── ems_sim/
└── out/
```

双击 `零碳储能仿真模型.exe` 即可运行，自动生成图与报告。

> 💡 一键脚本：双击项目根目录的 `打包运行_install_run.bat`，菜单选 `2` 自动打包，选 `1` 直接运行。

## 关键结果（仿真输出示例）

- 储能 4MW / 8MWh；光伏 4.8MW；SOC 20%–90%；日循环≤2次（一充两放）
- 经济模式: IPSO 综合目标 F 显著优于启发式基线, 日循环≈1.6次
- 光伏消纳模式: 弃光率从基线 ~57% 降至 ~38%(弃光 21.8→14.6 MWh)
- 收益概算: 峰谷套利72.8万/年+需求响应42万/年 ≈ 115万/年 

## 说明

- `weights` 可改四场景权重、`config.py` 可改容量/电价/排放因子等参数，重跑即可复现。
- 收益数值为仿真标定值，与文章目标量级一致但存在参数偏差；实际需按江西电网电价/补贴细则校准。
