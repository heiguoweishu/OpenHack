"""NullMoth NVIDIA 驱动支持。

这是从 1401（nullmoth）移植过来的，让我把它的来历说清楚：

上游 OpCore-Simplify 的判定是"Kepler 之后的 NVIDIA 卡一律不支持"，遇到没有其他显卡的机器直接退出。
NullMoth 驱动让一批 Turing 及更新的 NVIDIA 卡能跑 macOS，做法是在上游判定跑完之后把
支持的卡重新标成受支持，于是版本选择器自己就会推荐，卡也不再被禁用。

三步：

  1. ``mark(report)``    在兼容性检查之后跑，把在支持表里的独显标成驱动可用的版本区间。
  2. ``apply(cfg, ...)`` 生成 config.plist 之后跑，写入该驱动实测的 boot-args、SIP 值
                         以及它要求的其余 OpenCore 设置。
  3. ``stage(root, ...)`` 把驱动包（带 SHA-256 校验）放到安装介质上，装好 macOS 后由用户手动装上。

必须如实说明的限制：
  * 支持 **macOS 15 Sequoia 与 macOS 26 Tahoe**（驱动对两者分别构建 NVAccel，
    ``NM_TAHOE=1``）；其它版本不在构建目标内。
  * 这是**第三方驱动，不是 macOS 原生支持**。上游引擎自己的判定结论是"不支持"，
    这里只是按驱动的实际能力改写区间，界面上必须照实说。
  * 支持表里的 235 个 ID 中，``tested`` 只有一个（2D05）是上游实测过的 ——
    其余属于"在 NVIDIA 自家固件支持表内"，未经 NullMoth 实测。
    工具会把这个区别显示出来，不会假装"你的卡保证能用"。
  * 许可：驱动主体是 **PolyForm Noncommercial**（着色器翻译器 LGPL-3.0），
    个人 / 研究 / 教育 / 非营利可用，**任何商用都不行** —— 这一点已带进免责页。
"""
import hashlib
import json
import os
import re

from . import paths

# (max, min) Darwin：24 = macOS 15 Sequoia，25 = macOS 26 Tahoe。
# 驱动对这两个版本分别构建 NVAccel（NM_TAHOE=1），别的版本一概不行。
RANGE = ("25.99.99", "24.0.0")
SIP_DRIVER = bytes.fromhex("430A0000")

# nvfb/nvaccel 打开 framebuffer 与加速器；nvfbheads=4 是卡的输出头数；
# -nvkmsnosmooth 关掉平滑接管；两条 AMFI 参数让 WindowServer 能加载 Apple 未签名的 GPU bundle；
# ipc_control_port_options=0 是官方 README 列明的必需项（早期版本漏了它，桌面会冻）。
BOOT_ARGS = ("nvfb=1", "nvaccel=1", "nvfbheads=4", "-nvkmsnosmooth",
             "amfi_get_out_of_my_way=0x1", "amfi=0x80", "ipc_control_port_options=0")

# 驱动 README 的"Wiring into an existing OpenCore setup"一节列明的其余硬性要求。
# 少任何一项驱动都不会起来，所以 apply() 里逐条落实并在日志里说明。
EXTRA_REQUIREMENTS = (
    ("Misc > Security > SecureBootModel", "Disabled",
     "Apple Secure Boot 会拒绝 Apple 未签名的 kext"),
    ("Kernel > Block", "com.apple.iokit.IONDRVSupport / Exclude",
     "否则固件 framebuffer 会抢走 NVRMFB 的显示索引 0"),
    ("Kernel > Add", "Lilu.kext + AMFIPass.kext 且启用",
     "两者是驱动加载的前置依赖"),
)

PACKAGE = {
    "name": "nullmoth-nvidia-2.4.3.tar.gz",
    "url": "https://github.com/nullmoth/nvidia-macos-driver/releases/download/v2.4.3/nullmoth-nvidia-2.4.3.tar.gz",
    "sha256": "e95a6a5d0ab8727bf88b77ddb77140fedeff845a294758d4b52761a40d0a3632",
}

# 查找顺序很重要：
#   1) 运行时数据目录 —— 允许用户/后续版本替换这张表（可写）
#   2) 随包资源目录 —— PyInstaller 把 data/ 打进 _MEIPASS，只能从这里读
# 只认第 1 个路径是之前那个 bug：打包后 data/ 在 _MEIPASS 里，
# 运行时目录下压根没有这个文件，于是表读成空的，整台机器的 N 卡被判不支持。
_TABLE = None
_TABLE_SRC = None


