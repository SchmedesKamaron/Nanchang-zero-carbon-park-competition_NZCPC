# 南昌零碳园区 储能系统仿真数学模型（20261017 v5）

依据外部技术审查意见对 `ems_sim_model` 重构，重点修复物理模型、指标核算与对比实验公平性。

## 一键使用

```bash
pip install -r requirements.txt        # 安装 numpy
python -m pytest -q                    # 运行 23 项回归测试
python run_simulation.py --params demo_params.json   # 四场景仿真 + 报告
```

## 修复要点（对照审查）

| 领域       | 修复                                  |
| -------- | ----------------------------------- |
| SOC 物理约束 | 放电按可用能量(SOC-SOC_MIN)·E·η 裁剪，杜绝负 SOC |
| 停电场景     | 孤岛电网交换功率=0，缺供正确统计                   |
| 日内滚动     | 支持 4/6/24h 窗口，窗口间 SOC 连续传递，修复越界     |
| SOC 罚函数  | 修正方向，按实际越限幅度正向罚                     |
| 碳排放      | MWh→kWh 单位修正，消除 1000 倍量级错误          |
| 需求响应     | curtail 落地为净负荷，补贴按实际响应量             |
| 公平对比     | 基线与所有 IPSO 重启共享同一 state             |
| 参数校验     | 非法配置显式报错                            |
| 自动化测试    | tests/ 23 项全通过                      |

## 目录结构

```
ems_sim_model/
├── run_simulation.py   # 主入口
├── demo_params.json    # 默认参数
├── pyproject.toml / requirements.txt
├── ems_sim/
│   ├── config.py       # 参数配置
│   ├── environment.py  # 物理模型
│   ├── optimizer.py    # IPSO+基线+三层调度
│   └── scenes.py       # 四类场景
├── tests/              # 回归测试
└── out/                # 运行报告输出
```
