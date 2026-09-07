# -*- coding: utf-8 -*-
"""UR02 Vibe 助手 — 主界面 (CustomTkinter 暗色蓝主题)
版本: v1.15-20260905
流程: 拖圆圈到按键位置 -> 按遥控器键录键码 -> 右侧学键盘键/选修饰键 -> 自动保存
输出方式: 每个槽位可选 单击 / 按住·切换 / 按住·跟随
         语音键 00CF 与其它键完全一样, 在槽位里自己学自己配(如 Win+Alt = 微信输入法)
"""
import array
import collections
import datetime
import os
import numpy as np
import sys
import ctypes
import threading
import time
import tkinter as tk
import wave

import customtkinter as ctk
import pystray
from PIL import Image, ImageTk

from .consts import (APP_NAME, APP_VERSION, AUDIO_SAMPLE_RATE, CODEC_RATES,
                     VOICE_USAGE, usage_name)
from .hid_reader import RemoteKeyReader
from .keymap import (MOD_CHOICES, KeyboardHook, send_combo,
                     build_vks, press_keys, release_keys)
from .profile import Profile, norm_hid, OUTPUT_MODES, DEFAULT_MODE
from .atvv import AtvvEngine
from .adpcm import ImaAdpcmDecoder
from .audio_out import (CableSink, list_cable_outputs, highpass_80hz,
                        plc_fill)

# ---- 丢包补偿(PLC)控制参数 ----
FILL_KP = 25.0     # 比例反馈(样本/单位缓冲偏差): 缓冲一涨立刻少补
FILL_KI = 0.6      # 积分反馈(样本/帧, 约 23 帧/秒): 消除稳态补多/补少
FILL_MAX = 80      # 单帧最多补 80 个样本(10ms)
# 为什么封顶: v1.24 实测开流头两秒 BLE 热身, 窗口测到 60ms 间隔 -> 每帧补 236 个
# (一帧真实才 244 个, 等于一半是合成音), 用户听感"爆破音很重";
# 而 v1.23 只有 12 点斜坡 + 断流 5ms 渐入渐出, 听感反而更干净。
# 结论: 超过 10ms 的缺口宁可留白(渐入渐出), 也别硬造。

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 只算"修饰"的键名: 学键时最后一位是这些就当作纯修饰键组合(没有主键)
PURE_MOD_NAMES = {"Ctrl", "Alt", "Shift", "Win"}


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