def table_candidates():
    """按优先级列出可能存放支持表的路径。

    顺序很重要：
      1) 运行时数据目录 —— 允许用户/后续版本替换这张表（可写）
      2) 随包资源目录 —— PyInstaller 把 data/ 打进 _MEIPASS，只能从这里读
    之前只认第 1 个路径：打包后 data/ 在 _MEIPASS 里，运行时目录下压根没有
    这个文件，于是支持表读成空的，整台机器的 N 卡都被判成不支持。
    """
    return (
        os.path.join(str(paths.DATA), "nvidia_gsp_ids.json"),
        str(paths.resource("data", "nvidia_gsp_ids.json")),
    )


def table_path():
    """实际生效的支持表路径，找不到返回空串（界面/日志用得上）。"""
    for p in table_candidates():
        if os.path.isfile(p):
            return p
    return ""


def table():
    """惰性加载支持表。数据文件缺失时返回空表而不是崩 —— 工具的其他功能不该被这一项拖垮。

    但"缺失"必须喊出来：静默返回空表的后果是整台 N 卡机器被误判成"没有可用显卡"，
    而这看起来完全像业务结论，没人会想到是文件没找到。
    """
    global _TABLE, _TABLE_SRC
    if _TABLE is not None:
        return _TABLE
    path = table_path()
    if not path:
        print("[nullmoth] 警告：找不到 NVIDIA 支持表，N 卡将一律判为不支持。已查找："
              + " ; ".join(table_candidates()))
        _TABLE = {"ids": {}, "tested": []}
        return _TABLE
    try:
        with open(path, encoding="utf-8") as f:
            _TABLE = json.load(f)
        _TABLE_SRC = path
    except Exception as e:
        print(f"[nullmoth] 警告：支持表解析失败（{path}）：{e}")
        _TABLE = {"ids": {}, "tested": []}
    return _TABLE


def _pid(gpu):
    did = (gpu.get("Device ID") or "").upper()
    return did[5:] if did.startswith("10DE-") else ""


def supported(gpu):
    """NVIDIA 自家固件支持表里列出的独显。笔记本的混合输出不在处理范围内。"""
    return (gpu.get("Manufacturer") == "NVIDIA"
            and gpu.get("Device Type") == "Discrete GPU"
            and _pid(gpu) in table().get("ids", {}))


def cards(report):
    return {n: g for n, g in (report.get("GPU") or {}).items()
            if isinstance(g, dict) and supported(g)}


def tested(report):
    """支持表中被 NullMoth 实测过的卡 —— 只有这些才有实测背书。"""
    t = set(table().get("tested") or [])
    return [n for n, g in cards(report).items() if _pid(g) in t]


def mark(report):
    """把受支持的卡标成驱动可用的版本区间，返回被标记的卡名。

    注意标的是"第三方驱动可用"而不是"原生支持"：上游引擎对 Kepler 之后的 N 卡
    一律判不支持，这里是按驱动的实际构建目标改写区间。
    """
    hit = []
    for name, gpu in cards(report).items():
        gpu["Compatibility"] = RANGE
        gpu.pop("OCLP Compatibility", None)
        if not gpu.get("Codename") or gpu.get("Codename") == "Unknown":
            gpu["Codename"] = "NullMoth driver"
        hit.append(name)
    return hit


def _gpu_disabled(name, gpu, disabled_devices):
    labels = {name, "GPU: " + name}
    if gpu.get("Device Type"):
        labels.add(f"{gpu['Device Type']}: {name}")
    return any(l in disabled_devices for l in labels)


def live_cards(report, disabled_devices):
    """标记过、且没被禁用的受支持卡。"""
    out = []
    for name, gpu in cards(report).items():
        if gpu.get("Compatibility") == RANGE and not _gpu_disabled(name, gpu, disabled_devices or {}):
            out.append(name)
    return out


def _drop_conflicting_args(cfg, log=None):
    """删掉会让驱动起不来的启动参数。

    驱动 README 明确要求移除 ``nv_disable=1`` / ``-wegnoegpu`` / ``agdpmod=pikera``：
    前两个直接把 N 卡藏掉，第三个是给 AMD 用的 connector 补丁，会抢显示。
    """
    nv = cfg.get("NVRAM", {}).get("Add", {}).get(
        "7C436110-AB2A-4BBB-A880-FE41995C9F82", {})
    args = (nv.get("boot-args") or "").split()
    keep, dropped = [], []
    for a in args:
        if a in ("nv_disable=1", "-wegnoegpu") or a.startswith("agdpmod="):
            dropped.append(a)
        else:
            keep.append(a)
    if dropped:
        nv["boot-args"] = " ".join(keep)
        if log:
            log(f"已移除与 NullMoth 冲突的启动参数：{' '.join(dropped)}", "ok")
    return dropped


