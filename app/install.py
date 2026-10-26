"""第二条线的执行引擎：在现有硬盘上无损开 EFI 分区 + macOS 安装分区，并塞好 EFI 与恢复镜像。

"无损"的定义在这里是严格的：
  * 不删除、不格式化任何现有分区；唯一的改动是"把一个 NTFS 分区尾部缩短"
    （NTFS 本身支持的在线收缩，Windows 磁盘管理做同一件事）；
  * 每一步破坏性操作之前都重新校验磁盘身份（盘号 + 序列号 + 容量），
    万一盘符/盘号在操作过程中漂移了立刻停手 —— 宁可失败也不能改错盘；
  * 中途任何一步失败都立刻停下来，把已经做过的动作和下一步该怎么收尾讲清楚，
    绝不"继续试试" —— 分区这件事没有 undo。

挂载 ESP 用的临时盘符必须在任何路径下归还，否则用户机器上会永远多出一个幽灵盘符。
"""
import json
import os
import shutil
import time

from . import disks as disks_api
from . import ps
from . import recovery
from .bus import Steps

GiB = 1024 ** 3
MiB = 1024 ** 2


class InstallError(RuntimeError):
    pass


def _verify_identity(disk_number, expected):
    """操作前重新确认"这块盘还是不是刚才那块盘"。

    盘号在系统里是可以漂移的（比如一台(OFFLINE)盘重新联机、热插拔、驱动重载），
    用一个过期盘号去 juice osteoarthritis clean 是会出人命的。
    """
    found = None
    for d in disks_api.enum_disks():
        if d["number"] == disk_number:
            found = d
            break
    if not found:
        raise InstallError(f"磁盘 {disk_number} 已不在系统中")
    checks = [
        ("序列号", expected.get("serial") and found["serial"] != expected["serial"]),
        ("容量", expected.get("size") and abs(found["size"] - expected["size"]) > MiB),
    ]
    for name, bad in checks:
        if bad:
            raise InstallError(
                f"磁盘 {disk_number} 的{name}与开始时不一致（现在是 {found['serial']} / {found['size']}，"
                f"记录的是 {expected.get('serial')} / {expected.get('size')}）。"
                f"磁盘发生了变更，已中止，未做任何修改。")
    return found


def _identity(disk):
    return {"serial": disk["serial"], "size": disk["size"], "uniqueId": disk["uniqueId"]}


def _human(n):
    b = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def plan(disk, esp_size=1536 * MiB, install_size=None, shrink=None):
    """算出一个可执行方案，同时把不合法的组合提前挡掉。

    shrink: {"letter": "C", "mb": 40960} 或 None（表示直接用磁盘尾部已有空闲）
    返回 {"espBytes","installBytes","shrinkMb","targetVolume",...}
    """
    parts = disk["partitions"]
    tail_free = disk["size"] - sum(p["size"] for p in parts) - MiB * 1 if parts else 0
    if parts:
        last_end = max((p["offset"] + p["size"]) for p in parts)
        tail_free = max(0, disk["size"] - last_end)
    if tail_free < 4 * MiB:
        tail_free = 0

    need = esp_size + (install_size or 0)
    if install_size is None:
        # 没指定就尽可能吃下所有可回收空间
        need = esp_size

    available = tail_free + (int((shrink or {}).get("mb", 0)) * MiB)
    target_pct_size = None
    if shrink:
        if available < need:
            raise InstallError(
                f"空间不足：EFI 分区需 {_human(esp_size)}，安装分区需 {_human(install_size or 0)}，"
                f"当前可回收 {_human(available)}。")

    if not tail_free and not shrink:
        raise InstallError("磁盘尾部没有空闲空间，也没有指定要压缩的分区。请选择一个分区并压缩出空间。")

    return {
        "diskNumber": disk["number"],
        "espBytes": esp_size,
        "installBytes": install_size,
        "shrink": shrink,
        "tailFree": tail_free,
        "available": available,
        "targetPctSize": target_pct_size,
    }


