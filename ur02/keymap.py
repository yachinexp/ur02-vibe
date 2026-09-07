# -*- coding: utf-8 -*-
"""键盘输出: SendInput 注入 + 低级键盘钩子学键 (移植自已验证的 C# KeyTools.cs)
注意铁律: 所有 Ptr 类 API 显式声明 argtypes/restype, 用 c_void_p 代替 LONG_PTR。
学键钩子跑在专用消息泵线程, 捕获结果经队列回调。
"""
import ctypes
import ctypes.wintypes as wt
import threading

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
# 铁律: 必须显式声明 restype=c_void_p, 否则 x64 下 64 位模块句柄被截断成 32 位 int,
# 导致 SetWindowsHookEx 失败 (GLE=126 ERROR_MOD_NOT_FOUND)
_GetModuleHandleW = _kernel32.GetModuleHandleW
_GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_GetModuleHandleW.restype = ctypes.c_void_p

# ---------- VK 名称表 (与 C# 版一致) ----------
_VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
}
_SPECIAL_CHARS = {0xBC: ",", 0xBE: ".", 0xBA: ";", 0xDB: "[", 0xDD: "]",
                  0xBF: "/", 0xBD: "-", 0xBB: "=", 0xDC: "\\", 0xDE: "'"}
_MOD_VKS = {"Ctrl": 0x11, "Shift": 0x10, "Alt": 0x12, "Win": 0x5B, "Fn": None, "Esc": 0x1B, "Tab": 0x09}
# 修饰/功能下拉可选项 (学键时按下 Ctrl 等修饰键不会立即捕获, 等主键)
MOD_CHOICES = ["无", "Ctrl", "Alt", "Shift", "Win", "Fn", "Esc", "Tab"]


