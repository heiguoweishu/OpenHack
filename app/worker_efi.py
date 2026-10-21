"""EFI 生成 worker（独立子进程）。

流水线直接跑 vendor 里 OpCore-Simplify 的原代码，本文件只负责：
  * 把它那些交互提问换成确定性答案（见 autopilot.py）；
  * 接管 stdout，把它 print 的进度转成结构化事件给 UI；
  * 把产物挪到 data/results 并做完整性检查。

用法：
    python -m app.worker_efi --mode analyze --report <Report.json> [--acpi <dir>]
    python -m app.worker_efi --mode build   --report <Report.json> [--json <plan.json>]
"""
import argparse
import json
import os
import shutil
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import autopilot  # noqa: E402
from app import bus as busmod  # noqa: E402

# 分析阶段从整条进度条的哪里开始（前面 0-30 归硬件扫描，见 worker_scan.SCAN_SPAN）。
ANALYZE_START = 32
from app import paths  # noqa: E402


def _write_pid():
    pass


def run(argv, out):
    """在给定输出流上执行。

    打包成 onefile 后 sys.executable 就是 exe 本身，拿 subprocess 起 worker 等于
    每次重新解压整个程序、还弹一个控制台窗口。所以 frozen 模式下由 server 在子线程里
    直接调这个函数，源码模式和打包模式走同一条代码路径。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=("analyze", "build"))
    ap.add_argument("--report", required=True)
    ap.add_argument("--acpi", default="")
    ap.add_argument("--json", default="", help="build 模式下的构建参数 JSON")
    args = ap.parse_args(argv)

    # stdout 必须尽早接管，引擎一开始就会 print
    bus = busmod.EventStream(out)
    saved = busmod.install_tee(bus)
    exit_code = 0
    try:
        if args.mode == "analyze":
            do_analyze(args, bus)
        else:
            do_build(args, bus)
    except Exception as e:
        bus.log(f"{type(e).__name__}: {e}", "error")
        tb = traceback.format_exc().strip().splitlines()
        bus.log(" | ".join(tb[-3:])[:600], "error")
        exit_code = 1
    finally:
        busmod.restore_tee(saved)
    bus.done(exit_code)
    return exit_code


# ------------------------------------------------------------------ analyze

def _load_report(path, bus, mods):
    """读入硬件报告并用引擎自带的校验器过一遍。

    ReportValidator.validate_report 收的是文件路径（它自己会读），且返回布尔值；
    具体的错误信息要看它的 errors / warnings 列表。
    """
    if not os.path.isfile(path):
        raise RuntimeError(f"硬件报告不存在：{path}")
    validator = mods["report_validator"].ReportValidator()
    ok = validator.validate_report(path)
    for w in (getattr(validator, "warnings", None) or []):
        bus.log(f"报告警告：{w}", "warn")
    if not ok:
        for e in (getattr(validator, "errors", None) or []):
            bus.log(f"报告错误：{e}", "error")
        raise RuntimeError(f"硬件报告校验未通过：{path}")
    utils = mods["utils"].Utils()
    data = utils.read_file(path)
    if not data:
        raise RuntimeError(f"无法解析硬件报告：{path}")
    return data


def do_analyze(args, bus):
    # 进度条和扫描共用一条（前端扫完立刻接着分析）：扫描占 0-30，分析从 30 接着走到 100。
    # 之前分析从 5 重新开始、最后只推到 95，表现出来就是"硬件检测完了却卡在 9x%"。
    bus.step("加载 OpCore-Simplify 引擎", 0, 4)
    bus.progress(ANALYZE_START, "加载引擎")
    mods = autopilot.load_engine()
    # 必须在任何 check_* 之前接管交互 —— 兼容性检查内部就会提问
    autopilot.install_answers(mods, {})
    autopilot.patch_nvidia(mods, bus)
    # 修掉 10 秒超时 + 加镜像兜底，否则大一点的 kext 在国内网络环境下基本下不动
    from app import mirror
    mirror.patch(mods, bus)

    bus.step("读取并校验硬件报告", 1, 4)
    bus.progress(44, "校验报告结构")
    report_path = args.report
    acpi_dir = args.acpi or os.path.join(os.path.dirname(report_path), "ACPI")
    report = _load_report(report_path, bus, mods)
    bus.progress(55, "报告校验通过")

    bus.step("分析硬件兼容性", 2, 4)
    bus.progress(62, "判定各设备支持的 macOS 范围")
    bus.log(f"机型：{report.get('Motherboard', {}).get('Name', '未知')}", "info")
    bus.log(f"处理器：{report.get('CPU', {}).get('Processor Name', '未知')} "
            f"（{report.get('CPU', {}).get('Codename', '未知')}）", "info")
    for name, props in (report.get("GPU") or {}).items():
        bus.log(f"显卡：{name} [{props.get('Device ID', '')}] {props.get('Device Type', '')}", "info")

    report, native_version, oclp_version, gpu_blocked = _run_compatibility(
        report, mods, bus, allow=True)
    from app import nullmoth as _nv0
    _log_compatibility(bus, native_version, oclp_version,
                       third_party_gpu=bool(_nv0.cards(report)))

    bus.step("整理配置选项", 3, 4)
    bus.progress(86, "汇总可用 macOS 版本")

    # 注意 check_compatibility 返回的是 (min, max)，很多"越低越旧"的判定都以这个顺序为准。
    # 这里统一转成 min/max 两个名字，后面所有计算都基于它，避免再一次踩反。
    native_min = str(native_version[0]) if native_version else None
    native_max = str(native_version[-1]) if native_version else None
    oclp_min = str(oclp_version[-1]) if oclp_version else None
    oclp_max = str(oclp_version[0]) if oclp_version else None

    majors = []
    try:
        lo = min(int(native_min[:2]), int(oclp_min[:2]) if oclp_min else 99)
        hi = max(int(native_max[:2]), int(oclp_max[:2]) if oclp_max else 0)
        if 13 <= lo <= hi <= 30:
            majors = list(range(lo, hi + 1))
    except Exception:
        pass

    # N 卡这条路必须把版本收窄。NullMoth 驱动只对 darwin 24（macOS 15 Sequoia）和
    # darwin 25（macOS 26 Tahoe）分别构建了 NVAccel，别的版本即使"系统能装"，
    # 显卡也没有任何加速 —— 而选项是拿 native/OCLP 区间算的（OCLP 能到 14），
    # 不拦的话下拉里会出现 macOS 14，用户选中就得到一份没图形加速的 EFI。
    try:
        from app import nullmoth as _nv_opt
        if _nv_opt.cards(report):
            allowed = {int(_nv_opt.RANGE[0][:2]), int(_nv_opt.RANGE[1][:2])}
            majors = [m for m in majors if m in allowed] or sorted(allowed)
    except Exception:
        pass

    major_options = []
    for m in majors:
        n = f"{m}"
        try:
            label = _safe_name(mods, n)
        except Exception:
            label = f"Darwin {m}"
        major_options.append({"darwin": n, "name": label})

    # 推荐版本：以原生支持的最高版本为起点（Beta 版往下退一档）
    suggested = native_max or "24"
    try:
        suggested = _suggest_version(mods, report, suggested)
    except Exception:
        pass
    try:
        while "Beta" in _safe_name(mods, suggested):
            suggested = f"{int(suggested[:2]) - 1}{suggested[2:]}"
    except Exception:
        pass

    cpu = report.get("CPU", {})
    bus.progress(100, "分析完成")
    from app import nullmoth as _nv
    bus.result({
        "nullmoth": _nv.summary(report),
        "gpuBlocked": gpu_blocked,
        "reportPath": report_path,
        "acpiDir": acpi_dir if os.path.isdir(acpi_dir) else "",
        "motherboard": report.get("Motherboard", {}),
        "cpu": cpu,
        "bios": report.get("BIOS", {}),
        "suggested": suggested,
        "suggestedName": _safe_name(mods, suggested),
        "nativeRange": [str(x) for x in native_version] if native_version else None,
        "thirdPartyGpu": bool(_nv.cards(report)),
        "oclpRange": [str(x) for x in oclp_version] if oclp_version else None,
        "options": major_options,
        "devices": _device_summary(report, suggested, mods),
        "acpiPatches": _acpi_options(mods),
        "raw": {
            "GPU": report.get("GPU", {}),
            "Network": report.get("Network", {}),
            "Sound": report.get("Sound", {}),
            "Bluetooth": report.get("Bluetooth", {}),
            "Storage Controllers": report.get("Storage Controllers", {}),
        },
    })


def _safe_name(mods, v):
    try:
        return mods["os_data"].get_macos_name_by_darwin(str(v))
    except Exception:
        return str(v)


def _explain_gpu_block(report, bus):
    """把"显卡不支持"讲成人话，并给出真正可行的出路。"""
    unsupported = []
    for name, props in (report.get("GPU") or {}).items():
        compat = props.get("Compatibility")
        if compat in (None, (None, None)):
            unsupported.append((name, props))
    if not unsupported:
        return
    bus.log("=" * 58, "warn")
    bus.log("这台机器当前没有 macOS 能驱动的显卡：", "warn")
    for name, props in unsupported:
        bus.log(f"   • {name} [{props.get('Device ID', '')}]", "warn")
    bus.log("", "warn")
    bus.log("macOS 早已停止支持 NVIDIA 显卡驱动（最后一代是 Kepler 架构），", "warn")
    bus.log("因此这台机器无法直接进入图形界面。可选的出路：", "warn")
    bus.log("  1. 换一块受支持的 AMD 显卡（RX 5000/6000 系列原生支持）；", "warn")
    bus.log("  2. 若 CPU 带核显（非 F 后缀），进 BIOS 打开 iGPU 并用核显输出；", "warn")
    bus.log("  3. 先生成 EFI 备用，作为无头服务器跑（没有图形界面）。", "warn")
    bus.log("=" * 58, "warn")


def _log_compatibility(bus, native_version, oclp_version, third_party_gpu=False):
    if native_version:
        # 有 NullMoth 认领时，这个区间已经包含了第三方驱动的能力，不能再说成"原生"
        label = "显卡经 NullMoth 第三方驱动可用" if third_party_gpu else "原生支持"
        bus.log(f"{label}：macOS {native_version[0]} ~ {native_version[1]}", "ok")
    else:
        bus.log("未能确定原生支持的 macOS 范围", "warn")
    if oclp_version:
        bus.log(f"配合 OCLP 根补丁可达：{oclp_version[0]} ~ {oclp_version[1]}", "info")


def _run_compatibility(report, mods, bus, allow=False):
    """跑兼容性检查。allow=True 时容忍"没有可用显卡"这台机器直接判死的情况。

    之所以允许：这正是很多时候用户想先拿到 EFI 备用、或打算后续换显卡的场景，
    直接拦死在门外并没有帮助。但判定结论必须原样传出去，由界面高亮提示。
    """
    from app import nullmoth

    checker = mods["compatibility_checker"].CompatibilityChecker()
    try:
        out = checker.check_compatibility(report)
        return (*out, False)
    except Exception as e:
        if not allow:
            raise
        if "autopilot" not in str(type(e)) and "EngineAbort" not in type(e).__name__:
            raise
        _explain_gpu_block(report, bus)
        autopilot.allow_unsupported_gpu(mods)
        checker = mods["compatibility_checker"].CompatibilityChecker()
        out = checker.check_compatibility(report)
        return (*out, True)


def _suggest_version(mods, report, fallback):
    """复刻 OpCore-Simplify 的推荐逻辑：以原生支持上限为起点，再被每个设备自身的上限压低。

    AMD 集显要压到 Ventura(22) 以下、Intel Gen9 压到 Monterey(21) 以下，这些是它的经验值。

    一个必须处理的冲突：NullMoth 驱动只构建 macOS 15 与 26 两个版本，而"取所有设备的
    最小上限"这个规则会把它拖回更低的版本（比如本机的 Broadcom 网卡原生只到 Ventura），
    结果显卡又不能用。所以只要 NullMoth 认领了卡，版本就直接取驱动支持的最高档 ——
    其他设备的缺口由 OCLP 根补丁补（它们本来就有 OCLP 区间）。
    想装 15 不装 26，在界面上的版本下拉里改，两个都在驱动构建目标内。
    """
    from app import nullmoth  # noqa: PLC0415

    if nullmoth.cards(report):
        return nullmoth.RANGE[0]

    utils = mods["utils"].Utils()
    os_data = mods["os_data"]
    suggested = str(fallback)
    for device_type in ("GPU", "Network", "Bluetooth", "SD Controller"):
        for name, props in (report.get(device_type) or {}).items():
            compat = props.get("Compatibility")
            if not compat or compat == (None, None):
                continue
            if device_type == "GPU" and props.get("Device Type") == "Integrated GPU":
                dev_id = str(props.get("Device ID", ""))[5:]
                if props.get("Manufacturer") == "AMD" or dev_id.startswith(("59", "87C0")):
                    suggested = "22.99.99"
                elif dev_id.startswith(("09", "19")):
                    suggested = "21.99.99"
            try:
                if utils.parse_darwin_version(suggested) > utils.parse_darwin_version(compat[0]):
                    suggested = str(compat[0])
            except Exception:
                pass
    return suggested


def _pick_smbios(smbios, mods, customized, full_report, macos_version, utils, bus):
    """选机型。

    一个坑：当所有显卡都被判为不支持时，定制后的报告里 GPU 键会被整个删掉，
    而引擎的 select_smbios_model 会无条件读 hardware_report["GPU"]，拿不到就崩。
    所以先拿定制后的报告试，失败就退回用完整报告（GPU 信息还在）再试一次。
    """
    fallback = "MacPro7,1" if utils.parse_darwin_version(macos_version) >= utils.parse_darwin_version("25.0.0") \
        else "iMacPro1,1"
    for attempt, data in enumerate((customized, full_report)):
        try:
            return smbios.select_smbios_model(data, macos_version)
        except Exception as e:
            bus.log(f"机型匹配第 {attempt + 1} 次尝试失败：{e}", "warn")
    bus.log(f"自动匹配机型失败，回退默认 {fallback}", "warn")
    return fallback


def _device_summary(report, macos_version, mods):
    try:
        return autopilot.summarise_devices(report, macos_version, mods)
    except Exception:
        return []


def _acpi_options(mods):
    """把引擎的 ACPI 补丁列表暴露给 UI（保留它给的默认值）。"""
    try:
        patches = mods["acpi_patch_data"].patches
        return [{"name": p.name, "checked": bool(getattr(p, "checked", False)),
                 "description": (getattr(p, "description", "") or p.name)}
                for p in patches]
    except Exception:
        return []


# -------------------------------------------------------------------- build

def _norm_version(v):
    """把 "22" / "22.4" 这类简写补全成 "22.0.0"。

    引擎的 parse_darwin_version 直接 split('.') 拆三段，缺一段就抛
    "not enough values to unpack"。更糟的是它在异常时把设备判成不兼容，
    于是整份硬件会被无声地全禁用 —— 所以必须在入口就补好。
    """
    s = str(v or "24").strip()
    parts = s.split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def do_build(args, bus):
    cfg = json.loads(args.json or "{}")
    report_path = args.report
    acpi_dir = cfg.get("acpiDir") or args.acpi or ""
    macos_version = _norm_version(cfg.get("macosVersion") or "24")
    overrides = cfg.get("devices") or {}
    prefs = cfg.get("prefs") or {}

    steps = ["加载引擎", "读取硬件报告", "兼容性判定", "硬件定制",
             "选定 SMBIOS 机型", "选择内核扩展", "下载 OpenCore 与 kext", "生成 config.plist", "收尾"]
    st = busmod.Steps(bus, steps)

    st.enter(0)
    bus.progress(2, "加载引擎")
    mods = autopilot.load_engine()
    autopilot.install_answers(mods, prefs)
    autopilot.patch_nvidia(mods, bus)
    # 修掉 10 秒超时 + 加镜像兜底，否则大一点的 kext 在国内网络环境下基本下不动
    from app import mirror
    mirror.patch(mods, bus)
    autopilot.patch_removal()
    utils = mods["utils"].Utils()

    st.enter(1)
    bus.progress(8, "读取报告")
    report = _load_report(report_path, bus, mods)

    st.enter(2)
    bus.progress(15, "兼容性判定")
    report, native_version, oclp_version, gpu_blocked = _run_compatibility(
        report, mods, bus, allow=bool(cfg.get("allowUnsupportedGpu", True)))
    _log_compatibility(bus, native_version, oclp_version)
    if gpu_blocked:
        bus.log("注意：本配置没有 macOS 支持的显卡，生成的 EFI 仅供备用或无头用途", "warn")

    # 目标版本和 N 卡驱动必须对得上：驱动只构建了 darwin 24 / 25（macOS 15 / 26），
    # 选了别的版本还照常写驱动参数，用户拿到的就是"能装、但显卡完全没加速"的配置，
    # 而且从日志里看不出任何异常 —— 所以这里必须显式说一句。
    try:
        from app import nullmoth as _nv_ver
        if _nv_ver.cards(report):
            nm_lo = int(_nv_ver.RANGE[1][:2])
            nm_hi = int(_nv_ver.RANGE[0][:2])
            mj = int(str(macos_version)[:2])
            if not (nm_lo <= mj <= nm_hi):
                bus.log(
                    f"目标 Darwin {mj} 不在 NullMoth 驱动的构建范围内"
                    f"（Darwin {nm_lo} / {nm_hi}，即 macOS 15 / 26）——"
                    f"这份 EFI 里显卡不会有图形加速，建议改用 macOS 15 或 26。", "warn")
    except Exception:
        pass

    st.enter(3)
    bus.progress(22, "自动判定需禁用的设备")
    customized, disabled, needs_oclp = autopilot.auto_hardware_customization(
        report, macos_version, mods, overrides)
    if disabled:
        bus.log(f"以下 {len(disabled)} 个设备在该 macOS 版本下无驱动，将被禁用：", "warn")
        for k in list(disabled.keys())[:12]:
            bus.log("   • " + k, "warn")
    else:
        bus.log("所有设备在目标版本下均有可用驱动", "ok")

    st.enter(4)
    bus.progress(28, "匹配机型")
    smbios = mods["smbios"].SMBIOS()
    autopilot.redirect_cache(smbios.g)
    model = _pick_smbios(smbios, mods, customized, report, macos_version, utils, bus)
    bus.log(f"选定机型：{model}", "ok")

    st.enter(5)
    bus.progress(34, "分析所需 kext")
    km = mods["kext_maestro"].KextMaestro()
    autopilot.redirect_cache(km)
    ag = mods["acpi_guru"].ACPIGuru()
    acpi_ok = _prepare_acpi(ag, acpi_dir, bus)
    patches = mods["acpi_patch_data"].patches

    # 补丁的 checked 默认全是 False，真正决定"该开哪些"的是引擎自己的 select_acpi_patches()：
    # 它按笔记本/台式、CPU 代次、被禁用设备来挑（比如 Z390 需要 PMC、HEDT 需要 APIC）。
    # 不调它的话，一份补丁都不会进 config。
    if acpi_ok:
        try:
            ag.select_acpi_patches(_ensure_gpu(customized, report, bus), disabled)
            auto_on = [p.name for p in patches if p.checked]
            bus.log(f"ACPI 补丁自动选定 {len(auto_on)} 项"
                    + (f"：{', '.join(auto_on)}" if auto_on else ""), "ok")
        except Exception as e:
            bus.log(f"ACPI 补丁自动选择失败（将不带 SSDT 构建）：{e}", "warn")

    # 界面上的显式选择优先于自动判定。三种预设必须分清：
    #   none → 全部关闭（此时 acpiPatches 本身就是空数组，绝不能再被当成"没指定"）
    #   all  → 全部开启
    #   auto → 不传 acpiPatches，保留 select_acpi_patches 挑出来的结果
    # 早先只看"acpiPatches 是不是 None"：前端默认 auto 会算出空数组，空数组被当成
    # 显式"全部关闭"，于是默认配置反而产出 0 个 SSDT —— EFI 看着"不完整"就是这个。
    preset = str(cfg.get("acpiPreset") or "").strip().lower()
    wanted = cfg.get("acpiPatches")
    if preset == "none":
        for p in patches:
            p.checked = False
        bus.log("按界面设置：ACPI 补丁全部关闭", "info")
    elif preset == "all":
        for p in patches:
            p.checked = True
        bus.log(f"按界面设置：ACPI 补丁全部开启（{len(patches)} 项）", "info")
    elif wanted:
        selected = set(wanted)
        for p in patches:
            p.checked = p.name in selected
        bus.log(f"按界面设置 ACPI 补丁 {len(selected)} 项", "info")
    else:
        bus.log("ACPI 补丁：沿用引擎自动判定", "info")

    km.select_required_kexts(customized, macos_version, needs_oclp, patches)
    try:
        smbios.smbios_specific_options(customized, model, macos_version, patches, km)
    except Exception as e:
        bus.log(f"机型专属设置跳过：{e}", "warn")

    names = [k.name for k in km.kexts if getattr(k, "checked", False)]
    bus.log(f"将安装 {len(names)} 个 kext：{', '.join(names[:10])}{' …' if len(names) > 10 else ''}", "info")

    st.enter(6)
    bus.progress(38, "拉取 OpenCore 与 kext（首次较慢，已缓存则跳过）")
    km.ock_files_dir = str(paths.CACHE)
    gf = km.g if hasattr(km, "g") else mods["gathering_files"].gatheringFiles()
    if not hasattr(km, "g"):
        autopilot.redirect_cache(gf)
    try:
        gf.gather_bootloader_kexts(km.kexts, macos_version)
    except Exception as e:
        bus.log(f"下载 OpenCore / kext 失败：{e}", "error")
        raise
    bus.progress(55, "资源就绪")

    st.enter(7)
    bus.progress(60, "生成 EFI")
    _build_opencore_efi(mods, report, disabled, customized, model, macos_version,
                        needs_oclp, km, ag, bus, acpi_ok=acpi_ok)

    st.enter(8)
    out = _finalize(bus)
    bus.progress(100, "完成")
    bus.result(out)


def _prepare_acpi(ag, acpi_dir, bus):
    """把 Hardware-Sniffer 导出的原始 ACPI 表读进引擎。

    这一步不能省：engine 的 ensure_dsdt() 在没有表时会去问用户"把 ACPI 文件夹拖进来"，
    在无人值守的场景下那个提问只会空转。主动把目录喂进去，它才会安静地做反编译和 SSDT 生成。
    """
    if not acpi_dir or not os.path.isdir(acpi_dir):
        bus.log("没有 ACPI 原始表目录，将跳过 SSDT 补丁（EFI 仍可启动，但缺少电源/休眠等优化）", "warn")
        return False
    aml = [f for f in os.listdir(acpi_dir) if f.lower().endswith(".aml")]
    if not aml:
        bus.log(f"ACPI 目录里没有 .aml 文件：{acpi_dir}", "warn")
        return False
    try:
        ag.read_acpi_tables(acpi_dir)
        dsdt = getattr(ag, "dsdt", None)
        if not dsdt and hasattr(ag, "acpi"):
            try:
                ag.dsdt = ag.acpi.get_dsdt_or_only()
                dsdt = ag.dsdt
            except Exception:
                pass
        bus.log(f"已载入 {len(aml)} 份 ACPI 原始表" + ("，DSDT 解析成功" if dsdt else ""), "ok")
        return bool(dsdt)
    except Exception as e:
        bus.log(f"ACPI 表载入失败（将跳过 SSDT 补丁）：{e}", "warn")
        return False


def _ensure_gpu(build_report, full_report, bus):
    """保证传给 config 生成器的报告里一定有 GPU 键。

    显卡全被判定为不支持时，硬件定制阶段会把整个 GPU 键删掉；而 config_prodigy 里有多处
    直接 `hardware_report["GPU"].items()`，拿不到就崩。这里把原始报告里的显卡信息补回去 ——
    硬件本来就在，只是没有驱动，让它"在册但无驱动"比凭空删掉更贴近事实。
    """
    if build_report.get("GPU"):
        return build_report
    merged = dict(build_report)
    original = dict(full_report.get("GPU") or {})
    if not original:
        original = {"Unknown GPU": {
            "Manufacturer": "NVIDIA", "Codename": "Unknown", "Device ID": "10DE-0000",
            "Device Type": "Discrete GPU",
        }}
        bus.log("原始报告中没有显卡信息，使用占位条目", "warn")
    merged["GPU"] = original
    return merged


def _build_opencore_efi(mods, report, disabled, hardware_report, smbios_model,
                        macos_version, needs_oclp, km, ag, bus, acpi_ok=False):
    """这一段是 OpCore-Simplify 的 build_opencore_efi 的等价实现，
    步骤与它保持一致（复制基础 EFI → ACPI 补丁 → 装 kext → 生成 config → 清理）。"""
    import plistlib
    utils = mods["utils"].Utils()
    kext_mod = mods["kext_maestro"]
    result_dir = str(paths.RESULTS)

    steps = ["复制 EFI 基础文件", "应用 ACPI 补丁", "安装 kext 并写入 config",
             "生成 config.plist", "清理多余驱动与资源"]
    st = busmod.Steps(bus, steps, offset=62, span=32)

    st.enter(0)
    ock = km.ock_files_dir if hasattr(km, "ock_files_dir") else str(paths.CACHE)
    source = os.path.join(ock, "OpenCorePkg")
    if not os.path.isdir(source):
        raise RuntimeError(f"OpenCorePkg 未就绪：{source}")
    utils.create_folder(result_dir, remove_content=True)
    shutil.copytree(source, result_dir, dirs_exist_ok=True)

    config_file = os.path.join(result_dir, "EFI", "OC", "config.plist")
    config_data = utils.read_file(config_file)
    if not config_data:
        raise RuntimeError(f"找不到 {config_file}")

    st.enter(1)
    config_data["ACPI"]["Add"] = []
    config_data["ACPI"]["Delete"] = []
    config_data["ACPI"]["Patch"] = []
    # 后续 ACPI 与 config 阶段都要多次读 GPU，先保证这个键存在
    hardware_report = _ensure_gpu(hardware_report, report, bus)
    try:
        if acpi_ok and ag.ensure_dsdt():
            ag.hardware_report = hardware_report
            ag.disabled_devices = disabled
            ag.acpi_directory = os.path.join(result_dir, "EFI", "OC", "ACPI")
            ag.smbios_model = smbios_model
            try:
                ag.lpc_bus_device = ag.get_lpc_name()
            except Exception:
                ag.lpc_bus_device = None
            for patch in ag.patches:
                if not patch.checked:
                    continue
                if patch.name == "BATP":
                    try:
                        patch.checked = getattr(ag, patch.function_name)()
                    except Exception:
                        patch.checked = False
                    idx = kext_mod.kext_data.kext_index_by_name.get("ECEnabler")
                    if idx is not None:
                        km.kexts[idx].checked = patch.checked
                    continue
                try:
                    acpi_load = getattr(ag, patch.function_name)()
                except Exception as e:
                    bus.log(f"ACPI 补丁 {patch.name} 跳过：{e}", "warn")
                    continue
                if isinstance(acpi_load, dict):
                    config_data["ACPI"]["Add"].extend(acpi_load.get("Add", []))
                    config_data["ACPI"]["Delete"].extend(acpi_load.get("Delete", []))
                    config_data["ACPI"]["Patch"].extend(acpi_load.get("Patch", []))
    except Exception as e:
        bus.log(f"ACPI 处理遇到困难（继续构建）：{e}", "warn")

    try:
        config_data["ACPI"]["Patch"].extend(ag.dsdt_patches)
        config_data["ACPI"]["Patch"] = ag.apply_acpi_patches(config_data["ACPI"]["Patch"])
    except Exception:
        pass

    st.enter(2)
    kexts_directory = os.path.join(result_dir, "EFI", "OC", "Kexts")
    km.install_kexts_to_efi(macos_version, kexts_directory)
    try:
        config_data["Kernel"]["Add"] = km.load_kexts(hardware_report, macos_version, kexts_directory)
    except Exception as e:
        bus.log(f"kext 快照失败：{e}", "warn")

    st.enter(3)
    co = mods["config_prodigy"].ConfigProdigy()
    co.genarate(hardware_report, disabled, smbios_model, macos_version,
                needs_oclp, km.kexts, config_data)
    # NullMoth 的 boot-args / SIP 必须在 genarate 之后写，否则会被它覆盖掉
    from app import nullmoth
    try:
        nullmoth.apply(config_data, report, disabled, bus.log)
    except Exception as e:
        bus.log(f"NullMoth 驱动参数写入失败：{e}", "warn")
    utils.write_file(config_file, config_data)

    st.enter(4)
    _cleanup(result_dir, config_data, utils, bus)
    st.finish_all()


def _cleanup(result_dir, config_data, utils, bus):
    files_to_remove = []

    # 注意 find_matching_paths 返回的是**相对路径**元组 (相对路径, "file"/"dir")，
    # 直接拿去 os.path.isfile 会永远查不到、于是"清理"变成空转（第一次就是这么踩的）。
    drivers_directory = os.path.join(result_dir, "EFI", "OC", "Drivers")
    try:
        driver_list = utils.find_matching_paths(drivers_directory, extension_filter=".efi")
        driver_loaded = [d.get("Path") for d in config_data.get("UEFI", {}).get("Drivers", [])]
        for rel_path, kind in driver_list:
            abs_path = os.path.join(drivers_directory, rel_path)
            if os.path.basename(rel_path) not in driver_loaded:
                files_to_remove.append(abs_path)
    except Exception as e:
        bus.log(f"驱动清理跳过：{e}", "warn")

    tools_directory = os.path.join(result_dir, "EFI", "OC", "Tools")
    try:
        for rel_path, kind in utils.find_matching_paths(tools_directory, extension_filter=".efi"):
            files_to_remove.append(os.path.join(tools_directory, rel_path))
    except Exception:
        pass

    picker_variant = config_data.get("UEFI", {}).get("OpenCore", {}).get("PickerVariant")
    if picker_variant:
        for resource in ("Label", "Image"):
            path = os.path.join(result_dir, "EFI", "OC", "Resources", resource)
            if not os.path.isdir(path):
                continue
            theme_path = os.path.join(path, picker_variant)
            if os.path.isdir(theme_path):
                for name in os.listdir(path):
                    if name != picker_variant:
                        files_to_remove.append(os.path.join(path, name))
            if resource == "Image":
                for root, dirs, files in os.walk(path):
                    for file in files:
                        if not file.lower().endswith((".icns", ".png", ".jpg", ".jpeg", ".tiff", ".bin")):
                            files_to_remove.append(os.path.join(root, file))

    audio_path = os.path.join(result_dir, "EFI", "OC", "Resources", "Audio")
    if os.path.isdir(audio_path):
        files_to_remove.append(audio_path)
    manifest = os.path.join(result_dir, "EFI", "OC", "Resources", "manifest.json")
    if os.path.isfile(manifest):
        files_to_remove.append(manifest)

    for path in files_to_remove:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    bus.log(f"清理 {len(files_to_remove)} 个未使用的资源项", "info")


def _finalize(bus):
    result = str(paths.RESULTS)
    efi_root = os.path.join(result, "EFI")
    summary = {
        "output": result,
        "files": 0,
        "size": 0,
        "kexts": [],
        "acpi": [],
        "drivers": [],
        "hasConfig": False,
        "hasBooter": False,
    }
    if os.path.isfile(os.path.join(efi_root, "OC", "config.plist")):
        summary["hasConfig"] = True
    if os.path.isfile(os.path.join(efi_root, "BOOT", "BOOTx64.efi")):
        summary["hasBooter"] = True
    kext_dir = os.path.join(efi_root, "OC", "Kexts")
    if os.path.isdir(kext_dir):
        summary["kexts"] = sorted(d for d in os.listdir(kext_dir) if d.endswith(".kext"))
    acpi_dir = os.path.join(efi_root, "OC", "ACPI")
    if os.path.isdir(acpi_dir):
        summary["acpi"] = sorted(f for f in os.listdir(acpi_dir) if f.endswith(".aml"))
    drv = os.path.join(efi_root, "OC", "Drivers")
    if os.path.isdir(drv):
        summary["drivers"] = sorted(os.listdir(drv))
    total = 0
    count = 0
    for root, _dirs, files in os.walk(efi_root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    summary["size"] = total
    summary["files"] = count

    bus.log(f"EFI 已生成：{efi_root}", "ok")
    bus.log(f"共 {count} 个文件，{total / 1024 / 1024:.1f} MB；"
            f"{len(summary['kexts'])} 个 kext，{len(summary['acpi'])} 个 ACPI 表", "ok")
    if summary["kexts"]:
        for k in summary["kexts"][:14]:
            bus.log("   • " + k, "info")
    if not summary["hasConfig"]:
        bus.log("警告：未生成 config.plist，EFI 可能不完整", "warn")
    return summary


def main():
    return run(sys.argv[1:], sys.__stdout__)


if __name__ == "__main__":
    sys.exit(main())