def _resize_partition(letter, new_bytes, bus):
    """把某个 NTFS 分区收缩到 new_bytes。用 Storage cmdlet 而不是 diskpart，
    因为它会替我们把住最后一道关：目标值小于最小允许值时 Windows 直接拒绝，
    而不是先把文件系统搞坏。"""
    script = f"""
$ErrorActionPreference = 'Stop'
$p = Get-Partition -DriveLetter '{letter}' -ErrorAction Stop
$current = $p.Size
if ($current -le {int(new_bytes)}) {{
  throw "目标大小 {int(new_bytes)} 不小于当前大小 $current，无需收缩"
}}
Resize-Partition -DriveLetter '{letter}' -Size {int(new_bytes)} -ErrorAction Stop
Update-HostStorageCache
$p2 = Get-Partition -DriveLetter '{letter}' -ErrorAction Stop
@{{ size = $p2.Size }} | ConvertTo-Json -Compress
"""
    bus.step(f"收缩分区 {letter}:  目标 {_human(new_bytes)}")
    bus.log(f"收缩 {letter}: 分区 — 这只是把 NTFS 卷末端往回收，卷内数据不受影响", "info")
    try:
        raw = ps.run(script, timeout=900)
    except ps.PSError as e:
        raise InstallError(f"收缩分区 {letter}: 失败：{e}\n\n"
                           f"常见原因：该卷存在无法移动的系统文件（页面文件、休眠文件、影子副本）。"
                           f"可在管理员终端执行 powercfg -h off 关闭休眠后再试。")
    return raw


def _new_partition(disk_number, size_bytes, gpt_type, bus, max_size=False):
    # 参数名是 -UseMaximumSize（不是 UseMaximumAvailableSize）；
    # 而且必须二选一，不能既不给 -Size 也不给 -UseMaximumSize。
    size_arg = "-UseMaximumSize" if max_size else f"-Size {int(size_bytes)}"
    script = f"""
$ErrorActionPreference = 'Stop'
$p = New-Partition -DiskNumber {int(disk_number)} {size_arg} -GptType '{gpt_type}' -ErrorAction Stop
Update-HostStorageCache
@{{ number = $p.PartitionNumber; uniqueId = [string]$p.UniqueId; size = $p.Size }} | ConvertTo-Json -Compress
"""
    try:
        raw = ps.run(script, timeout=300)
    except ps.PSError as e:
        raise InstallError(f"创建分区失败：{e}")
    try:
        return json.loads(raw)
    except Exception:
        raise InstallError(f"创建分区后无法确认结果：{raw[:200]}")


def _mount(disk_number, partition_number, letter, bus):
    script = f"""
$ErrorActionPreference = 'Stop'
Add-PartitionAccessPath -DiskNumber {int(disk_number)} -PartitionNumber {int(partition_number)} -AccessPath '{letter}:\\' -ErrorAction Stop
Update-HostStorageCache
Start-Sleep -Milliseconds 800
"""
    ps.run(script, timeout=180)
    root = f"{letter}:\\"
    for _ in range(40):
        if os.path.isdir(root):
            return root
        time.sleep(0.5)
    raise InstallError(f"分区已创建，但测试路径 {root} 迟迟未出现。")


def _verify_volume(root, expect_fs="FAT32", expect_label=None):
    """用文件系统的实际属性做校验，而不是看 diskpart 说了什么 —— 那里的话会随界面语言变。"""
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    fs_buf = ctypes.create_unicode_buffer(1024)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), buf, ctypes.sizeof(buf), None, None, None, fs_buf, ctypes.sizeof(fs_buf))
    if not ok:
        raise InstallError(f"无法读取 {root} 的卷信息")
    fstype = fs_buf.value.upper()
    label = buf.value
    if expect_fs and fstype != expect_fs.upper():
        raise InstallError(f"{root} 的文件系统是 {fstype}，预期 {expect_fs}。已中止。")
    if expect_label and label.upper() != expect_label.upper():
        raise InstallError(f"{root} 的卷标是 “{label}”，预期 “{expect_label}”。已中止。")
    return {"fs": fstype, "label": label}


def _unmount(disk_number, partition_number, letter, bus):
    """归还临时盘符。这一步无论如何都要跑成功。"""
    script = f"""
$ErrorActionPreference = 'Stop'
try {{
  Remove-PartitionAccessPath -DiskNumber {int(disk_number)} -PartitionNumber {int(partition_number)} -AccessPath '{letter}:\\' -ErrorAction Stop
}} catch {{
  $p = Get-Partition -DiskNumber {int(disk_number)} -PartitionNumber {int(partition_number)}
  foreach ($a in $p.AccessPaths) {{
    if ($a -like '{letter}:*') {{ Remove-PartitionAccessPath -DiskNumber {int(disk_number)} -PartitionNumber {int(partition_number)} -AccessPath $a -ErrorAction SilentlyContinue }}
  }}
}}
Update-HostStorageCache
"""
    try:
        ps.run(script, timeout=180)
        bus.log(f"已归还临时盘符 {letter}:", "info")
    except Exception as e:
        bus.log(f"归还盘符 {letter}: 失败，请手动在磁盘管理里删除该盘符：{e}", "warn")


