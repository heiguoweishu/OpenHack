"""装机前的系统体检。

黑苹果失败绝大多数不是 EFI 写错了，而是固件环境没准备好。这些检查项每一条都对应
一个真实会导致"装到一半起不来"的坑。TPM / SGX 一律不动 —— 关掉 TPM 会让 Windows 11
和 BitLocker 当场失效，这个代价远比收益大。
"""
import winreg

from . import ps

FIRMWARE_KEY = r"SYSTEM\CurrentControlSet\Control\PEFirmwareType"
SECUREBOOT_KEY = r"SYSTEM\CurrentControlSet\Control\SecureBoot\State"

# 存储控制器名字里出现这些词，NVMe/SATA 盘在 macOS 里就会彻底消失
VMD_HINTS = ("VMD", "VOLUME MANAGEMENT", "RAID", "RST", "OPTANE")


def _reg_value(key_path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def firmware_type():
    """PEFirmwareType: 1=BIOS, 2=UEFI。"""
    v = _reg_value(FIRMWARE_KEY, "PEFirmwareType")
    if v == 2:
        return "UEFI"
    if v == 1:
        return "Legacy"
    # 个别机器注册表读不到，退回固件环境变量
    try:
        out = ps.run("$env:firmware_type", timeout=30).strip().lower()
        if out == "uefi":
            return "UEFI"
        if out == "legacy":
            return "Legacy"
    except Exception:
        pass
    return "Unknown"


def secure_boot():
    v = _reg_value(SECUREBOOT_KEY, "UEFISecureBootEnabled")
    if v == 1:
        return "Enabled"
    if v == 0:
        return "Disabled"
    return "Unknown"


def bitlocker():
    """返回每个卷的保护状态列表。Win32 家庭版没有这个 WMI 命名空间，要能优雅降级。"""
    rows = []
    script = r"""
$ErrorActionPreference = 'Stop'
try {
  $ns = 'root\CIMV2\Security\MicrosoftVolumeEncryption'
  $vs = @(Get-CimInstance -Namespace $ns -ClassName Win32_EncryptableVolume -ErrorAction Stop)
  $out = @()
  foreach ($v in $vs) {
    $st = 0
    try { $st = (Get-CimInstance -Namespace $ns -ClassName Win32_EncryptableVolume -Filter "DeviceID='$($v.DeviceID)'").GetProtectionStatus() } catch {}
    $out += @{ driveLetter = [string]$v.DriveLetter; protection = $st.ProtectionStatus }
  }
  $out | ConvertTo-Json -Compress
} catch { '[]' }
"""
    try:
        data = ps.parse_json(script, timeout=60)
        if isinstance(data, dict):
            data = [data]
        for r in data:
            rows.append({
                "drive": (r.get("driveLetter") or "").strip(),
                "protection": r.get("protection"),
                "status": {0: "off", 1: "on", 2: "unknown"}.get(r.get("protection"), "unknown"),
            })
    except Exception:
        pass
    return rows


def storage_risks():
    """找出会让磁盘在 macOS 里消失的存储控制器。

    用 PowerShell 的 Get-PnpDevice 而不是 wmi 模块：GUI 子系统的 exe 里
    WMI/COM 调用会挂死，PowerShell 是独立进程，两种模式下行为一致。
    """
    hits = []
    pattern = "|".join(VMD_HINTS)
    script = f"""
$ErrorActionPreference = 'Stop'
$m = '{pattern}'
@(Get-PnpDevice -ErrorAction SilentlyContinue |
  Where-Object {{ $_.FriendlyName -match $m -and $_.FriendlyName -match 'Controller|VMD|RAID|RST' }} |
  ForEach-Object {{ @{{ name = $_.FriendlyName; deviceId = $_.InstanceId }} }}) | ConvertTo-Json -Compress
"""
    try:
        data = ps.parse_json(script, timeout=60)
        if isinstance(data, dict):
            data = [data]
        for r in data or []:
            if not isinstance(r, dict):
                continue
            hits.append({
                "name": (r.get("name") or "").strip(),
                "deviceId": (r.get("deviceId") or "")[:80],
            })
    except Exception:
        pass
    return hits


def check_all(report=None):
    """产出一份体检报告。每个检查项带有阻塞级别：blocker / warn / ok。"""
    items = []

    fw = firmware_type()
    items.append({
        "id": "firmware", "title": "固件启动模式",
        "value": fw,
        "level": "ok" if fw == "UEFI" else "blocker",
        "detail": "已将 firmware 设为 UEFI。黑苹果只支持 UEFI 引导。" if fw == "UEFI" else
                  "当前是 Legacy/BIOS 启动。需先用 Windows 自带的 mbr2gpt 转成 UEFI："
                  "管理员 PowerShell 执行 mbr2gpt /validate /allowFullOS 通过后，再 mbr2gpt /convert，"
                  "然后进 BIOS 把启动模式改成 UEFI Only（关闭 CSM）。",
    })

    sb = secure_boot()
    items.append({
        "id": "secureboot", "title": "安全启动 (Secure Boot)",
        "value": sb,
        "level": "ok" if sb == "Disabled" else ("warn" if sb == "Unknown" else "blocker"),
        "detail": "已关闭，OpenCore 可以正常加载。" if sb == "Disabled" else
                  "需要进 BIOS 关掉 Secure Boot（通常在 Security / Boot 菜单，"
                  "部分主板需要先设置管理员密码才能改）。开着它签名校验会拦住 OpenCore。",
    })

    bls = bitlocker()
    on = [b for b in bls if b["status"] == "on"]
    items.append({
        "id": "bitlocker", "title": "BitLocker / 设备加密",
        "value": ("已启用：" + ", ".join(b["drive"] for b in on)) if on else "未启用",
        "level": "warn" if on else "ok",
        "detail": "调整分区前请先暂停 BitLocker 保护并备份恢复密钥。" if on else
                  "没有检测到加密卷。",
    })

    risks = storage_risks()
    if risks:
        items.append({
            "id": "vmd", "title": "Intel VMD / RAID 控制器",
            "value": f"检测到 {len(risks)} 项",
            "level": "warn",
            "detail": "检测到可能的 Intel VMD / RST / RAID 控制器："
                      + "、".join(r["name"] for r in risks[:3])
                      + "。这类模式下 macOS 看不到你的 NVMe 硬盘。解决方法是进 BIOS 把 "
                        "VMD Controller 关掉（部分主板叫 Map PCIe Storage under VMD），"
                        "或者先在 Windows 里切到 AHCI：bcdedit /set {current} safeboot minimal → "
                        "重启进 BIOS 改 AHCI → 安全模式启动后再 bcdedit /deletevalue {current} safeboot。",
        })
    else:
        items.append({"id": "vmd", "title": "Intel VMD / RAID 控制器", "value": "未检测到",
                      "level": "ok", "detail": "存储控制器处于 AHCI/NVMe 直通模式。"})

    if report:
        bios = report.get("BIOS", {})
        if (bios.get("Firmware Type") or "") == "BIOS":
            for it in items:
                if it["id"] == "firmware":
                    it["value"] = "BIOS (Legacy)"
                    it["level"] = "blocker"

    return {
        "items": items,
        "blockers": [i for i in items if i["level"] == "blocker"],
        "warnings": [i for i in items if i["level"] == "warn"],
        "ready": not any(i["level"] == "blocker" for i in items),
        "admin": ps.is_admin(),
    }
