# -*- mode: python ; coding: utf-8 -*-
"""GlassHack 的 PyInstaller 打包配置。

用 --onefile 出单个 exe。要带的东西：

  vendor/   两个开源引擎（OpCore-Simplify / Hardware-Sniffer），运行时靠 sys.path 动态 import
  web/      液态玻璃界面（HTML/CSS/JS）
  data/nvidia_gsp_ids.json   NullMoth 的 NVIDIA 支持表

注意 data/ 里只打这一个 json —— 报告、EFI、缓存都是运行时产物，
而且打包后 data 已改道到 %LOCALAPPDATA%\\GlassHack，不该塞进 exe。
"""

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('vendor', 'vendor'),
        ('web', 'web'),
        ('data/nvidia_gsp_ids.json', 'data'),
    ],
    hiddenimports=[
        # WMI：静态分析看不出 wmi.WMI() 里的动态导入，必须显式列
        'wmi',
        'win32com',
        'win32com.client',
        'pythoncom',
        'pywintypes',
        'win32api',
        'win32con',
        # worker 由 launcher 按命令行参数分发（exe 的 bootloader 不认 -m），
        # 静态分析看不到这些 import，不列进去打包后会是 "No module named app.worker_xxx"
        'app.worker_scan',
        'app.worker_efi',
        'app.worker_install',
        'app.autopilot',
        'app.nullmoth',
        'app.mirror',
        'app.recovery',
        'app.install',
        'app.wincom',
        'app.bus',
        'app.ps',
        # vendor/ 下的两个引擎是通过 sys.path 动态加载的，PyInstaller 的静态分析
        # 看不到它们的依赖，只能手动补齐。缺一个就是运行时的 ModuleNotFoundError
        # （曾经就缺 queue，扫描直接崩）。
        'queue',
        'threading',
        'hashlib',
        'binascii',
        'gzip',
        'zlib',
        'struct',
        'uuid',
        'plistlib',
        'ctypes',
        'ctypes.wintypes',
        'winreg',
        'getpass',
        'argparse',
        'zipfile',
        'socket',
        'ssl',
        'tempfile',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 这些用不到，剔掉能省下几十 MB
        'tkinter',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'test',
        'unittest',
        'sqlite3',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    # 后端产物改名 GlassHackCore：真正的 GlassHack.exe 是 native/ 编出来的原生外壳，
    # 两者都往 dist/ 写，同名会把原生 exe 整个覆盖掉 —— 必须靠名字区分开。
    name='OpenHackCore',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 无控制台窗口：双击之后只出现界面本身，不再跟着一个黑框
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
