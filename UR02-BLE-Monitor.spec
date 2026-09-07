# -*- mode: python ; coding: utf-8 -*-
# UR02 BLE 监视器打包 —— winrt 处理照抄 UR02-Flasher.spec 已验证方案
from PyInstaller.utils.hooks import collect_submodules
import os
import winrt

datas = []
binaries = []
hiddenimports = ['cffi']

# winrt 3.x: 把整个 site-packages/winrt 目录作为数据拷进运行时 _MEI/winrt,
# 由 Python 从临时目录直接 import(含 .pyd 原生绑定 + winrt_runtime.dll)。
# 不再把 winrt 放进 PYZ, 避免被冻结导入器遮蔽。
winrt_dir = winrt.__path__[0]
assert os.path.isdir(winrt_dir), winrt_dir
datas.append((winrt_dir, 'winrt'))

a = Analysis(
    ['ur02_ble_monitor.py'],
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
    name='UR02-BLE-Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,        # 控制台工具, 需要实时看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