def vk_to_name(vk):
    vk = int(vk)
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if vk in _SPECIAL_CHARS:
        return _SPECIAL_CHARS[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x7B:
        return "F%d" % (vk - 0x6F)
    if 0x60 <= vk <= 0x69:
        return "Num%d" % (vk - 0x60)
    return "VK%02X" % vk


def name_to_vk(name):
    if not name:
        return 0
    if name in _VK_NAMES.values():
        for v, n in _VK_NAMES.items():
            if n == name:
                return v
    for v, n in _SPECIAL_CHARS.items():
        if n == name:
            return v
    if len(name) == 1:
        c = name.upper()
        if "0" <= c <= "9" or "A" <= c <= "Z":
            return ord(c)
    if name.startswith("F") and name[1:].isdigit():
        n = int(name[1:])
        if 1 <= n <= 12:
            return 0x6F + n
    if name.startswith("Num") and name[3:].isdigit():
        return 0x60 + int(name[3:])
    return 0


def combo_to_vks(combo):
    """'Ctrl+Shift+S' -> ([vk_mods...], vk_main); 修饰部分支持中文名'无'过滤"""
    if not combo or combo == "无":
        return [], 0
    parts = [p for p in combo.split("+") if p and p != "无"]
    if not parts:
        return [], 0
    mods = []
    for p in parts[:-1]:
        vk = _MOD_VKS.get(p) or name_to_vk(p)
        if vk:
            mods.append(vk)
    main = name_to_vk(parts[-1])
    return mods, main


def build_vks(m1, m2, key):
    """把 (修饰键1, 修饰键2, 主键) 展开成要按下的 VK 列表。

    与 combo_to_vks 的区别: 支持【只有修饰键】的组合(如 Win+Alt), 主键可为"无"。
    """
    vks = []
    for m in (m1, m2, key):
        if not m or m == "无":
            continue
        vk = _MOD_VKS.get(m) or name_to_vk(m)
        if vk and vk not in vks:
            vks.append(vk)
    return vks


# ---------- SendInput 注入 ----------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    """必须声明: union 的真实大小由最大的 MOUSEINPUT(32B) 决定,
    否则 ctypes 算出的 sizeof(INPUT)=32 而不是 40, SendInput 会直接失败(返回0)。"""
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT_IU(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("iu", _INPUT_IU)]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002


def _key_input(vk, up=False):
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.iu.ki.wVk = vk
    if up:
        inp.iu.ki.dwFlags = _KEYEVENTF_KEYUP
    return inp


def _send(vks, up=False, up_order=False):
    """注入一组按键的按下(up=False)或抬起(up=True)。

    up_order=True: 抬起时保持与 vks 相同顺序(Win 先抬, 后面的修饰键垫着防开始菜单);
    up_order=False 且 up=True: 反序抬起(常规 tap 用, 主键先抬)。
    """
    if not vks:
        return False
    if up:
        order = list(vks) if up_order else list(reversed(vks))
    else:
        order = list(vks)
    arr = (len(order) * _INPUT)(*[_key_input(v, up=up) for v in order])
    n = _user32.SendInput(len(order), ctypes.byref(arr), ctypes.sizeof(_INPUT))
    return n == len(order)


def press_keys(vks):
    """按住一组键不松 (微信输入法 Win+Alt 这类"按住说话"场景)"""
    return _send(vks, up=False)


def release_keys(vks):
    """松开之前按住的那组键。

    顺序必须是【与按下相同】的顺序: 保证 Win 不最后一个抬 —— Win-up 单独出现
    会弹开始菜单/被 shell 吃掉(Win 抬起时若有其它键还按着则视为组合结束, 安全)。
    !! 2026-09-05 修复: 原代码 _send(vks, up=False) 把松开发成了【再次按下】,
    Win+Alt 永远松不开 -> 微信语音输入法第二次按取消不掉。
    """
    return _send(vks, up=True, up_order=True)


def send_combo(combo):
    """注入组合键: 修饰键依次按下 -> 主键按下 -> 主键抬起 -> 修饰键反序抬起"""
    mods, main = combo_to_vks(combo)
    if not main:
        return False
    downs = [_key_input(v) for v in mods] + [_key_input(main)]
    ups = [_key_input(main, up=True)] + [_key_input(v, up=True) for v in reversed(mods)]
    arr = (len(downs + ups) * _INPUT)(*downs, *ups)
    n = _user32.SendInput(len(downs + ups), ctypes.byref(arr), ctypes.sizeof(_INPUT))
    return n == len(downs + ups)


# ---------- 低级键盘钩子 (学键) ----------
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x100
WM_SYSKEYDOWN = 0x104
WM_KEYUP = 0x101
WM_SYSKEYUP = 0x105


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)

_SetWindowsHookEx = _user32.SetWindowsHookExW
_SetWindowsHookEx.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, wt.DWORD]
_SetWindowsHookEx.restype = ctypes.c_void_p
_UnhookWindowsHookEx = _user32.UnhookWindowsHookEx
_UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_CallNextHookEx = _user32.CallNextHookEx
_CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
_CallNextHookEx.restype = ctypes.c_ssize_t
_GetMessage = _user32.GetMessageW
_GetMessage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT]
_GetAsyncKeyState = _user32.GetAsyncKeyState
_PostThreadMessage = _user32.PostThreadMessageW
_PostThreadMessage.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]

WM_QUIT = 0x0012
# LL 钩子报的修饰键 VK 是"左右区分"码 (如按 Ctrl 报 0xA2=VK_LCONTROL 而非 0x11),
# 必须一并列入, 否则修饰键会被误判成主键。
_MOD_ALIAS = {
    0x10: "Shift", 0xA0: "Shift", 0xA1: "Shift",
    0x11: "Ctrl", 0xA2: "Ctrl", 0xA3: "Ctrl",
    0x12: "Alt", 0xA4: "Alt", 0xA5: "Alt",
    0x5B: "Win", 0x5C: "Win",
}
_is_modifier = tuple(_MOD_ALIAS)


class KeyboardHook:
    """学键盘键: arm() 后捕获下一个按键组合, 一次性。

    两种结果:
      - 修饰键 + 普通键  -> 'Ctrl+Shift+S' (按下普通键即刻返回)
      - 只有修饰键        -> 'Win+Alt'      (修饰键松开时返回, 用于"按住说话"类快捷键)

    回调在钩子线程触发, 转交 on_combo(str)。
    """

    def __init__(self, on_combo, on_error=None):
        self.on_combo = on_combo
        self.on_error = on_error or (lambda s: None)
        self._armed = threading.Event()
        self._thread = None
        self._tid = 0
        self._proc = None  # 防 GC
        self._hook = None
        self._mods_seen = []

    def arm(self, allow_mod_only=True):
        self._allow_mod_only = allow_mod_only
        self._mods_seen = []
        if self._thread and self._thread.is_alive():
            self._armed.set()
            return True
        self._armed.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def disarm(self):
        self._armed.clear()
        if self._tid:
            _PostThreadMessage(self._tid, WM_QUIT, 0, 0)

    def _mods_now(self):
        mods = []
        if _GetAsyncKeyState(0x11) & 0x8000:
            mods.append("Ctrl")
        if _GetAsyncKeyState(0x10) & 0x8000:
            mods.append("Shift")
        if _GetAsyncKeyState(0x12) & 0x8000:
            mods.append("Alt")
        if _GetAsyncKeyState(0x5B) & 0x8000:
            mods.append("Win")
        return mods

    def _vks_to_names(self, vks):
        return [_MOD_ALIAS.get(v) or vk_to_name(v) for v in vks]

    def _run(self):
        self._tid = _kernel32.GetCurrentThreadId()
        self._proc = _HOOKPROC(self._callback)
        self._hook = _SetWindowsHookEx(WH_KEYBOARD_LL, self._proc, _GetModuleHandleW(None), 0)
        if not self._hook:
            self.on_error("键盘钩子安装失败")
            return
        msg = ctypes.create_string_buffer(48)  # MSG
        while _GetMessage(msg, None, 0, 0) > 0:
            pass
        _UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._tid = 0

    def _callback(self, ncode, wparam, lparam):
        try:
            if ncode >= 0 and self._armed.is_set():
                info = ctypes.cast(ctypes.c_void_p(lparam),
                                   ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = info.vkCode
                mod = vk in _is_modifier

                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if mod and getattr(self, "_allow_mod_only", True):
                        # 吞掉修饰键(防止 Win 弹开始菜单), 记下来等后续键
                        if vk not in self._mods_seen:
                            self._mods_seen.append(vk)
                        return 1
                    if mod:
                        return _CallNextHookEx(self._hook, ncode, wparam, lparam)
                    # 普通键 -> 组合完成
                    self._armed.clear()
                    mods = self._mods_seen or []
                    names = self._vks_to_names(mods) if mods else self._mods_now()
                    names.append(vk_to_name(vk))
                    self._fire(names)
                    return 1                      # 吞键

                if wparam in (WM_KEYUP, WM_SYSKEYUP) and mod:
                    # 纯修饰键组合: 第一个修饰键抬起 -> 组合完成 (如 Win+Alt)
                    if getattr(self, "_allow_mod_only", True) and self._mods_seen:
                        self._armed.clear()
                        self._fire(self._vks_to_names(self._mods_seen))
                        return 1
        except Exception as e:
            try:
                self.on_error("钩子回调异常: %r" % (e,))
            except Exception:
                pass
        return _CallNextHookEx(self._hook, ncode, wparam, lparam)

    def _fire(self, names):
        seen, out = set(), []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        try:
            self.on_combo("+".join(out))
        except Exception:
            pass
