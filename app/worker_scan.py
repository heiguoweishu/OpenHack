"""硬件嗅探 worker（独立子进程）。

调用 vendor 里的 Hardware-Sniffer 生成标准硬件报告 + ACPI 原始表，
把它 print 出来的过程（一堆 [====] 进度条）转成结构化事件。
"""
import argparse
import os
import re
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bus as busmod  # noqa: E402
from app import paths  # noqa: E402

_NEWEST = None

# 扫描阶段在整条进度条上占多少（剩下 30→100 归分析）。worker_efi.ANALYZE_START 与之对应。
SCAN_SPAN = 30


def run(argv, out):
    """见 worker_efi.run 的说明：frozen 模式下不能起子进程，必须进程内执行。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(paths.REPORTS))
    args = ap.parse_args(argv)

    bus = busmod.EventStream(out)
    saved = busmod.install_tee(bus)
    exit_code = 0
    try:
        _run(args.out, bus)
    except Exception as e:
        bus.log(f"{type(e).__name__}: {e}", "error")
        bus.log(" | ".join(traceback.format_exc().strip().splitlines()[-3:])[:500], "error")
        exit_code = 1
    finally:
        busmod.restore_tee(saved)
    bus.done(exit_code)
    return exit_code


def _run(out_dir, bus):
    # 进度条是"扫描 + 分析"共用的一条（前端扫完立刻接着分析），
    # 所以扫描只占前 30%，剩下的留给分析。否则扫到 100 之后分析又从 5 开始，
    # 看起来就像"硬件检测明明完了，进度条却掉回去/卡在 9x%"。
    steps = ["准备输出目录", "采集硬件信息", "导出 ACPI 表", "完成"]
    st = busmod.Steps(bus, steps, span=SCAN_SPAN)

    st.enter(0)
    os.makedirs(out_dir, exist_ok=True)
    bus.progress(5, "准备输出目录")

    st.enter(1)
    bus.progress(10, "启动硬件采集引擎")
    engine = str(paths.HSNIFF)
    if not os.path.isfile(os.path.join(engine, "HardwareSniffer.py")):
        raise RuntimeError(f"找不到 Hardware-Sniffer：{engine}")
    bus.log(f"引擎目录：{engine}", "info")

    _run_sniffer(out_dir, engine, bus)
    bus.progress(26, "硬件采集结束")

    report = os.path.join(out_dir, "Report.json")
    acpi = os.path.join(out_dir, "ACPI")
    if not os.path.isfile(report):
        raise RuntimeError(f"未生成报告文件：{report}")
    if not os.path.isdir(acpi):
        bus.log("未生成 ACPI 目录（后续 ACPI 补丁将缺少原始表支持）", "warn")
        acpi = ""

    st.enter(3)
    n_tables = len([f for f in os.listdir(acpi) if f.endswith(".aml")]) if acpi else 0
    bus.progress(SCAN_SPAN, "扫描完成，接着分析兼容性")
    bus.log(f"报告已生成：{report}", "ok")
    if acpi:
        bus.log(f"ACPI 原始表 {n_tables} 份：{acpi}", "ok")

    bus.result({
        "reportPath": report,
        "acpiDir": acpi,
        "acpiTables": n_tables,
        "size": os.path.getsize(report),
    })
    st.finish_all()


# Hardware-Sniffer 自己画的进度条是 "[======    ] 44% 0.06/0.14 MB" 这种同一行 \r 刷新，
# 逐帧转发会刷屏。这里只保留每个阶段的最新百分比。
_PCT = re.compile(r"(\d{1,3})\s*%")
_GATHER = re.compile(r"\[\*\]\s*Gathering (.+?) information", re.IGNORECASE)
_STEP_TEXT = {
    "unique chipset": "识别芯片组",
    "motherboard": "读取主板信息",
    "bios": "读取 BIOS 信息",
    "cpu": "读取处理器信息",
    "gpu": "读取显卡信息",
    "monitor": "读取显示器信息",
    "network": "读取网络设备",
    "sound": "读取声卡信息",
    "usb controllers": "读取 USB 控制器",
    "input devices": "读取输入设备",
    "storage controllers": "读取存储控制器",
    "biometric information": "读取生物识别设备",
    "bluetooth information": "读取蓝牙设备",
    "sd controller information": "读取读卡器",
    "system devices": "读取系统设备",
}


def _run_sniffer(out_dir, engine, bus):
    """进程内跑 Hardware-Sniffer。

    不起子进程有两个原因：一是打包后 sys.executable 是 exe 本身，起子进程会重新解压
    并弹控制台窗口；二是它的 print 输出正好被我们架在 stdout 上的 Tee 接住，
    自动变成前端的实时日志，比转发管道更省事。

    两个必要的准备：
      * chdir 到引擎目录 —— 它用 os.getcwd() 定位 acpidump.exe；
      * CoInitialize —— 子线程里不初始化 COM 的话 wmi.WMI() 会直接抛错。
    """
    from app import wincom

    if engine not in sys.path:
        sys.path.insert(0, engine)
    previous = os.getcwd()
    try:
        os.chdir(engine)
        with wincom.com():
            import HardwareSniffer as HS  # noqa: PLC0415

            sniffer = HS.HardwareSniffer(out_dir, rich_format=False)
            sniffer.hardware_info.hardware_collector()
            sniffer.export_hardware_report()
            sniffer.dump_acpi_tables()
    finally:
        os.chdir(previous)


def _emit(line, bus):
    if not line.strip():
        return
    m = _GATHER.search(line)
    if m:
        key = m.group(1).strip().lower()
        bus.step(_STEP_TEXT.get(key, f"采集 {m.group(1).strip()}"))
        return
    if line.startswith("Please wait for download") or "Downloading from" in line:
        bus.log(line.strip(), "info")
        bus.step("下载 acpidump 工具")
        return
    if _PCT.search(line) and "[=" in line:
        v = _PCT.search(line).group(1)
        bus.progress(None, f"下载 acpidump {v}%")
        return
    if "Dumping tables" in line:
        bus.step("导出 ACPI 表")
        bus.log(line.strip(), "info")
        return
    if line.startswith("Report saved") or "Report saved to" in line:
        bus.log(line.strip(), "ok")
        return
    if "hardware information collection complete" in line.lower():
        bus.log("硬件信息采集完成", "ok")
        return
    if "acpi tables dumped successfully" in line.lower():
        bus.log("ACPI 表导出完成", "ok")
        return
    bus.log(line.strip()[:300], "info")


def main():
    return run(sys.argv[1:], sys.__stdout__)


if __name__ == "__main__":
    sys.exit(main())
