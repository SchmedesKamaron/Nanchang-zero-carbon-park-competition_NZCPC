# -*- coding: utf-8 -*-
"""
config.py — 南昌零碳园区储能系统仿真数学模型 · 参数配置
依据《储能系统运行策略优化与源网荷储协同深化方案》(Nanchang ZCPC)。

【修复版变更】按外部技术审查意见：
  1) 新增 DT_HOURS 统一时间步长，功率/能量/效率公式以此为基准（不再写死 1 小时）；
  2) 新增 OPEX_YUAN_PER_KWH 统一储能运维单价；
  3) 明确单位约定：功率 MW、能量 MWh、电价 元/kWh、排放因子 kgCO2/kWh。
"""

# ---------- 单位与时间步长 ----------
DT_HOURS = 1.0          # 时间步长(h)。扩展 15min 级调度时改为 0.25 即可。
KWH = 1.0
MWH = 1000.0            # 1 MWh = 1000 kWh

# ---------- 储能系统物理参数 ----------
STORAGE_CAP_MWH    = 8.0      # 额定容量 8 MWh
STORAGE_POWER_MW   = 4.0      # 额定功率 4 MW
SOC_MIN            = 0.20     # 荷电状态下限 20% (应急备用预留)
SOC_MAX            = 0.90     # 荷电状态上限 90%
ETA_CHARGE         = 0.95     # 充电效率
ETA_DISCHARGE      = 0.95     # 放电效率
MAX_CYCLES_DAY     = 2.0      # 日充放电循环次数上限
OPEX_YUAN_PER_KWH  = 0.03     # 储能运维单价 元/kWh(充放吞吐量)
ENERGY_USEFUL      = STORAGE_CAP_MWH * (SOC_MAX - SOC_MIN)   # 可用能量 5.6 MWh

# ---------- 光伏系统 ----------
PV_ROOF_MW        = 4.2      # 屋顶分布式光伏
PV_CANOPY_MW      = 0.6      # 光伏车棚
PV_TOTAL_MW       = PV_ROOF_MW + PV_CANOPY_MW   # 4.8 MW
PV_YEAR_KWH       = 457e4     # 优化后光伏自发自用量 457 万 kWh/年
PV_DISCARD_RATE   = 0.05      # 目标弃光率上限 5%

# ---------- 负荷 / 电网 ----------
LOAD_PEAK_MW      = 6.0      # 典型负荷峰值
DEMAND_RESP_PEAK_CUT_MW  = 4.0   # 单次最大削峰能力
DEMAND_RESP_VALLEY_FILL_MW = 3.0 # 单次最大填谷能力
DR_CURTAIL_FRAC = 0.25     # 需求响应可中断负荷削减比例(占该时段负荷)
DR_CURTAIL_FRAC   = 0.10     # 需求响应可中断负荷削减比例(用于真实改变净负荷)

# ---------- 经济参数 (江西分时电价, 元/kWh) ----------
PRICE = {
    "valley": 0.35,   # 谷段 00:00-08:00
    "flat":   0.70,   # 平段
    "peak":   1.10,   # 峰段 16:00-22:00
}
# 时段划分(小时索引 0-23)
VALLEY_HOURS = list(range(0, 8))                 # 谷
FLAT_HOURS   = [8,9,10,11,12,13,14,15,22,23]     # 平
PEAK_HOURS   = list(range(16,22))                # 峰

# 补贴标准 (元/kWh)
SUBSIDY_CUT   = 3.0    # 削峰需求响应补贴
SUBSIDY_FILL  = 1.5    # 填谷需求响应补贴
SUBSIDY_PEAKSHIFT = 0.65  # 调峰辅助服务补偿(0.5-0.8 取中值)

# ---------- 负荷 / 光伏日曲线模板 (相对值, 乘以各自峰值) ----------
PV_PROFILE = [0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.02,
              0.15,0.35,0.55,0.75,0.85,0.90,0.85,0.75,
              0.55,0.35,0.15,0.05,0.0,0.0,0.0,0.0]        # 24h 归一化光伏出力
LOAD_PROFILE = [0.45,0.42,0.40,0.38,0.40,0.45,0.55,0.65,
                0.72,0.78,0.85,0.95,0.92,0.84,0.82,0.86,
                0.94,1.00,0.97,0.88,0.78,0.68,0.58,0.50]  # 24h 归一化负荷

# ---------- 多目标权重 (四类场景默认) ----------
WEIGHTS = {
    "economic":       {"alpha": 0.6, "beta": 0.25, "gamma": 0.15},  # 日常经济
    "pv_absorb":      {"alpha": 0.2, "beta": 0.65, "gamma": 0.15},  # 光伏大发消纳
    "demand_response":{"alpha": 0.3, "beta": 0.15, "gamma": 0.55},  # 电网需求响应
    "blackout":       {"alpha": 0.0, "beta": 0.0,  "gamma": 0.0},   # 应急备用(孤岛)
}

# ---------- 优化算法 ----------
# SOC 越限罚系数(需与目标函数量级匹配, 建议取目标量级 10 倍以上)
PEN_SOC_FACTOR   = 20000.0
# 循环次数硬约束罚系数(DR/高收益场景收益可达数万元, 需足够大以压制超循环)
PEN_CYCLE_FACTOR = 20000.0

# ---------- 碳排放 ----------
EMISSION_GRID_KG_PER_KWH = 0.581   # 电网排放因子 kgCO2/kWh (华中电网近似)
