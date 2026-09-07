# -*- coding: utf-8 -*-
"""Profile 配置存取 (.set 文件, JSON 内容)
18 槽位模型 (对齐 UR02+V2 v2.0.2 数据模型):
  slot = {hid: 遥控键码 hex, m1/m2: 修饰键('无'/'Ctrl'/...), key: 键盘键, x,y: 图上相对坐标 0~1}
"""
import json
import os
import sys


def _profile_dir():
    """配置目录: 打包 exe 时存 exe 旁边(临时目录 _MEIPASS 重启即丢!), 源码运行存项目根"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "profiles")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")


PROFILE_DIR = _profile_dir()

# 默认槽位坐标: 对照遥控器实物图标定 (相对坐标 0~1, 用户可拖动微调)
DEFAULT_XY = [
    (0.40, 0.13),  # 1  红色电源
    (0.60, 0.13),  # 2  灰色电源
    (0.40, 0.21),  # 3  静音
    (0.50, 0.19),  # 4  空鼠
    (0.60, 0.21),  # 5  返回
    (0.50, 0.27),  # 6  上
    (0.41, 0.33),  # 7  左
    (0.50, 0.33),  # 8  OK
    (0.60, 0.33),  # 9  右
    (0.50, 0.40),  # 10 下
    (0.40, 0.46),  # 11 后退箭头
    (0.60, 0.46),  # 12 菜单
    (0.50, 0.49),  # 13 Home
    (0.40, 0.53),  # 14 音量+
    (0.50, 0.57),  # 15 麦克风
    (0.40, 0.61),  # 16 音量-
    (0.62, 0.53),  # 17 右侧长条上
    (0.62, 0.61),  # 18 右侧长条下
]

# 槽位输出方式:
#   单击       = 按一下发一次组合键 (默认)
#   按住·切换  = 按一下按住不放, 再按一下才松开 (微信输入法 Win+Alt 这类"按住说话")
#   按住·跟随  = 按住遥控器键期间一直按住, 松开遥控器才松开
OUTPUT_MODES = ["单击", "按住·切换", "按住·跟随"]
DEFAULT_MODE = "单击"

DEFAULT_SLOTS = [
    dict(hid="", m1="无", m2="无", key="无", mode=DEFAULT_MODE, x=xy[0], y=xy[1])
    for xy in DEFAULT_XY
]


def norm_hid(h):
    """键码归一: 'CF'/'0xcf'/'00CF' -> '00CF' (旧配置兼容)"""
    s = (h or "").strip().upper().replace("0X", "").replace(":", "")
    if not s:
        return ""
    try:
        return "%04X" % int(s, 16)
    except Exception:
        return s


class Profile:
    def __init__(self, name="default"):
        self.name = name
        self.slots = [dict(s) for s in DEFAULT_SLOTS]

    def save(self, name=None):
        if name:
            self.name = name
        os.makedirs(PROFILE_DIR, exist_ok=True)
        path = os.path.join(PROFILE_DIR, self.name + ".set")
        data = {"name": self.name, "slots": self.slots}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return path

    @classmethod
    def load(cls, name="default"):
        path = os.path.join(PROFILE_DIR, name + ".set")
        p = cls(name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                slots = data.get("slots", [])
                for i, s in enumerate(slots[:18]):
                    p.slots[i].update(s)
                    if not p.slots[i].get("mode"):
                        p.slots[i]["mode"] = DEFAULT_MODE
                # 兼容 v1.5 的独立语音键配置: 迁移到第一个空槽位
                v = data.get("voice")
                if isinstance(v, dict):
                    parts = [m for m in (v.get("m1"), v.get("m2")) if m and m != "无"]
                    if v.get("key") and v["key"] != "无":
                        parts.append(v["key"])
                    if parts:
                        for i, s in enumerate(p.slots):
                            if not norm_hid(s.get("hid")):
                                s["hid"] = "00CF"
                                s["m1"] = v.get("m1") or "无"
                                s["m2"] = v.get("m2") or "无"
                                s["key"] = v.get("key") or "无"
                                s["mode"] = "按住·切换"   # 语音键现实=点一下锁存, 跟随无意义
                                break
            except Exception:
                pass
        return p

    @classmethod
    def list_profiles(cls):
        if not os.path.isdir(PROFILE_DIR):
            return ["default"]
        names = [f[:-4] for f in os.listdir(PROFILE_DIR) if f.endswith(".set")]
        return names or ["default"]

    def slot_by_hid(self, hid):
        target = norm_hid(hid)
        if not target:
            return -1
        for i, s in enumerate(self.slots):
            if norm_hid(s.get("hid")) == target:
                return i
        return -1
