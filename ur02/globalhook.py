# -*- coding: utf-8 -*-
"""全局低级键盘钩子监听 (col01 兜底采集通道)

背景 (实测):
  - UR02 蓝牙 HID col01 在 RawInput 里登记为 RIM_TYPEKEYBOARD, 但实测遥控器
    未发数据时两条通道都是空的, 无法确认 RawInput 一定能收到。
  - 本模块用 WH_KEYBOARD_LL 兜底: 只要遥控器的键被系统识别成键盘输入, 就能捕获。

优点: 命中映射时可"吞掉"原键, 从根上避免双重触发。
缺点: 无法区分来源设备, 物理键盘按同一个键同样会触发 (已在 UI 提示)。
"""
import ctypes
import ctypes.wintypes as wt
import threading

from .keymap import send_combo

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x100
WM_SYSKEYDOWN = 0x104
WM_QUIT = 0x0012
_is_modifier = (0x10, 0x11, 0x12, 0x5B, 0x5C)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

_SetWindowsHookEx = _user32.SetWindowsHookExW
_SetWindowsHookEx.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wt.DWORD]
_SetWindowsHookEx.restype = ctypes.c_void_p
# 铁律: 必须显式声明 restype 为 c_void_p, 否则 x64 下 64 位模块句柄被截成 32 位 int,
# SetWindowsHookEx 返回 NULL / GLE=126 (ERROR_MOD_NOT_FOUND)
_GetModuleHandleW = _kernel32.GetModuleHandleW
_GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_GetModuleHandleW.restype = ctypes.c_void_p
_UnhookWindowsHookEx = _user32.UnhookWindowsHookEx
_UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_CallNextHookEx = _user32.CallNextHookEx
_CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
_CallNextHookEx.restype = ctypes.c_ssize_t
_GetMessage = _user32.GetMessageW
_GetMessage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT]
_PostThreadMessage = _user32.PostThreadMessageW
_PostThreadMessage.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]


class GlobalKeyMonitor:
    """常驻 WH_KEYBOARD_LL 监听。

    on_key(code_str, vk): 捕获到按键 (学习模式下回调一次)
    mapping: {vk:int -> combo:str} 命中则注入 combo 并吞掉原键
    """

    def __init__(self, on_key, on_status=None):
        self.on_key = on_key
        self.on_status = on_status or (lambda s: None)
        self.enabled = False       # 是否启用映射输出
        self.learning = False      # 学习模式: 捕获下一个键
        self.mapping = {}
        self.swallow = True        # 命中映射时吞掉原键
        self.last_vk = 0
        self.last_time = 0.0
        self._thread = None
        self._tid = 0
        self._proc = None
        self._hook = None
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._tid:
            _PostThreadMessage(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def set_learning(self, on):
        self.learning = bool(on)

    def update_mapping(self, mapping):
        with self._lock:
            self.mapping = dict(mapping or {})

    # ---------- 内部 ----------
    def _run(self):
        import time
        self._tid = _kernel32.GetCurrentThreadId()
        self._proc = _HOOKPROC(self._callback)
        self._hook = _SetWindowsHookEx(WH_KEYBOARD_LL, self._proc, _GetModuleHandleW(None), 0)
        if not self._hook:
            self.on_status("hook:钩子安装失败 GLE=%d" % _kernel32.GetLastError())
            return
        self.on_status("hook:全局键盘监听中")
        msg = ctypes.create_string_buffer(48)
        while _GetMessage(msg, None, 0, 0) > 0:
            pass
        _UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._tid = 0
        self.on_status("hook:已停止")

    def _callback(self, ncode, wparam, lparam):
        try:
            if ncode < 0 or wparam not in (WM_KEYDOWN, WM_SYSKEYDOWN):
                return _CallNextHookEx(self._hook, ncode, wparam, lparam)
            info = ctypes.cast(ctypes.c_void_p(lparam),
                               ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            import time
            self.last_vk = vk
            self.last_time = time.time()

            # 学习模式: 捕获第一个非修饰键
            if self.learning:
                if vk in _is_modifier:
                    return _CallNextHookEx(self._hook, ncode, wparam, lparam)
                self.learning = False
                try:
                    self.on_key("KB:%02X" % vk, vk)
                except Exception:
                    pass
                return 1  # 吞掉, 避免学键时原键生效

            # 映射模式
            if self.enabled:
                with self._lock:
                    combo = self.mapping.get(vk)
                if combo:
                    try:
                        send_combo(combo)
                    except Exception:
                        pass
                    if self.swallow:
                        return 1
        except Exception:
            pass
        return _CallNextHookEx(self._hook, ncode, wparam, lparam)
