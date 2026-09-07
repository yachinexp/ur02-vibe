# -*- coding: utf-8 -*-
"""UR02 按键采集 —— 对齐原版 UR02+ V2 实现 (反编译确认)

原版做法:
  1. pywinusb.hid.find_all_hid_devices() 枚举, 按 MAC 串匹配 device_path, 取第一个
  2. dev.open() + set_raw_data_handler 收原始报文
  3. 按键指纹 = 报文里的 Consumer Usage

实测结论(2026-09-05):
  * pywinusb 只能枚举到 UR02 的 col02 一个通道(col01/col03 被系统独占)
  * UR02 的【全部】按键都走 col02, 报文 6 字节: [reportID=2, usage_lo, usage_hi, 0,0,0]
    -> usage = lo | hi<<8 (16bit Consumer Page); 松开报文 = 除 reportID 外全 0
  * 所以根本不存在"col01 采集不到"的问题 —— 键全在 col02
"""
import threading
import time

import pywinusb.hid as hid

from .consts import HID_FILTER

_MAC6 = HID_FILTER[:6].lower()  # 'a4c138'


def find_remote_device():
    """枚举 HID, 返回 UR02 设备对象(未打开)。找不到返回 None。"""
    try:
        devs = hid.find_all_hid_devices()
    except Exception:
        return None
    for d in devs:
        if _MAC6 in (d.device_path or "").lower():
            return d
    return None


class RemoteKeyReader:
    """后台线程: 打开 col02 收报文, 解析成 4 位 usage hex 回调 on_key('0042')。

    on_key 每个物理按下只触发一次(按住期间的重复报文被吞掉)。
    """

    def __init__(self, on_key, on_status=None, on_release=None):
        self.on_key = on_key
        self.on_release = on_release      # (code) 遥控器【松开】某个键时回调
        self.on_status = on_status or (lambda s: None)
        self._stop = threading.Event()
        self._thread = None
        self._dev = None
        self.last_code = ""
        self.last_time = 0.0
        self.last_raw = []
        self.device_path = ""
        self._down = False

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """只置停止标志, 设备由采集线程自己关(pywinusb 要求同线程 open/close)"""
        self._stop.set()

    def _close_dev(self):
        d, self._dev = self._dev, None
        if d is not None:
            try:
                d.close()
            except Exception:
                pass

    def _loop(self):
        while not self._stop.is_set():
            dev = find_remote_device()
            if dev is None:
                self.on_status("hid:未找到遥控器(等连接)")
                if self._stop.wait(2.0):
                    return
                continue
            try:
                dev.open()
                dev.set_raw_data_handler(self._on_data)
            except Exception:
                self.on_status("hid:打开失败(重试中)")
                if self._stop.wait(2.0):
                    return
                continue
            self._dev = dev
            self.device_path = dev.device_path or ""
            self.on_status("hid:已连接, 监听按键中")
            try:
                while not self._stop.is_set():
                    if not dev.is_plugged():
                        break
                    if self._stop.wait(1.0):
                        break
            finally:
                self._close_dev()
            self.on_status("hid:连接断开, 重连中")
            if self._stop.wait(1.5):
                return

    # ---------- 报文解析 ----------
    def _on_data(self, data):
        """pywinusb 回调: data 为整条输入报告的字节列表, data[0] = report id

        2026-09-05 实测修正: 遥控器在某些状态(如语音/推流)下可能【不发】全 0 松开报文,
        旧逻辑 _down 卡死 -> 第二次同键按下被当成"按住重复"静默吞掉(用户实测踩中)。
        对策: 同码重复报文若距上次按下超过 1 秒, 判定松开报文丢失, 按新按下处理。
        """
        try:
            if not data or len(data) < 2:
                return
            payload = data[1:]
            now = time.time()
            if not any(payload):          # 全 0 = 松开
                if self._down and self.last_code and self.on_release:
                    code = self.last_code
                    self._down = False
                    try:
                        self.on_release(code)
                    except Exception:
                        pass
                self._down = False
                return
            lo = payload[0]
            hi = payload[1] if len(payload) >= 2 else 0
            usage = lo | (hi << 8)
            if usage == 0:
                return
            code = "%04X" % usage
            if self._down and code == self.last_code:
                if now - self.last_time < 1.0:
                    return                # 真·按住重复(1 秒内) -> 吞
                # 超过 1 秒的同码报文 = 松开报文丢了, 先补一个松开事件
                self._down = False
                if self.on_release:
                    try:
                        self.on_release(code)
                    except Exception:
                        pass
            self._down = True
            self.last_code = code
            self.last_time = now
            self.last_raw = list(data)
            self.on_key(code)
        except Exception:
            pass