def apply(cfg, report, disabled_devices, log=None):
    """把该驱动要求的设置写进 config.plist。返回是否写入成功。

    除了 boot-args 与 SIP，还包括它 README 里列明的几项硬性要求
    （SecureBootModel、IONDRVSupport 屏蔽、移除冲突参数）。
    少任何一项驱动都不会启动，所以逐条落实并在日志里说明。
    """
    live = live_cards(report, disabled_devices)
    if not live:
        return False

    nv = cfg.setdefault("NVRAM", {}).setdefault("Add", {}).setdefault(
        "7C436110-AB2A-4BBB-A880-FE41995C9F82", {})
    _drop_conflicting_args(cfg, log)
    args = (nv.get("boot-args") or "").split()
    added = [a for a in BOOT_ARGS if a not in args]
    if added:
        nv["boot-args"] = " ".join(args + added)
        if log:
            log(f"NullMoth 驱动参数已写入 boot-args：{' '.join(added)}", "ok")

    if nv.get("csr-active-config") != SIP_DRIVER:
        nv["csr-active-config"] = SIP_DRIVER
        if log:
            log("NullMoth 驱动的 SIP 值已写入（其 kext 未经 Apple 签名）", "ok")

    # SIP 要能反复声明，但不能像 boot-args 那样被删 —— 删掉会抹掉正在运行的机器上的开关。
    delete = cfg.setdefault("NVRAM", {}).setdefault("Delete", {}).setdefault(
        "7C436110-AB2A-4BBB-A880-FE41995C9F82", [])
    if "csr-active-config" not in delete:
        delete.append("csr-active-config")

    _secure_boot_off(cfg, log)
    _block_iondrv(cfg, log)

    _laptop_gpu_awake(cfg, report, live, log)
    return True


def _secure_boot_off(cfg, log=None):
    """SecureBootModel = Disabled：Apple Secure Boot 会拒绝未签名的 kext。"""
    sec = cfg.setdefault("Misc", {}).setdefault("Security", {})
    if sec.get("SecureBootModel") != "Disabled":
        sec["SecureBootModel"] = "Disabled"
        if log:
            log("SecureBootModel 已设为 Disabled（NullMoth 的 kext 未经 Apple 签名）", "ok")


def _block_iondrv(cfg, log=None):
    """屏蔽 com.apple.iokit.IONDRVSupport。

    不屏蔽的话固件 framebuffer 会占用显示索引 0，NVRMFB 抢不到，屏幕停在启动文字。
    """
    block = cfg.setdefault("Kernel", {}).setdefault("Block", [])
    for entry in block:
        if entry.get("Identifier") == "com.apple.iokit.IONDRVSupport":
            if entry.get("Strategy") != "Exclude":
                entry["Strategy"] = "Exclude"
                if log:
                    log("IONDRVSupport 的屏蔽策略已改为 Exclude", "ok")
            return
    block.append({"Arch": "Any", "Comment": "Block IONDRVSupport (NullMoth)",
                  "Enabled": True, "Identifier": "com.apple.iokit.IONDRVSupport",
                  "MaxKernel": "", "MinKernel": "", "Strategy": "Exclude"})
    if log:
        log("已屏蔽 com.apple.iokit.IONDRVSupport（否则固件 framebuffer 会抢走显示）", "ok")


_PCI_PATH = re.compile(r"PciRoot\(0x[0-9A-Fa-f]+\)(/Pci\(0x[0-9A-Fa-f]+,0x[0-9A-Fa-f]+\))+")


