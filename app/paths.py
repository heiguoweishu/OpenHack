"""统一的路径定义。

所有产物都落在工具自己的 data/ 目录里，绝不往别人的盘上乱写。

PyInstaller 打包后代码和静态资源在只读的 `_MEIPASS`（每次启动还会重新解压），
所以**可写**的产物必须换到 `%LOCALAPPDATA%\\GlassHack`，否则：
  * OpenCore / kext 的下载缓存每次启动都丢，等于每次全量重下；
  * 生成的 EFI 在关掉窗口后就没了。
"""
import os
import pathlib
import sys

_FROZEN = bool(getattr(sys, "frozen", False))

if _FROZEN:
    ROOT = pathlib.Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    _local = pathlib.Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))
    BASE = _local / "OpenHack"
    # 从旧名字（GlassHack）平滑迁移：老用户手里已经有下载好的 OpenCore / kext 缓存，
    # 直接整个目录搬过来，免得升级一次就要把上百 MB 重新下一遍。
    _legacy = _local / "GlassHack"
    if not BASE.exists() and _legacy.exists():
        try:
            os.replace(str(_legacy), str(BASE))
        except OSError:
            pass
else:
    ROOT = pathlib.Path(__file__).resolve().parent.parent          # GlassHack/
    BASE = ROOT

APP = ROOT / "app"
VENDOR = ROOT / "vendor"
WEB = ROOT / "web"

OCS = VENDOR / "ocsimplify"          # OpCore-Simplify 引擎
HSNIFF = VENDOR / "hsniff"           # Hardware-Sniffer 引擎

DATA = BASE / "data"
REPORTS = DATA / "reports"           # 硬件报告 + ACPI 原始表
RESULTS = DATA / "results"           # 生成的 EFI
CACHE = DATA / "cache"               # OCK_Files（OpenCore / kext / iasl / macserial）
LOGS = DATA / "logs"
STATE = DATA / "state.json"          # 用户偏好

for _d in (DATA, REPORTS, RESULTS, CACHE, LOGS):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def resource(*parts):
    """随包分发的只读资源（打包后位于只读的 _MEIPASS 内）。

    必须和 DATA 分开：DATA 是运行时可写目录（%LOCALAPPDATA%\\OpenHack\\data），
    而随 exe 打进去的静态文件（比如 NullMoth 的 NVIDIA 支持表）在 _MEIPASS 下，
    两者根本不是同一个路径 —— 把支持表当运行时数据读，打包后必然读成空表，
    于是"表里有这张卡"变成"没有可用显卡"，整条 N 卡支持就静默失效了。
    """
    return ROOT.joinpath(*parts)


def ensure(*parts):
    p = DATA.joinpath(*parts)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def where_data_lives():
    """给界面显示"产物放在哪"。"""
    return str(DATA)
