"""WMI / COM 的并发与单元模型保护。

三个坑，都踩过：

1. **不能并发。** HTTP 服务是多线程的，浏览器一打开就同时打四五个接口，
   几条线程一起 `wmi.WMI()` 会互相踩，表现是查询莫名其妙返回空，
   界面上"本机硬件"全变成"—"，而且时好时坏、极难查。
   WMI 的 COM 对象不是线程安全的，也没有便宜的线程本地方案（每次新建很慢），
   所以用一把全局锁串起来 —— 查询本身是毫秒级的，串行的代价远小于"偶发查不到"。

2. **不能用 STA。** `pythoncom.CoInitialize()` 建的是单线程单元，它要求线程有
   Windows 消息泵才能处理 COM 调用。打包成 GUI 子系统的 exe 后线程没有泵，
   于是 WMI 查询直接挂死（连接能建立、响应永远不来）。
   必须 `CoInitializeEx(0)` 建多线程单元。

3. **用完要释放。** 每个线程进门前 CoInitialize、出门 CoUninitialize，
   否则会抛 "CoInitialize has not been called"。
"""
import threading
from contextlib import contextmanager

WMI_LOCK = threading.RLock()

# COINIT_MULTITHREADED。STA 需要消息泵，见上文第 2 条。
_MTA = 0x0


@contextmanager
def com():
    """进入一个 COM 已就绪、且独占 WMI 的临界区。"""
    pythoncom = None
    try:
        import pythoncom  # type: ignore  # noqa: PLC0415
        # CoInitializeEx 而不是 CoInitialize：后者建 STA，GUI 子系统下会挂死
        pythoncom.CoInitializeEx(_MTA)
    except Exception:
        pythoncom = None
    try:
        with WMI_LOCK:
            yield
    finally:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


@contextmanager
def wmi_session():
    """拿到一个可用的 wmi 客户端（COM 就绪且独占）。"""
    with com():
        import wmi  # noqa: PLC0415
        yield wmi.WMI()
