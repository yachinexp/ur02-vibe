# -*- coding: utf-8 -*-
"""UR02 键盘通道采集 (Windows Raw Input)
根因: UR02 蓝牙 HID 有 3 个 collection:
  col01 = 标准键盘   (系统 kbdclass 独占, CreateFile GLE=5 打不开)
  col02 = Consumer   (音量/多媒体/语音键, 可直读)
  col03 = 鼠标/空鼠  (系统独占)
按方向/OK/返回等键走 col01, 只读 col02 永远收不到 -> 学键超时。
Raw Input 可在系统消费的同时旁路捕获, 且能按设备路径过滤出 UR02。
键码格式: "KB:%02X" (VKey); col02 直读保持 "%02X" (consumer usage)。
"""
import ctypes
import threading
from ctypes import POINTER, Structure, byref, c_int, c_ulong, c_ushort, c_void_p, wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
QS_ALLINPUT = 0x04FF
INFINITE = 0xFFFFFFFF

# GetRawInputData 的 cbSizeHeader 必须是 sizeof(RAWINPUTHEADER):
#   x64 = 24 (dwType4 + dwSize4 + hDevice8 + wParam8), x86 = 16
# 传错(曾传 sizeof(void*)=8) 会导致取数据失败 -> 一个键都收不到
RID_HEADER_SIZE = 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16


class RAWINPUTDEVICELIST(Structure):
    _fields_ = [("hDevice", c_void_p), ("dwType", c_ulong)]


class RAWINPUTDEVICE(Structure):
    _fields_ = [("usUsagePage", c_ushort), ("usUsage", c_ushort),
                ("dwFlags", c_ulong), ("hwndTarget", c_void_p)]


