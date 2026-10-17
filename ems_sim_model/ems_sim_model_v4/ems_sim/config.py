# -*- coding: utf-8 -*-
"""
config.py — 南昌零碳园区储能系统仿真数学模型 · 参数配置 (v4)

依据《v3_Optimized_Version_审查意见.md》系统性修复后的统一配置。
本文件是全项目参数的唯一事实来源：所有功率/能量/效率公式都以
DT_HOURS 为基准，避免把时间步长写死为 1 小时。

单位约定（全项目统一）：
    功率   : MW        (p_batt, p_grid, pv, load, p_actual)
    能量   : MWh       (batt_in_hr, batt_out_hr, SOC×容量)
    电价   : 元/kWh    (PRICE)
    排放因子: kg CO2/kWh (EMISSION_GRID_KG_PER_KWH)
    时间步  : DT_HOURS (小时)
"""

# ---------- 单位与时间步长 ----------
DT_HOURS = 1.0          # 时间步长(h)。扩展到 15 分钟级调度时改为 0.25 即可。
KWH = 1.0
MWH = 1000.0            # 1 MWh = 1000 kWh

# ---------- 储能系统物理参数 ----------
STORAGE_CAP_MWH   = 8.0      # 额定容量 8 MWh
STORAGE_POWER_MW  = 4.0      # 额定功率 4 MW
SOC_MIN           = 0.20     # 荷电状态下限 20% (应急备用预留)
SOC_MAX           = 0.90     # 荷电状态上限 90%
ETA_CHARGE        = 0.95     # 充电效率
ETA_DISCHARGE     = 0.95     # 放电效率
MAX_CYCLES_DAY    = 2.0      # 日充放电循环次数上限
OPEX_YUAN_PER_KWH = 0.03     # 储能运维单价 元/kWh (按充放吞吐量计)
ENERGY_USEFUL     = STORAGE_CAP_MWH * (SOC_MAX - SOC_MIN)   # 可用能量 5.6 MWh

# ---------- 光伏系统 ----------
PV_ROOF_MW        = 4.2      # 屋顶分布式光伏
PV_CANOPY_MW      = 0.6      # 光伏车棚
PV_TOTAL_MW       = PV_ROOF_MW + PV_CANOPY_MW   # 4.8 MW
PV_YEAR_KWH       = 457e4    # 优化后光伏自发自用量 457 万 kWh/年
PV_DISCARD_RATE   = 0.05     # 目标弃光率上限 5%

# ---------- 负荷 / 电网 / 需求响应 ----------
LOAD_PEAK_MW              = 6.0   # 典型负荷峰值
DEMAND_RESP_PEAK_CUT_MW   = 4.0   # 单次最大削峰能力
DEMAND_RESP_VALLEY_FILL_MW = 3.0  # 单次最大填谷能力
# 需求响应可中断负荷削减比例(占该时段负荷的百分比)。单一定义，避免重复。
DR_CURTAIL_FRAC           = 0.10

# ---------- 经济参数 (江西分时电价, 元/kWh) ----------
PRICE = {
    "valley": 0.35,   # 谷段 00:00-08:00
    "flat":   0.70,   # 平段
    "peak":   1.10,   # 峰段 16:00-22:00
}
# 时段划分(小时索引 0-23)
VALLEY_HOURS = list(range(0, 8))                # 谷
FLAT_HOURS   = [8, 9, 10, 11, 12, 13, 14, 15, 22, 23]  # 平
PEAK_HOURS   = list(range(16, 22))              # 峰

# 补贴标准 (元/kWh)
SUBSIDY_CUT       = 3.0    # 削峰需求响应补贴
SUBSIDY_FILL      = 1.5    # 填谷需求响应补贴
SUBSIDY_PEAKSHIFT = 0.65   # 调峰辅助服务补偿(0.5-0.8 取中值)

# ---------- 负荷 / 光伏日曲线模板 (相对值, 乘以各自峰值) ----------
PV_PROFILE = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.02,
              0.15, 0.35, 0.55, 0.75, 0.85, 0.90, 0.85, 0.75,
              0.55, 0.35, 0.15, 0.05, 0.0, 0.0, 0.0, 0.0]   # 24h 归一化光伏出力
LOAD_PROFILE = [0.45, 0.42, 0.40, 0.38, 0.40, 0.45, 0.55, 0.65,
                0.72, 0.78, 0.85, 0.95, 0.92, 0.84, 0.82, 0.86,
                0.94, 1.00, 0.97, 0.88, 0.78, 0.68, 0.58, 0.50]  # 24h 归一化负荷

# ---------- 多目标权重 (四类场景默认) ----------
WEIGHTS = {
    "economic":        {"alpha": 0.6, "beta": 0.25, "gamma": 0.15},  # 日常经济
    "pv_absorb":       {"alpha": 0.2, "beta": 0.65, "gamma": 0.15},  # 光伏大发消纳
    "demand_response": {"alpha": 0.3, "beta": 0.15, "gamma": 0.55},  # 电网需求响应
    "blackout":        {"alpha": 0.0, "beta": 0.0,  "gamma": 0.0},   # 应急备用(孤岛)
}

# ---------- 优化算法罚系数 ----------
# 说明：物理硬约束(SOC/功率)由 environment.step() 保证；罚函数只用于引导优化器
# 偏向可行解。量级取目标函数(经济收益可达数千元/日)的 10 倍以上即可。
PEN_SOC_FACTOR   = 20000.0   # SOC 越限罚(按越限幅度平方累计)
PEN_CYCLE_FACTOR = 20000.0   # 日循环次数超限罚

# ---------- 碳排放 ----------
EMISSION_GRID_KG_PER_KWH = 0.581   # 电网排放因子 kgCO2/kWh (华中电网近似)