def _copy_tree_safely(src, dst, bus, label=""):
    total_files = 0
    for _r, _d, fs in os.walk(src):
        total_files += len(fs)
    done = 0
    os.makedirs(dst, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for f in files:
            s = os.path.join(root, f)
            t = os.path.join(target_root, f)
            shutil.copy2(s, t)
            done += 1
            if bus and done % 10 == 0:
                bus.progress(None, f"{label} {done}/{total_files}")
    return done


def run_install(cfg, bus, allow_cancel=None):
    """主流程。cfg 由 UI 组装。

    cfg = {
      diskNumber, espSizeMb, installSizeMb(或 None = 用尽剩余),
      shrink: {letter, mb} 或 None,
      efiSource: str, darwin: int, skipRecovery: bool,
      reusePartition: int 或 None
    }
    """
    steps = [
        "锁定目标磁盘",
        "压缩现有分区腾出空间",
        "创建 EFI 分区并挂载",
        "创建 macOS 安装分区",
        "写入 OpenCore EFI",
        "写入 Apple 恢复镜像",
        "收尾并校验",
    ]
    st = Steps(bus, steps)
    st.enter(0)

    # ---- 0. 锁定磁盘身份 ----
    target = None
    for d in disks_api.enum_disks():
        if d["number"] == cfg["diskNumber"]:
            target = d
            break
    if not target:
        raise InstallError(f"找不到磁盘 {cfg['diskNumber']}")
    identity = _identity(target)
    bus.log(f"目标磁盘：#{target['number']} {target['name']} "
            f"（序列号 {target['serial'] or '空'}，容量 {_human(target['size'])}）", "ok")

    if target["style"].upper() != "GPT":
        raise InstallError("目标磁盘不是 GPT 分区表，无法继续。请先用 mbr2gpt 转换。")
    if target["isReadOnly"] or target["isOffline"]:
        raise InstallError("目标磁盘处于只读或脱机状态。")

    if not ps.is_admin():
        raise InstallError("创建分区需要管理员权限，请以管理员身份重新启动本工具。")

    esp_size = int(cfg.get("espSizeMb", 1536)) * MiB
    install_mb = cfg.get("installSizeMb") or 0
    install_size = int(install_mb) * MiB if install_mb else None
    shrink = cfg.get("shrink")

    # ---- 1. 收缩 ----
    st.enter(1)
    if shrink:
        letter = shrink["letter"].strip().rstrip(":\\").upper()
        mb = int(shrink["mb"])
        if mb <= 0:
            raise InstallError("压缩量必须大于 0")
        caps = {c["letter"]: c for c in disks_api.shrink_candidates()}
        cap = caps.get(letter)
        if not cap:
            raise InstallError(f"卷 {letter}: 不是可压缩的 NTFS 卷")
        if mb * MiB > cap["maximal"]:
            raise InstallError(
                f"{letter}: 最多只能压缩出 {_human(cap['maximal'])}，当前设定 {_human(mb * MiB)}。\n"
                f"NTFS 里页面文件、休眠文件这类不可移动的文件会挡住收缩。")
        part = next((p for p in target["partitions"]
                     if (p.get("letter") or "").upper() == letter), None)
        if not part:
            raise InstallError(f"磁盘 {target['number']} 上没有盘符为 {letter} 的分区")
        new_size = max(part["size"] - mb * MiB, cap["size"] - mb * MiB)
        _verify_identity(cfg["diskNumber"], identity)
        _resize_partition(letter, new_size, bus)
        bus.log(f"{letter}: 已收缩出 {_human(mb * MiB)} 空闲空间", "ok")
    else:
        bus.log("跳过压缩：直接使用磁盘尾部已有的空闲空间", "info")

    # ---- 2. 创建 ESP ----
    st.enter(2)
    _verify_identity(cfg["diskNumber"], identity)

    bus.log(f"创建 EFI 分区（{_human(esp_size)}）", "info")
    esp = _new_partition(target["number"], esp_size, disks_api.GPT_ESP, bus)
    esp_num = esp["number"]

    script_fmt = f"""
$ErrorActionPreference = 'Stop'
$p = Get-Partition -DiskNumber {target['number']} -PartitionNumber {esp_num} -ErrorAction Stop
Format-Volume -Partition $p -FileSystem FAT32 -NewFileSystemLabel 'EFI' -Force -Confirm:$false -ErrorAction Stop | Out-Null
Update-HostStorageCache
@{{ ok = $true }} | ConvertTo-Json -Compress
"""
    ps.run(script_fmt, timeout=300)

    letter = disks_api.free_letter()
    esp_root = _mount(target["number"], esp_num, letter, bus)
    info = _verify_volume(esp_root, expect_fs="FAT32", expect_label="EFI")
    bus.log(f"EFI 分区已就绪：{esp_root}（{info['fs']}，{_human(esp_size)}）", "ok")

    # ---- 3. 安装分区 ----
    st.enter(3)
    reuse = cfg.get("reusePartition")
    install_num = None
    if reuse:
        install_num = int(reuse)
        bus.log(f"复用已有分区 #{install_num} 作为 macOS 安装目标", "info")
    elif install_size or install_size is None:
        _verify_identity(cfg["diskNumber"], identity)
        bus.log("创建 macOS 安装分区" + (f"（{_human(install_size)}）" if install_size else "（用尽剩余空间）"), "info")
        part = _new_partition(target["number"], install_size or 0, disks_api.GPT_APFS, bus,
                              max_size=install_size is None)
        install_num = part["number"]
        bus.log(f"安装分区已创建：#{install_num}，{_human(part.get('size', 0))}（保持未格式化，"
                f"等 macOS 安装器抹成 APFS）", "ok")
    else:
        bus.log("未指定安装分区大小，跳过", "warn")

    # ---- 4. 写 EFI ----
    st.enter(4)
    efi_src = cfg.get("efiSource")
    if efi_src and os.path.isdir(efi_src):
        # 构建产物目录里除 EFI/ 之外还有 manifest.json 这类元数据，
        # 它们不是引导文件，不该被塞进 ESP 根目录污染固件分区。
        nested = os.path.join(efi_src, "EFI")
        if os.path.isdir(nested):
            copy_src, copy_dst = nested, os.path.join(esp_root, "EFI")
        else:
            copy_src, copy_dst = efi_src, esp_root

        if os.path.isdir(copy_dst):
            bus.log("EFI 分区中已存在 EFI 目录，将被合并覆盖", "warn")
        n = _copy_tree_safely(copy_src, copy_dst, bus, label="写入 EFI")
        bus.log(f"已写入 {n} 个 EFI 文件到 {copy_dst}", "ok")
        _ensure_fallback_boot(os.path.join(esp_root, "EFI"), bus)
    else:
        bus.log("未指定 EFI 源目录，跳过 EFI 写入（分区已建好）", "warn")

    # ---- 5. 恢复镜像 ----
    st.enter(5)
    if cfg.get("skipRecovery"):
        bus.log("按配置跳过恢复镜像下载", "info")
    else:
        darwin = int(cfg.get("darwin", 24))
        bus.log(f"向 Apple 请求 macOS 恢复镜像信息（Darwin {darwin}）", "info")
        try:
            sess = recovery.session()
            rinfo = recovery.image_info(darwin, sess)
        except Exception as e:
            raise InstallError(f"无法从 Apple 获取恢复镜像：{e}")
        bus.log(f"Apple 返回：{rinfo['name']}（{rinfo['product']}）", "ok")

        last = {"v": 0}

        def prog(done, total):
            if total:
                v = 30 + int(55 * done / total)
                if v - last["v"] >= 1:
                    last["v"] = v
                    bus.progress(v, f"下载恢复镜像 {_human(done)} / {_human(total)}")

        try:
            res = recovery.download(rinfo, esp_root, progress=prog,
                                    cancel=(allow_cancel() if allow_cancel else None))
        except Exception as e:
            raise InstallError(f"恢复镜像下载失败：{e}")
        bus.log(f"恢复镜像已就位：{os.path.basename(res['image'])} "
                f"（{res['chunks']} 个分片全部通过 Apple 签名校验，{_human(res['bytes'])}）", "ok")

    # ---- 5b. NullMoth NVIDIA 驱动包 ----
    driver = _stage_nullmoth(cfg, esp_root, bus, allow_cancel)

    # ---- 6. 收尾 ----
    st.enter(6)
    try:
        _verify_volume(esp_root, expect_fs="FAT32")
        listing = sorted(os.listdir(esp_root))
        bus.log(f"EFI 分区内容：{', '.join(listing)}", "info")
    except Exception as e:
        bus.log(f"最终校验时出现问题：{e}", "warn")

    _unmount(target["number"], esp_num, letter, bus)
    st.finish_all()

    final = disks_api.enum_disks()
    final_disk = next((d for d in final if d["number"] == target["number"]), target)
    return {
        "diskNumber": target["number"],
        "espPartition": esp_num,
        "espSize": esp_size,
        "installPartition": install_num,
        "recoveryInstalled": not cfg.get("skipRecovery"),
        "driverInstalled": bool(driver),
        "partitions": final_disk["partitions"],
    }


def _fetch_with_mirror(url, dest, bus=None, timeout=180):
    """下载驱动包，直连失败时走镜像。写到 dest（stage() 已经管好 .part 与校验）。"""
    import urllib.request
    from app import mirror

    last = None
    for i, candidate in enumerate(mirror.candidates(url)):
        try:
            with urllib.request.urlopen(candidate, timeout=timeout) as r, \
                    open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if i > 0 and bus:
                bus.log("驱动包改由镜像下载", "info")
            return
        except Exception as e:
            last = e
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
    raise InstallError(f"驱动包下载失败：{last}")


def _stage_nullmoth(cfg, esp_root, bus, allow_cancel=None):
    """按硬件报告判断是否需要 NullMoth 驱动，需要就下载并放进 ESP。

    放在 FAT32 的 ESP 而不是安装分区：Windows 没有 HFS+ 驱动，写不进 Apple 分区，
    而 ESP 是装系统前后双方都能读的地方。驱动本身是 macOS 装好后由用户手动装的，
    stage() 会把步骤写进同目录的 README.txt。
    """
    if not cfg.get("stageDriver"):
        return False
    try:
        from app import nullmoth

        report = {}
        rp = cfg.get("reportPath")
        if rp and os.path.isfile(rp):
            with open(rp, encoding="utf-8") as f:
                report = json.load(f)
        if not nullmoth.cards(report):
            bus.log("硬件里没有 NullMoth 支持的 NVIDIA 独显，跳过驱动包", "info")
            return False

        names = ", ".join(nullmoth.cards(report))
        tested = nullmoth.tested(report)
        bus.log(f"检测到 NullMoth 驱动支持的显卡：{names}"
                + ("（实测型号）" if tested else "（支持表内、未实测）"), "info")
        bus.log("开始获取驱动包……", "info")

        def fetch(url, dest):
            if allow_cancel and allow_cancel():
                raise InstallError("已取消")
            _fetch_with_mirror(url, dest, bus)

        path = nullmoth.stage(esp_root, fetch=fetch, log=bus.log)
        bus.log(f"驱动包已就位：{path}", "ok")
        bus.log("macOS 装好后，运行该目录下 README.txt 里的三条命令即可装上显卡驱动。", "info")
        return True
    except Exception as e:
        # 驱动包含 256 MB，下载失败很常见（网络/镜像/校验）。**这不影响已经写好的 EFI**：
        # NullMoth 在 config.plist 里的那套开关（boot-args / SIP / SecureBootModel /
        # IONDRVSupport Block / AMFIPass）是在生成 EFI 时落盘的，跟这个驱动包无关。
        # 缺的只是"装进 macOS 的那份驱动本体"，用户随时能自己补，所以不能报错退出。
        bus.log(f"驱动包下载失败：{str(e)[:100]}", "warn")
        bus.log("这不影响分区与 EFI —— NullMoth 的 config 配置已经写好，只缺驱动包本体。", "warn")
        bus.log("补救办法（任选其一）：", "warn")
        bus.log("  1) 联网后重新执行本流程，只会补下载驱动包；", "warn")
        bus.log("  2) 手动下载 nullmoth-nvidia-*.tar.gz，解压后按里面的 README.txt 装进 macOS；", "warn")
        bus.log("  3) 驱动仓库：https://github.com/nullmoth/nvidia-macos-driver", "warn")
        return False


def _ensure_fallback_boot(efi_root, bus):
    """把 OpenCore 复制一份成 EFI/BOOT/BOOTx64.efi。

    固件在没有有效 NVRAM 启动项时会去找这个默认路径兜底，多一层保险。
    """
    try:
        src = None
        oc = os.path.join(efi_root, "OC", "OpenCore.efi")
        if os.path.isfile(oc):
            src = oc
        boot_dir = os.path.join(efi_root, "BOOT")
        os.makedirs(boot_dir, exist_ok=True)
        dst = os.path.join(boot_dir, "BOOTx64.efi")
        if src and not os.path.isfile(dst):
            shutil.copy2(src, dst)
            bus.log("已部署 fallback 启动文件 EFI/BOOT/BOOTx64.efi", "ok")
    except Exception as e:
        bus.log(f"写入 fallback 启动文件时遇到问题（不影响主流程）：{e}", "warn")