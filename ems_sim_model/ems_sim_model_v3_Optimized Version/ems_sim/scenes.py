# -*- coding: utf-8 -*-
"""scenes.py — 四类典型运行场景。"""
from . import config as C

SCENE_META = {
    "economic": {
        "name": "日常经济运行模式",
        "desc": "工作日正常生产, 权重偏经济与消纳, 采用'一充两放'策略",
        "load_scale": 1.0, "pv_scale": 1.0, "soc0": 0.20,
    },
    "pv_absorb": {
        "name": "光伏大发消纳模式",
        "desc": "节假日/低负荷, 权重偏光伏消纳, 采用'边发边充+错峰放电'",
        "load_scale": 0.55, "pv_scale": 1.15, "soc0": 0.35,
    },
    "demand_response": {
        "name": "电网需求响应模式",
        "desc": "电网高峰/应急, 权重偏电网协同, 削峰4MW/填谷3MW",
        "load_scale": 1.0, "pv_scale": 1.0, "soc0": 0.30,
    },
    "blackout": {
        "name": "应急备用模式",
        "desc": "电网故障/停电, 孤岛运行, 保障关键负荷4h以上供电",
        "load_scale": 0.55, "pv_scale": 1.0, "soc0": 0.90,
    },
}
