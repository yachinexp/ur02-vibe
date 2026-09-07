# -*- mode: python ; coding: utf-8 -*-
# UR02 OTA 刷机工具打包 (复用 UR02-Vibe.spec 已验证方案, 修复 winrt 命名空间子包缺失)
from PyInstaller.utils.hooks import collect_all, collect_submodules
import os
import winrt

datas = []
binaries = []
hiddenimports = ['cffi']

# winrt 3.x: winrt.windows 是命名空间包, 其下纯 py 包(如 winrt.windows.devices.bluetooth,
# 内 __init__.py 做 from winrt._winrt_xxx import *) PyInstaller 的 collect_submodules /
# 分析期 import 都会漏掉, 运行时 ModuleNotFoundError。最稳做法: 把整个 site-packages/winrt
# 目录作为数据拷进运行时 _MEI/winrt, 由 Python 直接从临时目录 import(含 .pyd 原生绑定 +
# 其同目录的 winrt_runtime.dll / MSVCP140.dll)。不再把 winrt 放进 PYZ, 避免被冻结导入器遮蔽。
winrt_dir = winrt.__path__[0]   # winrt 是命名空间包, __file__ 为 None, 用 __path__
assert os.path.isdir(winrt_dir), winrt_dir
datas.append((winrt_dir, 'winrt'))

# customtkinter 含主题子模块 + darkdetect 依赖
t = collect_all('customtkinter'); datas += t[0]; binaries += t[1]; hiddenimports += t[2]
t = collect_all('darkdetect'); datas += t[0]; binaries += t[1]; hiddenimports += t[2]
# 文件选择对话框是函数内惰性 import tkinter.filedialog, 收全 tkinter 子模块
hiddenimports += collect_submodules('tkinter')

a = Analysis(
    ['ur02_ota_flasher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='UR02-Flasher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # 与 UR02-Vibe 一致: 关 UPX, 避免压缩原生 DLL 导致静默异常
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # = --windowed, GUI 程序不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