class WNDCLASSW(Structure):
    _fields_ = [("style", c_ulong), ("lpfnWndProc", c_void_p), ("cbClsExtra", c_int),
                ("cbWndExtra", c_int), ("hInstance", c_void_p), ("hIcon", c_void_p),
                ("hCursor", c_void_p), ("hbrBackground", c_void_p),
                ("lpszMenuName", c_void_p), ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", c_void_p)]


class MSG(Structure):
    _fields_ = [("hwnd", c_void_p), ("message", c_ulong), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", c_ulong),
                ("pt", wintypes.POINT), ("lPrivate", c_ulong)]


_RegisterClassW = user32.RegisterClassW
_RegisterClassW.argtypes = [POINTER(WNDCLASSW)]
_RegisterClassW.restype = c_ushort
_CreateWindowExW = user32.CreateWindowExW
_CreateWindowExW.argtypes = [c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, c_ulong,
                             c_int, c_int, c_int, c_int, c_void_p, c_void_p, c_void_p, c_void_p]
_CreateWindowExW.restype = c_void_p
_GetMessageW = user32.GetMessageW
_GetMessageW.argtypes = [POINTER(MSG), c_void_p, c_ulong, c_ulong]
_TranslateMessage = user32.TranslateMessage
_DispatchMessageW = user32.DispatchMessageW
_DefWindowProcW = user32.DefWindowProcW
_DefWindowProcW.argtypes = [c_void_p, c_ulong, wintypes.WPARAM, wintypes.LPARAM]
_DefWindowProcW.restype = wintypes.LPARAM
_DestroyWindow = user32.DestroyWindow
_DestroyWindow.argtypes = [c_void_p]
_PostThreadMessageW = user32.PostThreadMessageW
_PostThreadMessageW.argtypes = [c_ulong, c_ulong, wintypes.WPARAM, wintypes.LPARAM]
WM_QUIT = 0x0012

_RegisterRawInputDevices = user32.RegisterRawInputDevices
_RegisterRawInputDevices.argtypes = [POINTER(RAWINPUTDEVICE), c_ulong, c_ulong]
_GetRawInputData = user32.GetRawInputData
_GetRawInputData.argtypes = [c_void_p, c_ulong, c_void_p, POINTER(c_ulong), c_ulong]
_GetRawInputData.restype = c_ulong
_GetRawInputDeviceInfoW = user32.GetRawInputDeviceInfoW
_GetRawInputDeviceInfoW.argtypes = [c_void_p, c_ulong, c_void_p, POINTER(c_ulong)]
_GetRawInputDeviceInfoW.restype = c_ulong
_GetRawInputDeviceList = user32.GetRawInputDeviceList
_GetRawInputDeviceList.argtypes = [POINTER(RAWINPUTDEVICELIST), POINTER(c_ulong), c_ulong]
_GetRawInputDeviceList.restype = c_ulong


class RawInputReader:
    """后台消息窗口线程: 捕获 UR02 键盘通道按键, 回调 on_key('KB:xx')"""

    def __init__(self, on_key, on_status=None, dev_filter="a4c138", capture_all=False):
        self.on_key = on_key
        self.on_status = on_status or (lambda s: None)
        self.dev_filter = dev_filter.lower()
        self.capture_all = capture_all   # 调试用: 不过滤设备, 收所有键盘事件
        self._stop = threading.Event()
        self._thread = None
        self._tid = 0
        self._hwnd = None
        self._devs = set()        # UR02 的 hDevice 集合(启动时静态枚举)
        self._dev_names = {}      # hDevice -> 设备名(调试显示)
        self.last_dev = 0         # 最近一次按键来自哪个 hDevice
        self._present = False   # 最近一次是否见到 UR02 键盘事件
        self._last_seen = 0.0

    @property
    def last_dev_name(self):
        """最近一次按键的设备名简短形式, 如 'UR02 Col01' / '未知设备'"""
        nm = self._dev_names.get(self.last_dev, "")
        if not nm:
            return "未知设备"
        low = nm.lower()
        if self.dev_filter in low:
            col = "Col01" if "&col01" in low else ("Col02" if "&col02" in low
                                                   else ("Col03" if "&col03" in low else "?"))
            return "UR02 %s" % col
        if "vid_" in low:
            i = low.find("vid_")
            return nm[i:i + 17]
        return nm[:40]

    def restart(self):
        """重启监听(改设备过滤/调试模式后调用)"""
        self.stop()
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None
        self.start()

    def set_capture_all(self, on):
        """切换'捕获所有键盘'调试模式"""
        on = bool(on)
        if self.capture_all == on and self._thread and self._thread.is_alive():
            return
        self.capture_all = on
        self.restart()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        tid = self._tid
        if tid:
            _PostThreadMessageW(tid, WM_QUIT, 0, 0)

    # ---- 设备枚举 ----
    def _build_device_set(self):
        """静态枚举 RawInput 设备, 找出 UR02 各 collection 的 hDevice"""
        self._devs = set()
        self._dev_names = {}
        num = c_ulong(0)
        r = _GetRawInputDeviceList(None, byref(num), ctypes.sizeof(RAWINPUTDEVICELIST))
        if r == 0xFFFFFFFF or not num.value:
            return
        arr = (RAWINPUTDEVICELIST * num.value)()
        n = _GetRawInputDeviceList(arr, byref(num), ctypes.sizeof(RAWINPUTDEVICELIST))
        for i in range(n):
            h = arr[i].hDevice
            if not h:
                continue
            nm = ""
            size = c_ulong(0)
            _GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, None, byref(size))
            if 0 < size.value < 1024:
                nb = ctypes.create_unicode_buffer(size.value)
                if _GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, nb, byref(size)) != 0xFFFFFFFF:
                    nm = nb.value or ""
            if nm:
                self._dev_names[int(h)] = nm
            if self.dev_filter in nm.lower():
                self._devs.add(int(h))
        return self._devs

    # ---- 内部 ----
    def _loop(self):
        import time
        self._tid = kernel32.GetCurrentThreadId()
        wndproc_cb = _WndProcType(self._wndproc)
        cls_name = "UR02VibeRawWnd"
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(wndproc_cb, c_void_p)
        wc.lpszClassName = cls_name
        _GetModuleHandleW = kernel32.GetModuleHandleW
        _GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        _GetModuleHandleW.restype = ctypes.c_void_p   # x64 必须, 否则句柄被截断
        wc.hInstance = _GetModuleHandleW(None)
        atom = _RegisterClassW(byref(wc))
        if not atom:
            self.on_status("raw:注册窗口类失败")
            return
        # message-only window
        HWND_MESSAGE = c_void_p(-3)
        self._hwnd = _CreateWindowExW(0, cls_name, "ur02vibe_raw", 0,
                                      0, 0, 0, 0, HWND_MESSAGE, None, wc.hInstance, None)
        if not self._hwnd:
            self.on_status("raw:创建消息窗口失败")
            return

        rids = (RAWINPUTDEVICE * 2)(
            RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK, self._hwnd),   # keyboard
            RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, self._hwnd),   # mouse (识别空鼠, 暂不用)
        )
        if not _RegisterRawInputDevices(rids, 2, ctypes.sizeof(RAWINPUTDEVICE)):
            self.on_status("raw:注册RawInput失败 GLE=%d" % kernel32.GetLastError())
            return

        self._build_device_set()
        if self.capture_all:
            self.on_status("raw:监听中(调试:全部键盘)")
        elif self._devs:
            self.on_status("raw:键盘通道监听中(UR02 已识别 %d 通道)" % len(self._devs))
        else:
            self.on_status("raw:监听中(未识别到 UR02, 按遥控器试试)")
        msg = MSG()
        while not self._stop.is_set():
            r = _GetMessageW(byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == WM_INPUT:
                self._handle_wm_input(msg.lParam)
            _TranslateMessage(byref(msg))
            _DispatchMessageW(byref(msg))
        if self._hwnd:
            _DestroyWindow(self._hwnd)
        self.on_status("raw:已停止")

    def _wndproc(self, hwnd, umsg, wparam, lparam):
        # 大部分消息交给默认处理; WM_INPUT 已由消息循环处理
        return _DefWindowProcW(hwnd, umsg, wparam, lparam)

    def _handle_wm_input(self, hraw):
        import time
        size = c_ulong(0)
        _GetRawInputData(hraw, RID_INPUT, None, byref(size), RID_HEADER_SIZE)
        if size.value <= 0 or size.value > 512:
            return
        buf = ctypes.create_string_buffer(size.value)
        copied = c_ulong(size.value)
        n = _GetRawInputData(hraw, RID_INPUT, buf, byref(copied), RID_HEADER_SIZE)
        if n == 0xFFFFFFFF or n <= 0 or copied.value < 32:
            return

        h_dev = int.from_bytes(buf.raw[8:16], 'little')

        # 设备过滤: 优先用启动时静态枚举到的 UR02 句柄集合
        if not self.capture_all:
            if self._devs:
                if h_dev not in self._devs:
                    return
            else:
                # 回退: 按设备名过滤
                name_size = c_ulong(0)
                _GetRawInputDeviceInfoW(ctypes.c_void_p(h_dev),
                                        RIDI_DEVICENAME, None, byref(name_size))
                dev = ""
                if 0 < name_size.value < 512:
                    nb = ctypes.create_unicode_buffer(name_size.value)
                    _GetRawInputDeviceInfoW(ctypes.c_void_p(h_dev),
                                            RIDI_DEVICENAME, nb, byref(name_size))
                    dev = nb.value or ""
                if self.dev_filter not in dev.lower():
                    return

        self.last_dev = h_dev
        self._present = True
        self._last_seen = time.time()
        # x64: header dwType@0 dwSize@4 hDevice@8 wParam@16, data@24
        dw_type = int.from_bytes(buf.raw[0:4], 'little')
        if dw_type != 1:  # RIM_TYPEKEYBOARD
            return
        data = buf.raw[24:40]
        make_code = int.from_bytes(data[0:2], 'little')
        flags = int.from_bytes(data[2:4], 'little')
        vkey = int.from_bytes(data[6:8], 'little')
        msg_id = int.from_bytes(data[8:12], 'little')
        if flags & 1:  # RI_KEY_KEYBREAK = keyup
            return
        if msg_id == 0x0104:  # WM_SYSKEYDOWN (Alt)
            pass
        code = vkey if vkey else (make_code | 0x80 if make_code else 0)
        if not code:
            return
        try:
            self.on_key("KB:%02X" % code)
        except Exception:
            pass


_WndProcType = ctypes.WINFUNCTYPE(ctypes.c_longlong, c_void_p, ctypes.c_uint,
                                  wintypes.WPARAM, wintypes.LPARAM)