class App(ctk.CTk):
    SLOT_R = 13  # 槽位圆半径(显示像素)

    def __init__(self):
        super().__init__()
        self.title("%s %s" % (APP_NAME, APP_VERSION))
        self.geometry("1180x780")
        self.minsize(1080, 700)

        # ---------- 状态 ----------
        self.profile = Profile.load("default")
        self.sel_slot = -1
        self.learning_hid = -1      # 正在学遥控键的槽位
        self.learning_key = -1      # 正在学键盘键的槽位
        self.mapping_on = ctk.BooleanVar(value=True)
        # 2026-09-06: 语音桥接默认软件启动即自动开启(见 _start_voice)
        # 2026-09-07: 用户要求把开关加回来 —— 启动仍自动开, 但断连后能"关一下再开"手动重连, 不用重启软件
        self.voice_on = ctk.BooleanVar(value=False)
        self.mic_open = False
        self.audio_rate = AUDIO_SAMPLE_RATE   # 实测固定 8kHz(见 consts 注释)
        # 2026-09-05 底噪根因修复:
        #   * 每帧帧头 6B 自带状态[pred_lo,pred_hi,step_idx] -> 每帧用它重置解码器
        #     (固件 0x8AF4 处已证实: 头写在 0x7590 编码调用之前)
        #   * 80Hz 高通滤掉底噪里 <100Hz 的低频鼓包(占静默段能量 94%)
        #   * -6dB 整体降一档: 实测说话段 0.76% 削顶 + RMS=-10dBFS, 说话大声容易爆
        self._audio_hdr = b""                  # 最近收到的 6B 序号头里的状态
        self._hp = highpass_80hz(fs=8000)      # 80Hz Butterworth 高通 biquad (Direct Form I)
        self._gain = 10 ** (-6 / 20)           # -6dB 系数 ≈ 0.5012
        self._voice_evt_ts = 0.0        # 语音键防抖时间戳
        self._last_audio_ts = 0.0       # 最后收到音频包时间(流看门狗用)
        self._abuf = bytearray()        # 音频组缓冲(组首剥头后累积, 8B尾到达 flush)
        self._drop_frames = 0           # 因蓝牙丢包而整组丢弃的帧数(诊断用)
        # 诊断: 每次推流把"解码后原始"与"送 CABLE 的最终"两份 PCM 存 wav,
        # 用于离线定位"失真到底出在解码环节还是滤波/增益环节"。文件很小(17s 约 270KB)
        self._diag_on = True
        self._diag_raw = []             # 解码后、滤波前
        self._diag_out = []             # 滤波+增益后, 即真正送 VB-CABLE 的数据
        self._diag_frames = 0
        self._diag_48k = []             # 真正写进声卡的数据(设备采样率), 由 CableSink.diag_capture 回填
        self._stream_t0 = 0.0           # 本次推流首帧时刻(算平均组间隔用)
        self._frame_count = 0
        self._last_frame_ts = 0.0       # 上一组到达时间(实测组间隔用)
        self._frame_interval = 0.032    # 组间隔(秒), 实测自适应
        self._prev_last = None          # 上一帧最后一个样本(帧间补点用)
        # ---- 丢包补偿(PLC) ----
        # 2026-09-06 实测: 遥控器是【真 8kHz 实时采样 + 射频来不及发而丢样本】
        # (三份不同组间隔的录音反推内容基频都落在 110~124Hz, 不随组间隔变化 -> 排除"整体慢采样")
        # 所以每组之间会真实丢掉 (组间隔*8000 - 244) 个样本, 43.6ms 时 = 105 个 = 13ms。
        # 这 13ms 必须补出来, 否则缓冲被抽干 -> 断流 -> "哒哒"爆破音。
        self._hist = collections.deque(maxlen=1200)   # 最近已送出样本(8k), PLC 取材用
        self._grp_T = 0.0               # 滑动窗口实测组间隔(秒), PLC 补点数由它决定
        self._win_t0 = 0.0              # 滑动窗口起点
        self._win_n = 0                 # 滑动窗口内帧数
        self._win_k = 0                 # 已完成窗口数(第 1 个窗口是 BLE 热身期, 丢弃)
        self._fill_trim = 0.0           # 补点量积分修正(样本), 由缓冲水位驱动
        self._fill_max = 0              # 单帧最大补点数(诊断)
        self._plc_filled = 0            # 本次推流补偿的样本数(诊断)
        self._plc_events = 0            # 触发补偿的帧数(诊断)
        self._held = {}                 # 槽位 index -> 按住中的 VK 列表
        self._hid_flash_after = {}
        self._learn_timeout = None
        self._saving = threading.Lock()
        self._devices = []

        # ---------- 引擎 ----------
        self.decoder = ImaAdpcmDecoder()
        self.cable = CableSink(self.log_status)
        self.ble = AtvvEngine(self._on_audio, self._on_ctrl, self.log_status,
                              self._on_battery)
        self.reader = RemoteKeyReader(self._on_hid_key, self.log_status, self._on_hid_release)
        self.hook = KeyboardHook(self._on_kbd_combo, self.log_status)
        self.reader.start()

        # ---------- 布局 ----------
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_topbar()
        self._build_canvas()
        self._build_side()
        self._build_statusbar()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 默认选中第一个已配的槽位(默认配置=语音键), 没有则选 1 号
        first = next((i for i, s in enumerate(self.profile.slots)
                      if norm_hid(s.get("hid"))), 0)
        self._select_slot(first)
        self.log("就绪。左侧遥控器图上有 18 个槽位圈(灰虚线=空位, 蓝圈=已配)。")
        self.log("用法: 点任意圈(含空圈)选中 -> [学遥控键]按遥控器键 -> [学键盘键]配输出 -> 完成。")
        self.log("按遥控键右侧[实时捕获]会立即显示键码和名称; 一直显示'等待按键'就点[遥控器自检]。")
        self.log("提示: UR02 全部按键走 col02 通道; 音量/电源/返回等系统也会响应, 映射后可能双触发。")
        # 语音桥接自动开启(延后 800ms, 等主窗口渲染完再连 BLE, 避免启动卡顿感)
        self.after(800, self._start_voice)

    def _start_voice(self):
        """软件启动即自动开语音桥接(顶栏[语音桥接(BLE)]开关同步变开)。
        2026-09-07: 开关已加回, 断连时"关一下再开"即手动重连, 不必重启软件。
        失败(没装 VB-CABLE / 声卡打不开)时 _toggle_voice 会自己置 False 并写日志。"""
        self.voice_on.set(True)
        self._toggle_voice()

    # ================= 顶栏 =================
    def _build_topbar(self):
        bar = ctk.CTkFrame(self, height=64)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkLabel(bar, text="配置", font=("", 13, "bold")).grid(row=0, column=0, padx=(10, 4), pady=8)
        self.profile_menu = ctk.CTkOptionMenu(bar, width=140, values=Profile.list_profiles(),
                                              command=self._on_profile_switch)
        self.profile_menu.set(self.profile.name)
        self.profile_menu.grid(row=0, column=1, padx=4)
        ctk.CTkButton(bar, text="另存为", width=76, command=self._save_profile_as).grid(row=0, column=2, padx=4)

        # 关闭行为: 勾选 = 点 X 最小化到托盘(不退出), 不勾 = 直接关闭
        self.tray_min = ctk.BooleanVar(value=True)
        self._tray = None
        self._tray_icon_img = None
        self._load_settings()
        ctk.CTkCheckBox(bar, text="关闭最小化到托盘", variable=self.tray_min,
                        command=self._save_settings,
                        checkbox_width=18, checkbox_height=18,
                        font=("", 12)).grid(row=0, column=3, columnspan=2, padx=(12, 4))

        ctk.CTkLabel(bar, text="设备").grid(row=0, column=6, padx=(16, 2))
        self.device_menu = ctk.CTkOptionMenu(bar, width=170, values=["扫描中..."],
                                             command=self._on_device_pick)
        self.device_menu.set("扫描中...")
        self.device_menu.grid(row=0, column=7, padx=4)
        ctk.CTkButton(bar, text="刷新", width=56, command=self._scan_devices).grid(row=0, column=8, padx=(0, 4))
        ctk.CTkButton(bar, text="遥控器自检", width=96, fg_color="#7c3aed", hover_color="#6d28d9",
                      command=self._diag_remote).grid(row=0, column=9, padx=(6, 10))

        # 右上角: 遥控器电量(标准 Battery Service 0x180F / 0x2A19, NOTIFY 推送)
        bar.grid_columnconfigure(10, weight=1)      # 吃掉剩余宽度, 把电量顶到最右
        self.batt_var = ctk.StringVar(value="遥控器电量 --")
        self.batt_label = ctk.CTkLabel(bar, textvariable=self.batt_var,
                                       font=("", 13, "bold"), text_color="#8b8f94")
        self.batt_label.grid(row=0, column=11, sticky="e", padx=(8, 14), pady=8)

        self.mapping_switch = ctk.CTkSwitch(bar, text="启用按键映射", variable=self.mapping_on,
                                            command=self._toggle_mapping, onvalue=True, offvalue=False)
        self.mapping_switch.grid(row=1, column=0, columnspan=2, padx=(10, 4), pady=(2, 6), sticky="w")

        # 2026-09-07: [语音桥接(BLE)] 开关加回来(09-06 曾移除)。
        # 启动仍自动开启, 但 BLE 断连时可以直接关一下再开 = 重连, 不必重启软件。
        self.voice_switch = ctk.CTkSwitch(bar, text="语音桥接(BLE)", variable=self.voice_on,
                                          command=self._toggle_voice, onvalue=True, offvalue=False)
        self.voice_switch.grid(row=1, column=2, padx=(16, 4), pady=(2, 6), sticky="w")

        self.mic_btn = ctk.CTkButton(bar, text="开麦", width=68, state="disabled", command=self._toggle_mic)
        self.mic_btn.grid(row=1, column=3, padx=(16, 8), pady=(2, 6), sticky="w")

        ctk.CTkLabel(bar, text="输出到").grid(row=1, column=6, padx=(8, 2), pady=(2, 6))
        outs = [n for _, n in list_cable_outputs()] or ["(未装 VB-CABLE)"]
        self.cable_menu = ctk.CTkOptionMenu(bar, width=250, values=outs)
        self.cable_menu.set(outs[0])
        self.cable_menu.grid(row=1, column=7, columnspan=2, padx=4, pady=(2, 6), sticky="w")

        self.after(600, self._scan_devices)

    # ================= 左: 遥控器画布 =================
    def _build_canvas(self):
        left = ctk.CTkFrame(self)
        left.grid(row=1, column=0, sticky="nsw", padx=8, pady=4)
        self.canvas = tk.Canvas(left, width=400, height=650, bg="#1a1a1d",
                                highlightthickness=0)
        self.canvas.pack(padx=6, pady=6)

        img = Image.open(resource_path(os.path.join("assets", "remote.png")))
        cw, ch = 400, 650
        scale = min(cw / img.width, ch / img.height)
        self.img_w, self.img_h = int(img.width * scale), int(img.height * scale)
        self.tkimg = ImageTk.PhotoImage(img.resize((self.img_w, self.img_h), Image.LANCZOS))
        self.ox = (cw - self.img_w) // 2
        self.oy = (ch - self.img_h) // 2
        self.canvas.create_image(self.ox, self.oy, anchor="nw", image=self.tkimg)

        # 槽位圆圈: 18 个槽位全部常驻 (对齐遥控器实物键位)
        #   已配(学到键码) = 蓝实线大圈; 空槽位 = 灰虚线小圈, 点它即可开始配置
        #   命中: Tk 空心圆(fill="")内部点不中, 所以选择/拖动全部走画布级"最近圈心"判定
        self.slot_ids = {}
        self._drag_idx = -1
        self._drag_off = (0, 0)
        for i, s in enumerate(self.profile.slots):
            self._create_slot_circle(i, s.get("x", 0.5), s.get("y", 0.5))
        self.canvas.bind("<Button-1>", self._cv_press)
        self.canvas.bind("<B1-Motion>", self._cv_drag)
        self.canvas.bind("<ButtonRelease-1>", self._cv_release)

    def _hit_slot(self, x, y):
        """找离点击位置最近的槽位圈(容差 18px), 找不到返回 -1"""
        best, bd = -1, 18
        for i, (oid, tid) in self.slot_ids.items():
            x0, y0, x1, y1 = self.canvas.coords(oid)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d < bd:
                bd, best = d, i
        return best

    def _cv_press(self, e):
        idx = self._hit_slot(e.x, e.y)
        if idx < 0:
            return
        self._drag_idx = idx
        self._select_slot(idx)
        x0, y0, x1, y1 = self.canvas.coords(self.slot_ids[idx][0])
        self._drag_off = ((x0 + x1) / 2 - e.x, (y0 + y1) / 2 - e.y)

    def _cv_drag(self, e):
        if self._drag_idx >= 0:
            self._move_slot(self._drag_idx,
                            e.x + self._drag_off[0], e.y + self._drag_off[1])

    def _cv_release(self, e):
        if self._drag_idx >= 0:
            self._drag_idx = -1
            self._save_profile_quiet()

    def _create_slot_circle(self, i, rx, ry):
        learned = bool(norm_hid(self.profile.slots[i].get("hid")))
        cx = self.ox + rx * self.img_w
        cy = self.oy + ry * self.img_h
        tags = ("slot%d" % i, "slots")
        if learned:
            r, ol, w, dash, fs, fc = self.SLOT_R, "#3b82f6", 2, (), 10, "#9ca3af"
        else:
            r, ol, w, dash, fs, fc = 9, "#57534e", 1, (3, 3), 9, "#6b7280"
        oid = self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                      outline=ol, width=w, fill="", dash=dash, tags=tags)
        tid = self.canvas.create_text(cx, cy, text=str(i + 1), fill=fc,
                                      font=("", fs, "bold"), tags=tags)
        self.slot_ids[i] = (oid, tid)

    def _refresh_slot_style(self, idx):
        """按槽位是否已配键码刷新圆圈样式(学到/清空后调用)。删旧重建, 保持选中高亮。"""
        if idx in self.slot_ids:
            oid, tid = self.slot_ids.pop(idx)
            self.canvas.delete(oid, tid)
        s = self.profile.slots[idx]
        self._create_slot_circle(idx, s.get("x", 0.5), s.get("y", 0.5))
        if idx == self.sel_slot and idx in self.slot_ids:
            oid, _ = self.slot_ids[idx]
            self.canvas.itemconfig(oid, outline="#22d3ee", width=3)
        self.canvas.tag_raise("slots")

    def _slot_xy(self, i):
        oid, tid = self.slot_ids[i]
        x0, y0, x1, y1 = self.canvas.coords(oid)
        return (x0 + x1) / 2, (y0 + y1) / 2

    def _move_slot(self, i, cx, cy):
        oid, tid = self.slot_ids[i]
        r = self.SLOT_R
        cx = max(self.ox + r, min(self.ox + self.img_w - r, cx))
        cy = max(self.oy + r, min(self.oy + self.img_h - r, cy))
        self.canvas.coords(oid, cx - r, cy - r, cx + r, cy + r)
        self.canvas.coords(tid, cx, cy)
        self.profile.slots[i]["x"] = (cx - self.ox) / self.img_w
        self.profile.slots[i]["y"] = (cy - self.oy) / self.img_h

    def _on_slot_press(self, event, idx):
        self._select_slot(idx)
        self._drag_off = (self._slot_xy(idx)[0] - event.x, self._slot_xy(idx)[1] - event.y)

    def _on_slot_drag(self, event, idx):
        cx = event.x + getattr(self, "_drag_off", (0, 0))[0]
        cy = event.y + getattr(self, "_drag_off", (0, 0))[1]
        self._move_slot(idx, cx, cy)

    def _on_slot_release(self, idx):
        self._save_profile_quiet()

    def _select_slot(self, idx):
        # 恢复上一个选中样式(按是否已配恢复蓝/灰)
        if 0 <= self.sel_slot < 18 and self.sel_slot in self.slot_ids:
            oid, tid = self.slot_ids[self.sel_slot]
            if norm_hid(self.profile.slots[self.sel_slot].get("hid")):
                self.canvas.itemconfig(oid, outline="#3b82f6", width=2, dash=())
            else:
                self.canvas.itemconfig(oid, outline="#57534e", width=1, dash=(3, 3))
        self.sel_slot = idx
        if idx in self.slot_ids:
            oid, tid = self.slot_ids[idx]
            self.canvas.itemconfig(oid, outline="#22d3ee", width=3)
        self._refresh_side()

    def _ensure_slot_circle(self, idx):
        """学到键码后: 灰圈变蓝圈并高亮 (圈本就在实物键位上, 无需拖动)"""
        s = self.profile.slots[idx]
        if not s.get("hid"):
            return
        self._refresh_slot_style(idx)
        oid, _ = self.slot_ids.get(idx, (None, None))
        if oid:
            self.canvas.itemconfig(oid, outline="#22c55e", width=3)
        self.canvas.tag_raise("slots")

    def _remove_slot_circle(self, idx):
        if idx in self.slot_ids:
            oid, tid = self.slot_ids.pop(idx)
            self.canvas.delete(oid, tid)

    def _flash_slot(self, idx, color="#22c55e"):
        """按键触发/录码成功时槽位闪色"""
        if idx not in self.slot_ids:
            return
        oid, _ = self.slot_ids[idx]
        self.canvas.itemconfig(oid, fill=color)
        self.after(350, lambda: self.canvas.itemconfig(oid, fill=""))

    # ================= 右: 配置面板 =================
    def _build_side(self):
        side = ctk.CTkScrollableFrame(self, width=330)
        side.grid(row=1, column=1, sticky="nsew", padx=(0, 8), pady=4)

        self.lbl_slot = ctk.CTkLabel(side, text="槽位 -", font=("", 18, "bold"))
        self.lbl_slot.pack(anchor="w", padx=10, pady=(8, 2))
        self.lbl_hint = ctk.CTkLabel(side, text="", text_color="#9ca3af", wraplength=290, justify="left")
        self.lbl_hint.pack(anchor="w", padx=10, pady=(0, 8))

        # 实时捕获显示: 按遥控器键立即看到键码和来源通道
        box = ctk.CTkFrame(side, fg_color="#111827", corner_radius=6)
        box.pack(anchor="w", padx=10, pady=(0, 6), fill="x")
        ctk.CTkLabel(box, text="实时捕获", font=("", 11, "bold"),
                     text_color="#60a5fa").pack(anchor="w", padx=8, pady=(5, 0))
        self.lbl_last = ctk.CTkLabel(box, text="等待按键…", font=("", 15, "bold"),
                                     text_color="#facc15")
        self.lbl_last.pack(anchor="w", padx=8, pady=(2, 6))

        ctk.CTkLabel(side, text="① 遥控器键码").pack(anchor="w", padx=10)
        row1 = ctk.CTkFrame(side, fg_color="transparent")
        row1.pack(anchor="w", padx=10, pady=2, fill="x")
        self.lbl_hid = ctk.CTkLabel(row1, text="未设置", width=90, font=("", 14, "bold"))
        self.lbl_hid.pack(side="left")
        self.btn_learn_hid = ctk.CTkButton(row1, text="学遥控键", width=100, command=self._learn_hid)
        self.btn_learn_hid.pack(side="left", padx=8)

        ctk.CTkLabel(side, text="② 键盘输出 (支持组合键)").pack(anchor="w", padx=10, pady=(12, 0))
        row2 = ctk.CTkFrame(side, fg_color="transparent")
        row2.pack(anchor="w", padx=10, pady=2, fill="x")
        self.lbl_key = ctk.CTkLabel(row2, text="无", width=150, font=("", 14, "bold"))
        self.lbl_key.pack(side="left")
        self.btn_learn_key = ctk.CTkButton(row2, text="学键盘键", width=100, command=self._learn_key)
        self.btn_learn_key.pack(side="left", padx=8)

        row3 = ctk.CTkFrame(side, fg_color="transparent")
        row3.pack(anchor="w", padx=10, pady=(4, 0))
        ctk.CTkLabel(row3, text="修饰键1").pack(side="left")
        self.m1_menu = ctk.CTkOptionMenu(row3, width=90, values=MOD_CHOICES,
                                         command=lambda v: self._set_mod(1, v))
        self.m1_menu.set("无")
        self.m1_menu.pack(side="left", padx=(6, 14))
        ctk.CTkLabel(row3, text="修饰键2").pack(side="left")
        self.m2_menu = ctk.CTkOptionMenu(row3, width=90, values=MOD_CHOICES,
                                         command=lambda v: self._set_mod(2, v))
        self.m2_menu.set("无")
        self.m2_menu.pack(side="left", padx=6)

        row3b = ctk.CTkFrame(side, fg_color="transparent")
        row3b.pack(anchor="w", padx=10, pady=(6, 0), fill="x")
        ctk.CTkLabel(row3b, text="输出方式").pack(side="left")
        self.mode_menu = ctk.CTkOptionMenu(row3b, width=112, values=OUTPUT_MODES,
                                           command=self._set_mode)
        self.mode_menu.set(DEFAULT_MODE)
        self.mode_menu.pack(side="left", padx=(6, 8))
        self.btn_hold = ctk.CTkButton(row3b, text="按住测试", width=80,
                                      fg_color="#0e7490", hover_color="#0891b2",
                                      command=self._test_hold)
        self.btn_hold.pack(side="left")
        self.lbl_hold = ctk.CTkLabel(row3b, text="", text_color="#9ca3af")
        self.lbl_hold.pack(side="left", padx=6)

        row4 = ctk.CTkFrame(side, fg_color="transparent")
        row4.pack(anchor="w", padx=10, pady=(10, 0))
        ctk.CTkButton(row4, text="清空此槽位", width=110, fg_color="#7f1d1d",
                      hover_color="#991b1b", command=self._clear_slot).pack(side="left")
        self.btn_test = ctk.CTkButton(row4, text="测试输出", width=100, command=self._test_send)
        self.btn_test.pack(side="left", padx=8)

        # 使用说明
        sep = ctk.CTkLabel(side, text="— 使用说明 —", text_color="#6b7280")
        sep.pack(pady=(20, 4))
        help_text = ("1. 顶栏[设备]确认已连接 UR02\n"
                     "2. 左侧图上有 18 个槽位圈, 对齐实物按键:\n"
                     "   蓝圈=已配, 灰虚线小圈=空槽位\n"
                     "3. 点任意圈选中 -> [学遥控键] -> 按遥控器键\n"
                     "4. [学键盘键] -> 按键盘键(Ctrl/Alt/Shift/Win\n"
                     "   组合可叠加)\n"
                     "5. 修饰键1/2 可再叠 Ctrl/Alt/Shift/Win/Fn\n"
                     "6. 圈可拖动微调对齐; 自动保存; 映射开关打\n"
                     "   开后按遥控键即输出\n\n"
                     "通道: UR02 全部按键走 col02, 报文\n"
                     "[2, lo, hi, 0,0,0] -> 键码 = lo|hi<<8。\n"
                     "抓不到的常见原因: 遥控器休眠(30秒无\n"
                     "操作) 或 指示灯是红色=红外模式。\n\n"
                     "已知: 音量/电源/返回/主页 系统本身也会\n"
                     "响应, 映射后可能双触发 —— 建议这些键\n"
                     "配成系统没有的快捷键。\n\n"
                     "语音键 00CF 也能像普通键一样映射:\n"
                     " 选中槽位 -> [学遥控键] 按遥控器语音键 ->\n"
                     " [学键盘键] 按 Win+Alt(可只有修饰键)。\n"
                     " 输出方式选[按住·切换]: 按一下=按住+开麦\n"
                     " (蓝灯常亮, 不用按着不放), 再按一下=松开\n"
                     "+关麦 —— 跟遥控器本身的语音键行为一致,\n"
                     " 适合微信输入法这类按住说话的快捷键。\n"
                     " 点[按住测试]可先验证输入法有没有反应。\n"
                     " 声音进 VB-CABLE, 软件麦克风选 'CABLE Output'。\n"
                     "若蓝灯闪一下就灭 = 主机没及时回命令,\n"
                     "重开一次[语音桥接]再试。")
        ctk.CTkLabel(side, text=help_text, text_color="#9ca3af", justify="left",
                     wraplength=300).pack(anchor="w", padx=10, pady=4)

    def _refresh_side(self):
        i = self.sel_slot
        if not (0 <= i < 18):
            return
        s = self.profile.slots[i]
        self.lbl_slot.configure(text="槽位 %d" % (i + 1))
        hx = norm_hid(s["hid"])
        self.lbl_hid.configure(text=("%s %s" % (hx, usage_name(hx))) if hx else "未设置")
        self.lbl_key.configure(text=s["key"])
        self.m1_menu.set(s["m1"])
        self.m2_menu.set(s["m2"])
        # 语音键现实行为 = 点一下锁存(蓝灯常亮), 没有按住语义 -> 不提供[跟随]
        if hx == VOICE_USAGE:
            self.mode_menu.configure(values=["单击", "按住·切换"])
            if (s.get("mode") or DEFAULT_MODE) not in ("单击", "按住·切换"):
                s["mode"] = "按住·切换"
                self._save_profile_quiet()
        else:
            self.mode_menu.configure(values=OUTPUT_MODES)
        self.mode_menu.set(s.get("mode") or DEFAULT_MODE)
        self._upd_hold_ui()
        if self.learning_hid == i:
            self.lbl_hint.configure(text=">>> 按一下遥控器上的键 (语音键也能学)…",
                                    text_color="#fbbf24")
        elif self.learning_key == i:
            self.lbl_hint.configure(text=">>> 按键盘键; 只按修饰键(如 Win+Alt)也行…",
                                    text_color="#fbbf24")
        else:
            self.lbl_hint.configure(text="拖动圆圈定位; 学键自动保存。", text_color="#9ca3af")

    # ---------- 输出方式 / 按住型 ----------
    def _combo_text(self, s=None):
        s = s if s is not None else self.profile.slots[self.sel_slot]
        parts = [m for m in (s.get("m1"), s.get("m2")) if m and m != "无"]
        k = s.get("key")
        if k and k != "无":
            parts.append(k)
        return "+".join(parts) or "(未设置)"

    def _set_mode(self, value):
        i = self.sel_slot
        if not (0 <= i < 18):
            return
        self._slot_release(i, "切换输出方式")      # 改方式前先松开, 防卡键
        self.profile.slots[i]["mode"] = value
        self._save_profile_quiet()
        self._upd_hold_ui()
        self.log("槽位 %d 输出方式: %s" % (i + 1, value))

    def _test_hold(self):
        """手动按住/松开当前槽位, 用来验证'按住说话'类快捷键有没有反应"""
        i = self.sel_slot
        if i in self._held:
            self._slot_release(i, "手动松开")
        else:
            self._slot_press(i, "手动按住")

    def _upd_hold_ui(self):
        i = self.sel_slot
        held = i in self._held
        if held:
            self.btn_hold.configure(text="松开", fg_color="#b45309", hover_color="#d97706")
            self.lbl_hold.configure(text="按住中 ●", text_color="#22c55e")
            self.btn_test.configure(text="测试关闭", fg_color="#b45309", hover_color="#d97706")
        else:
            self.btn_hold.configure(text="按住测试", fg_color="#0e7490", hover_color="#0891b2")
            self.lbl_hold.configure(text="空闲", text_color="#9ca3af")
            self.btn_test.configure(text="测试输出", fg_color="#2f6f3f", hover_color="#3b8551")

    def _slot_press(self, i, src=""):
        """按住槽位 i 的快捷键 (按 mode 决定是否锁存), 语音键顺便开麦"""
        if i in self._held:
            return
        s = self.profile.slots[i]
        vks = build_vks(s.get("m1"), s.get("m2"), s.get("key"))
        txt = self._combo_text(s)
        if not vks:
            self.log("槽位 %d(%s): 快捷键未设置, 无输出" % (i + 1, src or "-"))
            return
        press_keys(vks)
        self._held[i] = vks
        self.log("槽位 %d(%s): 按住 [%s]" % (i + 1, src or "-", txt))
        self._upd_hold_ui()
        if norm_hid(s.get("hid")) == VOICE_USAGE:
            self._mic_on()

    def _slot_release(self, i, src=""):
        """松开槽位 i 的快捷键, 语音键顺便关麦"""
        vks = self._held.pop(i, None)
        if vks:
            release_keys(vks)
            self.log("槽位 %d(%s): 松开 [%s]" % (i + 1, src or "-",
                                                self._combo_text(self.profile.slots[i])))
        self._upd_hold_ui()
        if vks and norm_hid(self.profile.slots[i].get("hid")) == VOICE_USAGE:
            self._mic_off()

    def _release_all(self, src=""):
        for i in list(self._held):
            self._slot_release(i, src)

    # ================= 底: 状态栏 + 日志 =================
    def _build_statusbar(self):
        bottom = ctk.CTkFrame(self, height=150)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 8))
        bottom.grid_rowconfigure(1, weight=1)
        self.lbl_status = ctk.CTkLabel(bottom, text="hid:启动中...", anchor="w")
        self.lbl_status.grid(row=0, column=0, sticky="ew", padx=10, pady=(4, 0))
        self.txt_log = ctk.CTkTextbox(bottom, height=110, font=("", 12))
        self.txt_log.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 6))
        self.txt_log.configure(state="disabled")

    def log(self, msg):
        def _do():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.after(0, _do)

    def log_status(self, status):
        """引擎状态回调(可能来自其他线程): hid:xxx / raw:xxx / ble:xxx / audio:xxx"""
        def _do():
            if status.startswith("ble:mic"):
                pass
            if status.startswith("hid:"):
                self._st_hid = status
                self._st_raw = None
            elif status.startswith("raw:"):
                self._st_raw = status
            elif status.startswith("ble:"):
                self._st_ble = status
                # BLE 断开就别再显示上一次的电量(那是旧值, 会误导)
                if "已断开" in status:
                    self._battery_unknown()
                if "开麦命令" in status:
                    self.mic_open = True
                elif "关麦命令" in status:
                    self.mic_open = False
                self._upd_mic_btn()
            elif status.startswith("audio:"):
                self._st_audio = status
            hid_st = getattr(self, "_st_hid", "")
            if "已连接" in hid_st:
                dev_txt = "Device: UR02 Connected"
            elif "未找到" in hid_st:
                dev_txt = "Device: UR02 未连接(等连接...)"
            else:
                dev_txt = hid_st
            raw_st = getattr(self, "_st_raw", "")
            parts = [dev_txt] + [p for p in (raw_st, getattr(self, "_st_ble", ""), getattr(self, "_st_audio", "")) if p]
            self.lbl_status.configure(text="    |    ".join(p for p in parts if p))
        self.after(0, _do)

    # ================= 设备选择 =================
    def _scan_devices(self):
        """后台扫描 HID 里的 UR02 (pywinusb 只能枚举到 col02 一个通道)"""

        def _do():
            found = []
            try:
                import pywinusb.hid as pwh
                for d in pwh.find_all_hid_devices():
                    p = (d.device_path or "").lower()
                    if "a4c138" not in p:
                        continue
                    if "&col01" in p:
                        col = "键盘"
                    elif "&col02" in p:
                        col = "按键"
                    elif "&col03" in p:
                        col = "空鼠"
                    else:
                        col = "?"
                    mac = ""
                    for seg in p.split("_"):
                        if seg.startswith("a4c138"):
                            mac = seg.split("&")[0][:12]
                            break
                    found.append(("UR02 (%s) [%s]" % (mac or "BT", col), d.device_path or ""))
            except Exception as e:
                found = [("扫描失败: %s" % e, "")]
            if not found:
                found = [("未找到 UR02(请连接/唤醒)", "")]

            def _apply():
                self._devices = found
                names = [n for n, _ in found]
                self.device_menu.configure(values=names)
                if self.device_menu.get() not in names or self.device_menu.get().startswith(("扫描中", "未找到")):
                    self.device_menu.set(names[0])
            self.after(0, _apply)
        threading.Thread(target=_do, daemon=True).start()

    def _on_device_pick(self, name):
        self.log("已选择设备: %s" % name)

    # ================= 槽位配置动作 =================
    def _save_profile_quiet(self):
        def _do():
            with self._saving:
                self.profile.save()
        threading.Thread(target=_do, daemon=True).start()

    def _on_profile_switch(self, name):
        self._release_all("切换配置")    # 切配置前先松开按住的键
        self.profile = Profile.load(name)
        for i in list(self.slot_ids):
            self._remove_slot_circle(i)
        # 2026-09-05 修: 原来这里有 `if s.get("hid"):` 过滤 -> 切配置后只剩已配的槽位圈,
        # 空槽位的灰虚线圈全没了(与 _build_canvas 启动时画满 18 个的逻辑不一致)。
        # _create_slot_circle 内部本就按有没有键码自动画蓝圈/灰虚线圈, 故直接全画。
        for i, s in enumerate(self.profile.slots):
            self._create_slot_circle(i, s.get("x", 0.5), s.get("y", 0.5))
        self._select_slot(0)
        self.log("已切换配置: %s" % name)

    def _save_profile_as(self):
        dialog = ctk.CTkInputDialog(title="另存配置", text="输入配置名:")
        name = dialog.get_input()
        if name and name.strip():
            self.profile.save(name.strip())
            self.profile_menu.configure(values=Profile.list_profiles())
            self.profile_menu.set(name.strip())
            self.log("配置已保存: %s" % name.strip())

    def _learn_hid(self):
        self.learning_hid = self.sel_slot
        self.learning_key = -1
        self._refresh_side()
        self.btn_learn_hid.configure(state="disabled")
        if self._learn_timeout:
            self.after_cancel(self._learn_timeout)
        self._learn_timeout = self.after(15000, self._cancel_learn)

    def _learn_key(self):
        self.learning_key = self.sel_slot
        self.learning_hid = -1
        self._refresh_side()
        self.btn_learn_key.configure(state="disabled")
        self.hook.arm(allow_mod_only=True)   # 支持只按修饰键(如 Win+Alt)的组合
        if self._learn_timeout:
            self.after_cancel(self._learn_timeout)
        self._learn_timeout = self.after(8000, self._cancel_learn)

    def _cancel_learn(self):
        if self.learning_hid >= 0:
            self.log("学遥控键超时取消 — 请先点[遥控器自检]确认遥控器在发数据")
        if self.learning_key >= 0:
            self.hook.disarm()
            self.log("学键盘键超时取消")
        self.learning_hid = -1
        self.learning_key = -1
        self.btn_learn_hid.configure(state="normal")
        self.btn_learn_key.configure(state="normal")
        self._refresh_side()

    def _set_mod(self, which, value):
        s = self.profile.slots[self.sel_slot]
        s["m1" if which == 1 else "m2"] = value
        self._save_profile_quiet()

    def _clear_slot(self):
        i = self.sel_slot
        self._slot_release(i, "清空槽位")
        s = self.profile.slots[i]
        s["hid"] = ""
        s["key"] = "无"
        s["m1"] = "无"
        s["m2"] = "无"
        s["mode"] = DEFAULT_MODE
        self._refresh_slot_style(self.sel_slot)   # 蓝圈还原成灰虚线空圈
        self._save_profile_quiet()
        self._refresh_side()
        self.log("槽位 %d 已清空" % (self.sel_slot + 1))

    def _test_send(self):
        """测试输出(开关式): 点一下=按住, 再点一下=松开。
        按住型快捷键(微信输入法 Win+Alt 这类)必须保持按住才有反应,
        所以测试也做成可关的, 按钮文字随状态变[测试关闭]。"""
        i = self.sel_slot
        if i in self._held:
            self._slot_release(i, "测试关闭")
            return
        s = self.profile.slots[i]
        if not build_vks(s["m1"], s["m2"], s["key"]):
            self.log("槽位 %d 未设置键盘键" % (i + 1))
            return
        self._slot_press(i, "测试输出")

    # ================= HID 回调 =================
    def _on_hid_key(self, code):
        """遥控键码回调: code = 4 位 Consumer Usage hex, 如 '0042'(上) / '00CF'(语音)"""
        def _do():
            hx = norm_hid(code)
            name = usage_name(hx)
            self.lbl_last.configure(text="%s  %s" % (hx, name), text_color="#22c55e")

            # ① 学键中 -> 录入到当前槽位, 圆圈出现在画布中央
            if 0 <= self.learning_hid < 18:
                i = self.learning_hid
                self.profile.slots[i]["hid"] = hx
                self.learning_hid = -1
                if self._learn_timeout:
                    self.after_cancel(self._learn_timeout)
                self.btn_learn_hid.configure(state="normal")
                self._save_profile_quiet()
                self._ensure_slot_circle(i)
                self._flash_slot(i)
                self.log("槽位 %d 录入 [%s %s] -> 拖动圆圈到对应按键位置" % (i + 1, hx, name))
                self._refresh_side()
                return

            # ② 语音键防抖: 一次按键会【同时】产生 HID 00CF 和 BLE CTRL 0x08,
            #    两条路都触发就会"开完立刻关" —— 0.45 秒内只认一次
            if hx == VOICE_USAGE:
                now = time.time()
                if now - self._voice_evt_ts < 0.45:
                    return
                self._voice_evt_ts = now

            # ③ 统一映射 (语音键 00CF 与其它键完全一样, 在槽位里自己配)
            i = self.profile.slot_by_hid(hx)
            if i < 0:
                # 语音键没配映射 -> 只当麦克风开关
                if hx == VOICE_USAGE:
                    self._toggle_mic()
                return

            s = self.profile.slots[i]
            mode = s.get("mode") or DEFAULT_MODE
            self._flash_slot(i)

            if not self.mapping_on.get():
                # 映射关了: 语音键仍可当麦克风开关
                if hx == VOICE_USAGE:
                    self._toggle_mic()
                return

            if mode == "单击":
                vks = build_vks(s["m1"], s["m2"], s["key"])
                if vks:
                    press_keys(vks)
                    release_keys(vks)
                else:
                    self.log("槽位 %d: 快捷键未设置, 无输出" % (i + 1))
                if hx == VOICE_USAGE:
                    self._toggle_mic()
            elif mode == "按住·切换":
                if i in self._held:
                    self._slot_release(i, "再按")
                else:
                    self._slot_press(i, "按下")
            else:                                  # 按住·跟随
                self._slot_press(i, "按下")
        self.after(0, _do)

    # ================= 键盘学键回调 =================
    def _on_kbd_combo(self, combo):
        def _do():
            if not (0 <= self.learning_key < 18):
                return
            i = self.learning_key
            # 拆分: 'Ctrl+Shift+S' -> m1=Ctrl m2=Shift key=S
            #       'Win+Alt'     -> m1=Win  m2=Alt  key=无 (纯修饰键, 按住型)
            parts = [p for p in combo.split("+") if p and p != "无"]
            key = "无"
            mods = parts
            if parts and parts[-1] not in PURE_MOD_NAMES:
                key = parts[-1]
                mods = parts[:-1]
            s = self.profile.slots[i]
            s["m1"] = mods[0] if len(mods) > 0 else "无"
            s["m2"] = mods[1] if len(mods) > 1 else "无"
            s["key"] = key
            self.learning_key = -1
            self.btn_learn_key.configure(state="normal")
            self._save_profile_quiet()
            self.log("槽位 %d 键盘键: %s" % (i + 1, self._combo_text(s)))
            self._refresh_side()
        self.after(0, _do)

    # ================= 遥控器自检 =================
    def _diag_remote(self):
        """一键诊断: 各通道最后数据时间 + BLE 广播扫描"""
        def _ago(t):
            if not t:
                return "从未收到数据"
            return "%.0f 秒前" % (time.time() - t)

        self.log("=== 遥控器自检 ===")
        self.log("col02 按键通道: %s (最后键码 %s %s)"
                 % (_ago(self.reader.last_time), self.reader.last_code or "-",
                    usage_name(self.reader.last_code) if self.reader.last_code else ""))
        self.log("设备路径: %s" % (self.reader.device_path or "(未打开)"))
        self.log("BLE 扫描中(8 秒)...")

        def _scan():
            res = []
            try:
                import asyncio
                import bleak

                async def _run():
                    try:
                        return await bleak.BleakScanner.discover(timeout=8.0)
                    except Exception as e:
                        return "ERR:%s" % e

                devs = asyncio.run(_run())
                if isinstance(devs, str):
                    res.append(("扫描失败", devs))
                else:
                    for d in devs or []:
                        nm = (d.name or "")
                        ad = (d.address or "").lower()
                        if "a4c138" in ad or "ugoos" in nm.lower() or "ur02" in nm.lower():
                            res.append(("发现", "%s  %s" % (d.address, nm)))
            except Exception as e:
                res.append(("扫描异常", str(e)))

            def _done():
                if not res:
                    self.log("BLE: 未扫到遥控器广播 -> 它可能已连着本机(正常) 或 已关机/没电")
                for tag, txt in res:
                    self.log("BLE %s: %s" % (tag, txt))
                    if tag == "发现":
                        self.log("  ↑ 遥控器正在广播 = 当前【没有】连上电脑, 请重新配对")
                self.log("=== 自检结束。若按键通道'从未收到数据', 说明遥控器没连上电脑/已休眠 ===")
                self.log("处理: 遥控器按 [菜单+音量+] 3秒进配对(蓝灯慢闪), Windows 蓝牙设置里添加 'Ugoos Remote'")

            self.after(0, _done)

        threading.Thread(target=_scan, daemon=True).start()

    # ================= 映射/语音开关 =================
    def _toggle_mapping(self):
        self.log("按键映射: %s" % ("开" if self.mapping_on.get() else "关"))

    def _toggle_voice(self):
        if self.voice_on.get():
            outs = list_cable_outputs()
            if not outs:
                self.log("未检测到 VB-CABLE, 请先安装虚拟声卡驱动 (VBCABLE_Driver_Pack45.zip)")
                self.voice_on.set(False)
                return
            name = self.cable_menu.get()
            idx = next((i for i, n in outs if n == name), outs[0][0])
            if not self.cable.open(idx):
                self.voice_on.set(False)
                return
            # 2026-09-06 失真排查: 每次开流清零写入计数器
            self.cable.written_samples = 0
            self.cable.write_calls = 0
            self.cable.write_errors = 0
            self.cable.skipped_calls = 0
            self.cable.last_error = ""
            self.cable.diag_capture = [] if self._diag_on else None
            self._plc_filled = 0
            self._plc_events = 0
            self._fill_trim = 0.0
            self._fill_max = 0
            self._grp_T = 0.0
            self._win_t0 = 0.0
            self._win_k = 0
            self._win_n = 0
            self._hist.clear()
            self.ble.start()
            self.log("语音桥接已启动: BLE 连接中(不自动开麦)")
            self.log("按一下遥控器语音键 = 开麦, 再按一下 = 关麦")
        else:
            if self.mic_open:
                self.ble.close_mic()
                self.mic_open = False
                self._upd_mic_btn()
            self.ble.stop()
            self.cable.close()
            self.log("语音桥接已关闭")

    def _upd_mic_btn(self):
        self.mic_btn.configure(text="关麦" if self.mic_open else "开麦")

    # ---------------- 遥控器电量 ----------------
    def _on_battery(self, pct):
        """电量回调。来自 WinRT 线程池(不是 Tk 主线程), 必须 after(0) 切回来再动控件,
        否则 customtkinter 会偶发崩溃/界面卡死。"""
        try:
            self.after(0, self._upd_battery, int(pct))
        except Exception:
            pass

    def _upd_battery(self, pct):
        if pct >= 60:
            col = "#22c55e"          # 绿
        elif pct >= 30:
            col = "#f59e0b"          # 橙
        elif pct >= 15:
            col = "#ef4444"          # 红
        else:
            col = "#ff2d2d"          # 深红: 快没电了
        self.batt_var.set("遥控器电量 %d%%" % pct)
        try:
            self.batt_label.configure(text_color=col)
        except Exception:
            pass

    def _battery_unknown(self):
        """BLE 断开时把电量显示打回未知"""
        self.batt_var.set("遥控器电量 --")
        try:
            self.batt_label.configure(text_color="#8b8f94")
        except Exception:
            pass

    # ================= 音频链路 =================
    def _on_audio(self, adpcm_bytes):
        """2026-09-05 底噪根因修复(实测取证, 见 probe_noise_result.txt):
        音频 notify 按【组】发送: 组 = 6x20B + 1x8B = 128B。
          * 每组第一个 20B 前 6 字节是序号头(00 seq 00 ...), 必须剥掉, 否则当
            ADPCM 解 -> 每组一次爆音/尖刺(之前 28681 尖刺 -> 剥头后大幅下降)
          * 8B 尾包【是音频不是控制包】(6:1 定界), v1.11 丢弃它丢 1/7 音频
          * 采样率实测 8000Hz(组间隔 32ms/128B = 7995 样本/s), 不是 16k
        """
        if not adpcm_bytes:
            return
        self._last_audio_ts = time.time()   # 任何 notify 都算流活着
        n = len(adpcm_bytes)
        if n == 8:                          # 组尾
            # 只有前面正好攒满 6 个 20B (14 + 20*5 = 114) 这一组才完整。
            # 2026-09-05 重大修复: 蓝牙会丢 8B 尾包。旧代码无条件拼上再 flush,
            # 一旦组内丢过包, abuf 长度就不对 -> 解出"半截+错位"的帧 -> 严重失真。
            if len(self._abuf) == 114:
                self._abuf += adpcm_bytes   # 122 = 完整一帧
                self._flush_audio()
            else:
                # 这组不完整 -> 整组丢弃。宁可丢 32ms 音频, 也不能解错位帧
                self._drop_frames += 1
                self._abuf = bytearray()
                self._audio_hdr = b""
            return
        if n != 20:
            return
        if len(self._abuf) >= 114:
            # 上一组的 8B 尾包没来(被蓝牙丢了) -> 先把上一组解掉再开新组。
            # 114 字节 = 只少 8B 尾(=16 个样本), 帧头状态是对的, 不会解码错位。
            self._flush_audio()
        if not self._abuf:
            # 组首 20B: 前 6 字节是序号头
            #   byte0..1 = 序号 (大端); byte2 = 0
            #   byte3..4 = 本帧编码【前】的预测值 (pred_hi, pred_lo, 有符号 16-bit LE 反着)
            #   byte5    = 本帧编码【前】的步长索引 (0..88)
            # 抓下来给 _flush_audio 用, 解码器每帧用它重置 -> 不再把上帧残差接力
            self._audio_hdr = adpcm_bytes[:6]
            adpcm_bytes = adpcm_bytes[6:]
        self._abuf += adpcm_bytes
        # 不需要 ">=120 兜底" 了: 满 6 个 20B(114) 后, 要么 8B 尾来了拼成 122 解掉,
        # 要么下一个 20B 到达时走上面的 ">=114" 分支先把上一组解掉, 永远不会积压。
        # (旧代码的 >=120 兜底正是错位帧的来源: 8B 尾丢了时 abuf 会长到 134 才 flush,
        #  那 134 字节里混着下一组没剥掉的 6B 帧头 -> 解码全乱 -> 说话严重失真)

    def _flush_audio(self):
        """2026-09-05 改: 三步修复
        ① 每帧用帧头自带状态重置解码器(不接力上帧)
        ② 解出来的 PCM 走 80Hz 高通(Direct Form I, 状态跨帧保留)
        ③ 整体 *0.5012 (-6dB) 再夹回 int16 范围(避免说话大声削顶)
        """
        if not self._abuf:
            return
        payload = bytes(self._abuf)
        self._abuf = bytearray()
        hdr = self._audio_hdr
        self._audio_hdr = b""

        # ① 帧头状态 (缺头就退化到当前 decoder 状态, 不崩)
        try:
            if len(hdr) >= 6:
                # hdr[3] = pred_hi, hdr[4] = pred_lo, hdr[5] = step_index
                # 有符号 16-bit LE: 拼起来再按字节序翻一下
                pred = (hdr[3] << 8) | hdr[4]
                if pred & 0x8000:
                    pred -= 0x10000
                idx = hdr[5] & 0xFF
                samples = self.decoder.decode_with_state(payload, pred, idx)
            else:
                samples = self.decoder.decode(payload)
        except Exception:
            samples = self.decoder.decode(payload)

        # 诊断采样: 高通【前】的原始解码结果
        if self._diag_on:
            self._diag_raw.extend(samples)

        # ② 80Hz 高通 (返 float)
        samples = self._hp.process(samples)

        # ③ -6dB 缩放 + 限幅到 int16
        g = self._gain
        out = [max(-32768, min(32767, int(round(s * g)))) for s in samples]

        if self._diag_on:
            self._diag_out.extend(out)
            self._diag_frames += 1

        # ---- 滑动窗口实测组间隔 (2s 窗口, 突发包带不偏) ----
        now = time.time()
        if not self._stream_t0:
            self._stream_t0 = now
            self._frame_count = 0
            self._win_t0 = now
            self._win_n = 0
        self._frame_count += 1
        self._win_n += 1
        if self._frame_count > 30:      # 全程均值, 只用于诊断显示
            self._frame_interval = (now - self._stream_t0) / self._frame_count
        if now - self._win_t0 >= 2.0 and self._win_n > 5:
            T = (now - self._win_t0) / self._win_n
            self._win_t0 = now
            self._win_n = 0
            self._win_k += 1
            # !! 第 1 个窗口丢弃: 开流头两秒 BLE 还在热身, 测出来是 60ms 级的慢间隔,
            #    照它补会把缓冲灌爆(v1.24 实测补了全程 15.9%, 应该只有 4.7%)
            if self._win_k >= 2:
                T = max(0.020, min(0.080, T))       # 限幅 20~80ms
                self._grp_T = (T if not self._grp_T
                               else (self._grp_T * 0.35 + T * 0.65))

        # ---- 丢包补偿: 按实测组间隔算出真实缺口, 用基频周期复制补回来 ----
        # 2026-09-06: 遥控器每组只送来 244 样本(30.5ms), 真实组间隔若为 43.6ms,
        # 则每组之间【真的丢了 105 个样本 = 13ms】。老办法只插 12 点直线斜坡(补 1.5ms),
        # 剩下 11.5ms 全靠缓冲硬扛 -> 周期性断流 -> 这就是"哒哒哒"的来源。
        if self._prev_last is not None and out:
            T = self._grp_T or self._frame_interval
            base = int(round(T * self.audio_rate)) - len(out)
            # 补点量闭环: 缓冲水位高了就少补(积分+比例), 音高不受影响。
            # 没有这层反馈, 组间隔一变(环境漂移)就会一直补多/补少, 缓冲灌爆或抽干。
            berr = self.cable.buffer_error()
            self._fill_trim = max(-80.0, min(160.0,
                                             self._fill_trim + FILL_KI * berr))
            hole = int(round(base - FILL_KP * berr - self._fill_trim))
            # 封顶: 单帧最多补 FILL_MAX 个(10ms), 超出的缺口宁可留白(断流会做渐变)
            hole = max(0, min(hole, FILL_MAX, len(out)))
            if hole > 0:
                fill = self._plc_fill(hole)
                if hole > self._fill_max:
                    self._fill_max = hole
                real = np.asarray(out, dtype=np.float64)
                m = min(40, hole, len(real))        # 末端 5ms 与真实帧首交叉淡化
                if m > 0:
                    w = np.linspace(0.0, 1.0, m)
                    fill[-m:] = fill[-m:] * (1.0 - w) + real[:m] * w
                out = [int(max(-32768, min(32767, v))) for v in fill] + out
                self._plc_filled += hole
                self._plc_events += 1
        if out:
            self._prev_last = out[-1]
            self._hist.extend(out)

        # 播放速率锁定标称 8k(实测遥控器是真 8k 采样, 音高才对);
        # 供需缺口已由 PLC 补平, PLL 只吸收抖动(±6%)
        self.cable.write_pcm(out, self.audio_rate)

    # ---------------- 丢包补偿 (PLC) ----------------
    def _plc_fill(self, n):
        """补出 n 个被射频丢掉的样本 (实现见 audio_out.plc_fill)"""
        return plc_fill(self._hist, n, int(self.audio_rate))

    # ---------------- 音频诊断 ----------------
    def _save_diag(self):
        """把本次推流的 PCM 存成两份 wav (exe 旁 diag/):
        <时间>_raw.wav = 解码后、高通/增益【前】
        <时间>_out.wav = 高通+增益【后】, 即真正送进 VB-CABLE 的数据
        两者对比即可判断失真出在解码环节还是后处理环节; 同时打印丢帧数验证丢包假设。
        """
        if not (self._diag_on and self._diag_out):
            self._diag_raw = []
            return
        try:
            base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                    else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            d = os.path.join(base, "diag")
            os.makedirs(d, exist_ok=True)
            ts = datetime.datetime.now().strftime("%H%M%S")
            for tag, buf in (("raw", self._diag_raw), ("out", self._diag_out)):
                if not buf:
                    continue
                p = os.path.join(d, "%s_%s.wav" % (ts, tag))
                arr = array.array("h", [max(-32768, min(32767, int(v))) for v in buf])
                w = wave.open(p, "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(int(self.audio_rate))
                w.writeframes(arr.tobytes())
                w.close()
            # 2026-09-06: 存【真正写进声卡的 48k 数据】—— 判断失真出在重采样(程序)还是声卡链路
            cap = getattr(self.cable, "diag_capture", None)
            if cap:
                try:
                    p = os.path.join(d, "%s_48k.wav" % ts)
                    buf = np.concatenate(cap) if len(cap) > 1 else cap[0]
                    w = wave.open(p, "wb")
                    w.setnchannels(1); w.setsampwidth(2)
                    w.setframerate(int(getattr(self.cable, "_dev_rate", 48000)))
                    w.writeframes(buf.astype("<i2").tobytes())
                    w.close()
                except Exception as e:
                    self.log("48k 诊断保存失败: %s" % e)
            with open(os.path.join(d, "%s_info.txt" % ts), "w", encoding="utf-8") as f:
                f.write("时间 %s\n" % datetime.datetime.now())
                f.write("版本 %s\n" % APP_VERSION)
                f.write("帧数 %d\n" % self._diag_frames)
                f.write("丢帧(整组丢弃) %d\n" % self._drop_frames)
                f.write("解码采样率 %s Hz\n" % self.audio_rate)
                f.write("CABLE 设备 %s @ %s Hz\n"
                        % (getattr(self.cable, "device_name", "?"),
                           getattr(self.cable, "_dev_rate", "?")))
                # 2026-09-06: 写入实测计数 —— 判断是否有帧级丢写(断口/爆音)
                c = self.cable
                expected = self._diag_frames * int(getattr(c, "_dev_rate", 48000)) \
                    * 244 // max(int(self.audio_rate), 1)
                f.write("写入调用次数 %d\n" % getattr(c, "write_calls", 0))
                f.write("写入失败次数 %d\n" % getattr(c, "write_errors", 0))
                f.write("流未打开丢弃 %d\n" % getattr(c, "skipped_calls", 0))
                f.write("实写样本数 %d\n" % getattr(c, "written_samples", 0))
                f.write("应写样本数 %d\n" % expected)
                f.write("实测组间隔 %.2f ms (总时长/总帧数)\n" % (self._frame_interval * 1000))
                tot = self._plc_filled + self._diag_frames * 244
                f.write("PLC补点 样本 %d / 帧 %d (占全程 %.1f%%), 单帧峰值 %d\n"
                        % (self._plc_filled, self._plc_events,
                           100.0 * self._plc_filled / max(tot, 1), self._fill_max))
                f.write("缓冲水位 目标 %.2fs / 峰值 %.2fs\n"
                        % (getattr(c, "TARGET_BUF", 0),
                           getattr(c, "max_buf_samples", 0)
                           / max(int(getattr(c, "_dev_rate", 48000)), 1)))
                f.write("PLL 缓冲偏差 %+.2f (0=锁定, ±1=饱和)\n" % getattr(c, "_pll_err", 0.0))
                f.write("缓冲断流次数 %d\n" % getattr(c, "underruns", 0))
                f.write("缓冲峰值样本 %d\n" % getattr(c, "max_buf_samples", 0))
                f.write("最后错误 %s\n" % getattr(c, "last_error", ""))
            self.log("诊断: 已存 diag/%s_raw.wav + out.wav  (%d 帧, 丢帧 %d)"
                     % (ts, self._diag_frames, self._drop_frames))
        except Exception as e:
            self.log("诊断保存失败: %s" % e)
        self._diag_raw = []
        self._diag_out = []
        self._diag_frames = 0
        if self._diag_on:
            self.cable.diag_capture = []
        self._drop_frames = 0

    def _reset_stream_state(self):
        """停流/掐流后复位解码与补点状态(不碰 mic_open/按键槽位)。"""
        try:
            self.decoder.reset()
        except Exception:
            pass
        self._prev_last = None          # 帧间补点状态复位
        self._hist.clear()              # PLC 历史清零(不能拿上一段的波形补下一段)
        self._grp_T = 0.0
        self._win_k = 0
        self._fill_trim = 0.0
        self._last_frame_ts = 0.0
        self._stream_t0 = 0.0           # 归零 -> 下一段音频首帧会重新置位计时
        self._frame_count = 0
        self._win_t0 = 0.0
        self._win_n = 0

    def _audio_watchdog(self):
        """流看门狗: 推流中若音频包停止 >1.2s, 判定遥控器已自己停流
        (2026-09-05 实测: 遥控器停流只灭蓝灯【不发任何通知】, 程序等不到第二下按键/
        AUDIO_STOP) —— 只能靠检测音频流本身停止来松键关麦。

        v1.31 (2026-09-07): 区分【固件 20s 掐流】与【真停流】——
          * 本次推流已持续 >=15s 后无声中断(无 0x00 命令) = 固件 562 帧掐流(~19.7s)
            -> 不结束录音, 自动 0D->0C 重开麦续录(约 2s 停顿), 长说话不再被 20s 打断
          * 否则(用户松键/异常中断) = 原逻辑: 松键关麦
        """
        try:
            if self.mic_open and self._last_audio_ts:
                idle = time.time() - self._last_audio_ts
                streamed = (time.time() - self._stream_t0) if self._stream_t0 else 0.0
                if idle > 1.2:
                    if streamed >= 15.0:
                        # 掐流特征: 持续推流 >=15s 后无声(掐流是帧计数到点, 不发命令)
                        self.log("检测到固件 20s 掐流(推流 %.0fs 后无包) -> 自动重开麦续录(约2s停顿)"
                                 % streamed)
                        self._reset_stream_state()
                        self._save_diag()
                        if self.ble.reopen_mic():
                            self.log_status("ble:自动重开麦续录中...")
                    else:
                        self.log("检测到音频流停止(%.1fs 无包) -> 松开语音键" % idle)
                        for i in [i for i in list(self._held)
                                  if norm_hid(self.profile.slots[i].get("hid")) == VOICE_USAGE]:
                            self._slot_release(i, "流停止")
                        self.mic_open = False
                        self._reset_stream_state()
                        self._save_diag()
                        self._upd_mic_btn()
        except Exception:
            pass
        self.after(500, self._audio_watchdog)

    def _on_ctrl(self, data):
        """控制通道回调(在 BLE 线程) —— 只取字节, 所有 UI 操作交给主线程"""
        try:
            b = bytes(data) if data else b""
        except Exception:
            return
        if not b:
            return
        self.after(0, lambda: self._on_ctrl_ui(b))

    def _on_ctrl_ui(self, b):
        """解析 opcode: AUDIO_START 拿 codec 定采样率; MIC_BUTTON=语音键"""
        try:
            op = b[0]
            h = b.hex(" ").upper()
            if op == 0x04:                       # AUDIO_START: 04 <v?> <codec> <?>
                # 实测(2026-09-05): 本机收到的 AUDIO_START 是单字节 04(无 codec 字段),
                # 流实测为 8kHz —— 无 codec 字段时默认按 8k, 不再默认 16k
                codec = b[2] if len(b) > 2 else 1
                rate = CODEC_RATES.get(codec, AUDIO_SAMPLE_RATE)
                if rate != self.audio_rate:
                    self.audio_rate = rate
                    self.log("音频采样率: %d Hz (codec=0x%02X)" % (rate, codec))
                self.log_status("ble:音频开始 %dHz (推流中)" % rate)
                self.mic_open = True
            elif op == 0x00:                     # AUDIO_STOP
                self.log_status("ble:音频停止")
                # 2026-09-05 实测: 推流中再按语音键, 遥控器【只发 AUDIO_STOP 自己停流】,
                # 不发 08 也不发 HID 00CF —— 必须在这里松开语音键槽位的快捷键(放最前, 防后续异常跳过)
                for i in [i for i in list(self._held)
                          if norm_hid(self.profile.slots[i].get("hid")) == VOICE_USAGE]:
                    self._slot_release(i, "遥控器停流")
                self.mic_open = False
                try:
                    self.decoder.reset()
                except Exception:
                    pass
                self._prev_last = None          # 帧间补点状态复位
                self._hist.clear()              # PLC 历史清零
                self._grp_T = 0.0
                self._win_t0 = 0.0
                self._win_n = 0
                self._win_k = 0
                self._fill_trim = 0.0
                self._stream_t0 = 0.0
                self._save_diag()
            elif op == 0x08:                     # MIC_BUTTON: 遥控器语音键
                self._on_hid_key(VOICE_USAGE)    # 与 HID 00CF 同一条路径(内部防抖)
                return
            elif op == 0x0B:
                self.log_status("ble:能力应答 %s" % h)
            elif op == 0xFF:                     # 按键后 ~2 秒的心跳, 每 100ms 一条 -> 静默
                return
            else:
                self.log_status("ble:ctrl %s" % h)
            self._upd_mic_btn()
        except Exception:
            pass

    def _on_hid_release(self, code):
        """遥控器松开某个键 (HID 全 0 报文) —— [按住·跟随]模式下松开快捷键"""
        hx = norm_hid(code)

        def _do():
            i = self.profile.slot_by_hid(hx)
            if i < 0:
                return
            if (self.profile.slots[i].get("mode") or DEFAULT_MODE) == "按住·跟随":
                self._slot_release(i, "松开遥控键")
        self.after(0, _do)

    def _mic_on(self):
        if self.mic_open:
            return True
        if not self.voice_on.get():
            self.log("语音桥接没开 -> 不推流(没装 VB-CABLE 或声卡打开失败, 看上面日志)")
            return False
        if not self.ble.connected:
            self.log("BLE 还没连上遥控器(桥接刚开要等 1~2 秒), 稍后再按语音键")
            return False
        if not self.ble.open_mic():
            self.log("开麦失败: BLE 未就绪")
            return False
        self.mic_open = True
        self._upd_mic_btn()
        self.log("开麦: 遥控器推流中 (软件麦克风选 CABLE Output)")
        return True

    def _mic_off(self):
        if not self.mic_open:
            return
        self.ble.close_mic()
        self.mic_open = False
        self._upd_mic_btn()
        self.log("关麦")

    def _toggle_mic(self):
        """顶栏[开麦/关麦]按钮: 只管麦克风, 不碰快捷键"""
        if not self.voice_on.get():
            self.log("语音桥接没开(没装 VB-CABLE 或声卡打开失败, 看上面日志)")
            return
        if self.mic_open:
            self._mic_off()
        else:
            self._mic_on()

    # ================= 退出 =================
    # ================= 托盘 / 关闭行为 =================
    def _settings_path(self):
        """设置文件: exe 旁(打包) / 项目根(源码), 与 profiles 同级"""
        if getattr(sys, "frozen", False):
            return os.path.join(os.path.dirname(sys.executable), "settings.json")
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

    def _load_settings(self):
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                import json as _json
                d = _json.load(f)
            if isinstance(d.get("tray_min"), bool):
                self.tray_min.set(d["tray_min"])
        except Exception:
            pass

    def _save_settings(self):
        import json as _json
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                _json.dump({"tray_min": bool(self.tray_min.get())}, f)
        except Exception:
            pass

    def _ensure_tray(self):
        """创建托盘图标(线程内 run)。图标用遥控器图缩略。"""
        if self._tray is not None:
            return
        try:
            img = Image.open(resource_path(os.path.join("assets", "remote.png")))
            img = img.resize((64, 64), Image.LANCZOS)
            self._tray_icon_img = img
        except Exception:
            self._tray_icon_img = Image.new("RGB", (64, 64), "#1d4ed8")
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", self._tray_show, default=True),
            pystray.MenuItem("退出", self._tray_quit),
        )
        self._tray = pystray.Icon("ur02_vibe", self._tray_icon_img, APP_NAME, menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_show(self, icon=None, item=None):
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        self.after(0, self._show_from_tray)

    def _show_from_tray(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_quit(self, icon=None, item=None):
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        self.after(0, self._really_quit)

    def _to_tray(self):
        """最小化到托盘(不退出, 后台继续映射/语音)"""
        self.withdraw()
        self._ensure_tray()

    def _on_close(self):
        if self.tray_min.get():
            self.log("已最小化到托盘(可在托盘图标恢复/退出); 取消勾选可恢复'直接关闭'")
            self._to_tray()
            return
        self._really_quit()

    def _really_quit(self):
        try:
            self._release_all("退出")   # 退出前松开所有按住的键, 避免卡住不放
            self.reader.stop()
            if self.mic_open:
                self.ble.close_mic()
                time.sleep(0.3)  # 给关麦命令留发送时间
            self.ble.stop()
            self.cable.close()
            self.hook.disarm()
            self._save_profile_quiet()
            self._save_settings()
        finally:
            self.destroy()


_MUTEX_HANDLE = None   # 必须全程持有引用, 只用局部变量会被回收导致锁失效


def _bring_existing_to_front():
    """把已经在跑的那个实例(多半缩在托盘里)显示出来并提到前台。"""
    try:
        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        found = []

        def _cb(hwnd, lParam):
            try:
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if "UR02" in buf.value:
                        found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
        if found:
            hwnd = found[0]
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE: 从托盘/最小化恢复
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def _ensure_single_instance():
    """单实例锁: 多个 UR02-Vibe.exe 同时跑会抢同一个蓝牙连接,
    导致 BLE 连接-断开往复(flapping)。第二个实例直接退出并提示。

    2026-09-07 重修(旧版时灵时不灵, 用户能开出第二个来):
      * 旧版用 kernel32.GetLastError() 取错误码 —— ctypes 下这个值不可靠: CreateMutexW
        返回后到读错误码之间, Python 解释器自己的 Win32 调用会把 last error 冲掉,
        于是"明明已经有一个在跑"却判定成"没有"。
        改用 ctypes.WinDLL(..., use_last_error=True) + ctypes.get_last_error()。
      * 句柄存到模块级全局变量, 不随函数返回被回收。
      * 名字加 Local\\ 前缀, 明确限定在本用户会话内。
      * 第二个实例顺手把已在运行的窗口从托盘里唤出来, 免得用户以为没打开又去点。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
    except Exception:
        return
    global _MUTEX_HANDLE
    _MUTEX_HANDLE = k32.CreateMutexW(None, 0, "Local\\UR02VibeAssistantSingleInstance")
    err = ctypes.get_last_error()
    if not _MUTEX_HANDLE or err == 183:  # 183 = ERROR_ALREADY_EXISTS
        woke = _bring_existing_to_front()
        try:
            import tkinter.messagebox as tkmb
            root = tk.Tk()
            root.withdraw()
            tkmb.showinfo(
                "UR02 Vibe 助手",
                "程序已经在运行了。\n\n"
                + ("已经帮你把它从托盘里显示出来了。\n\n" if woke else
                   "它可能缩在屏幕右下角的托盘图标里, 点一下就能打开。\n\n")
                + "同时开两个会抢同一个蓝牙连接, 导致 BLE 反复断连,\n"
                  "所以这一次不会重复启动。",
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(1)


def run():
    _ensure_single_instance()
    app = App()
    app.mainloop()