def _laptop_gpu_awake(cfg, report, live, log=None):
    """笔记本：给独显的 PCI 路径设 acpi-wake-type=1。

    否则 macOS 的 ACPI 会把独显断电，驱动起不来（RmInitAdapter 失败）。
    台式机不动。
    """
    if (report.get("Motherboard") or {}).get("Platform") != "Laptop":
        return
    gpus = report.get("GPU") or {}
    add = cfg.setdefault("DeviceProperties", {}).setdefault("Add", {})
    for name in live:
        path = (gpus.get(name) or {}).get("PCI Path") or ""
        if not _PCI_PATH.fullmatch(path):
            continue
        props = add.setdefault(path, {})
        if props.get("acpi-wake-type") != 1:
            props["acpi-wake-type"] = 1
            if log:
                log(f"{name}：已设 acpi-wake-type=1 保持独显供电", "ok")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def stage(dest_root, cache_dir=None, fetch=None, log=None):
    """把驱动包（SHA-256 校验通过才落地）放到安装介质的 NullMoth/ 下。

    放进 FAT32 的 ESP 而不是安装分区：Windows 没有 HFS+ 驱动，写不进 Apple 分区，
    而 ESP 是唯一双方都能读的地方。
    """
    cache_dir = cache_dir or os.path.join(str(paths.CACHE), "nullmoth")
    os.makedirs(cache_dir, exist_ok=True)
    src = os.path.join(cache_dir, PACKAGE["name"])

    if not os.path.exists(src) or _sha256(src) != PACKAGE["sha256"]:
        if fetch is None:
            raise RuntimeError(f"驱动包不在缓存里：{src}")
        fetch(PACKAGE["url"], src + ".part")
        if _sha256(src + ".part") != PACKAGE["sha256"]:
            try:
                os.remove(src + ".part")
            except OSError:
                pass
            raise RuntimeError("驱动包的 SHA-256 与发布值不符，已拒绝写入")
        os.replace(src + ".part", src)

    out_dir = os.path.join(dest_root, "NullMoth")
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, PACKAGE["name"])
    with open(src, "rb") as a, open(dst + ".part", "wb") as b:
        for block in iter(lambda: a.read(1 << 20), b""):
            b.write(block)
    if _sha256(dst + ".part") != PACKAGE["sha256"]:
        try:
            os.remove(dst + ".part")
        except OSError:
            pass
        raise RuntimeError("驱动包在拷贝过程中发生变化")
    os.replace(dst + ".part", dst)

    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "NullMoth NVIDIA 驱动（支持 macOS 15 Sequoia / macOS 26 Tahoe）\n"
            "--------------------------------------------------------------\n"
            "第三方驱动，不是 macOS 原生支持。支持 Turing 及更新的 N 卡。\n\n"
            "macOS 装好后，在这个分区里执行：\n\n"
            f"    tar -xzf {PACKAGE['name']}\n"
            "    cd pkgroot\n"
            "    shasum -a 256 -c SHA256SUMS\n"
            "    sudo ./install.sh\n"
            "    sudo shutdown -r now\n\n"
            "shasum 那一步每个文件都必须显示 OK，缺一不可。\n"
            "重启后首次启动会在 \"PCI configuration end\" 停最多一分钟，\n"
            "那是 GPU 在上电，不是卡住。\n\n"
            "macOS 会要求在 系统设置 > 隐私与安全性 里允许这些扩展，允许后再重启一次。\n\n"
            "OpenCore 侧的必要设置已由 Windows 端的工具写进 config.plist：\n"
            "  boot-args  nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth\n"
            "             amfi_get_out_of_my_way=0x1 amfi=0x80 ipc_control_port_options=0\n"
            "  csr-active-config  430A0000\n"
            "  SecureBootModel    Disabled\n"
            "  Kernel > Block     com.apple.iokit.IONDRVSupport (Exclude)\n"
            "  Kernel > Add       Lilu.kext 与 AMFIPass.kext（需启用）\n\n"
            "注意：驱动 kext 装在 /Library/Extensions 并由 AKC 加载，\n"
            "不要再从 EFI/OC/Kexts 注入一遍，会冲突。\n\n"
            "许可：PolyForm Noncommercial 1.0.0（着色器翻译器为 LGPL-3.0）。\n"
            "个人 / 研究 / 教育 / 非营利可用，商用不行。\n")
    if log:
        log(f"驱动包已放入 {out_dir}", "ok")
    return dst


def summary(report):
    """给界面用的一句话状态。"""
    all_c = cards(report)
    if not all_c:
        return {"state": "none", "text": "未检测到受支持的 NVIDIA 独显"}
    t = tested(report)
    return {
        "state": "tested" if t else "listed",
        "cards": sorted(all_c.keys()),
        "tested": t,
        "text": (f"{'、'.join(t)}：NullMoth 实测支持，可跑 macOS 15 / 26"
                 if t else
                 f"{'、'.join(all_c.keys())}：在 NVIDIA 支持表内（未实测），"
                 f"可尝试 macOS 15 / 26"),
        "ids": {n: table()["ids"].get(_pid(g), "") for n, g in all_c.items()},
    }
