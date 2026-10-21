"""Windows 磁盘枚举与分区规划。

这一层只读地看清这台机器的磁盘布局，提供"能不能无损加分区"的判断依据。
真正动刀的代码在 install.py 里，且每一步都会重新校验磁盘身份。

用 PowerShell 的 Storage 模块而不是 diskpart，有两个很实际的理由：
  * diskpart 的输出随 Windows 显示语言变化，拿字符串判断成败在非英文机器上必翻车；
  * Storage 模块能用 ConvertTo-Json 直接吐结构化数据，盘 – 分区 – 卷三层关系一目了然。
"""
import json
import subprocess
import re
from . import ps

# GPT 分区类型 GUID（Windows 世界的通用常量）
GPT_ESP = "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"      # EFI System Partition
GPT_MSR = "{e3c9e316-0b5c-4db8-817d-f92df00215ae}"      # Microsoft Reserved
GPT_BASIC = "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"    # 基本数据分区
GPT_WINRE = "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}"    # Windows 恢复环境
GPT_APFS = "{7c3457ef-0000-11aa-aa11-00306543ecac}"     # Apple APFS
GPT_HFS = "{48465300-0000-11aa-aa11-00306543ecac}"      # Apple HFS+

OS_TYPES = (GPT_ESP, GPT_APFS, GPT_HFS, GPT_WINRE)

GiB = 1024 ** 3
MiB = 1024 ** 2

_ENUM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$out = @()
$disks = @(Get-Disk -ErrorAction SilentlyContinue)
foreach ($d in $disks) {
  $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue)
  $volumes = @{}
  foreach ($v in @(Get-Volume -ErrorAction SilentlyContinue)) {
    $t = $null
    try { $t = (Get-Partition -DriveLetter $v.DriveLetter -ErrorAction SilentlyContinue | Where-Object { $_.DiskNumber -eq $d.Number }) } catch {}
    if ($t) { $volumes[[string]$t.PartitionNumber] = @{ fs = $v.FileSystem; label = $v.FileSystemLabel; size = $v.Size; remaining = $v.SizeRemaining } }
  }
  $pl = @()
  foreach ($p in $parts) {
    $vol = $null
    if ($volumes.ContainsKey([string]$p.PartitionNumber)) { $vol = $volumes[[string]$p.PartitionNumber] }
    $pl += @{
      number = $p.PartitionNumber
      type = [string]$p.Type
      gptType = [string]$p.GptType
      size = $p.Size
      offset = $p.Offset
      driveLetter = $(if ($p.DriveLetter) { [string]$p.DriveLetter } else { '' })
      isActive = [bool]$p.IsActive
      isBoot = [bool]$p.IsBoot
      isSystem = [bool]$p.IsSystem
      isHidden = [bool]$p.IsHidden
      isReadOnly = [bool]$p.IsReadOnly
      noDefaultDriveLetter = [bool]$p.NoDefaultDriveLetter
      volume = $vol
    }
  }
  $out += @{
    number = $d.Number
    friendlyName = [string]$d.FriendlyName
    serialNumber = [string]$d.SerialNumber
    uniqueId = [string]$d.UniqueId
    size = $d.Size
    allocated = $d.AllocatedSize
    partitionStyle = [string]$d.PartitionStyle
    busType = [string]$d.BusType
    health = [string]$d.HealthStatus
    operational = [string]$d.OperationalStatus
    isBoot = [bool]$d.IsBoot
    isSystem = [bool]$d.IsSystem
    isOffline = [bool]$d.IsOffline
    isReadOnly = [bool]$d.IsReadOnly
    firmware = [string]$d.FirmwareVersion
    partitions = $pl
  }
}
$out | ConvertTo-Json -Depth 8 -Compress
"""


class DiskError(RuntimeError):
    pass


def _ps_json(script, timeout=120):
    return ps.parse_json(script, timeout=timeout)


def enum_disks():
    """枚举全部物理磁盘及其分区。异常一律抛出，绝不让调用方拿到半截结果。"""
    raw = ps.run(_ENUM_SCRIPT, timeout=180)
    if not raw or not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    disks = []
    for d in data:
        parts = []
        for p in (d.get("partitions") or []):
            if not isinstance(p, dict):
                continue
            parts.append({
                "number": p.get("number"),
                "type": (p.get("type") or "").strip(),
                "gptType": (p.get("gptType") or "").strip(),
                "size": int(p.get("size") or 0),
                "offset": int(p.get("offset") or 0),
                "letter": (p.get("driveLetter") or "").strip(),
                "isBoot": bool(p.get("isBoot")),
                "isSystem": bool(p.get("isSystem")),
                "isActive": bool(p.get("isActive")),
                "isHidden": bool(p.get("isHidden")),
                "isReadOnly": bool(p.get("isReadOnly")),
                "noDefaultDriveLetter": bool(p.get("noDefaultDriveLetter")),
                "volume": p.get("volume") or {},
            })
        parts.sort(key=lambda x: x["number"] or 0)
        disks.append({
            "number": d.get("number"),
            "name": (d.get("friendlyName") or "未知磁盘").strip(),
            "serial": (d.get("serialNumber") or "").strip(),
            "uniqueId": (d.get("uniqueId") or "").strip(),
            "size": int(d.get("size") or 0),
            "allocated": int(d.get("allocated") or 0),
            "style": (d.get("partitionStyle") or "").strip(),
            "busType": (d.get("busType") or "").strip(),
            "health": (d.get("health") or "").strip(),
            "operational": (d.get("operational") or "").strip(),
            "isSystem": bool(d.get("isSystem")),
            "isBoot": bool(d.get("isBoot")),
            "isOffline": bool(d.get("isOffline")),
            "isReadOnly": bool(d.get("isReadOnly")),
            "partitions": parts,
        })
    disks.sort(key=lambda x: x["number"] or 0)
    return disks


def _human(nbytes):
    b = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def analyse(disk, esp_size=1.6 * GiB, install_min=40 * GiB):
    """判断一块盘能不能无损加 macOS 分区，返回分析报告。"""
    issues = []          # 硬阻塞，不解决不许往下走
    warnings = []        # 警告，用户知情后可以继续
    size = disk["size"]
    parts = disk["partitions"]

    if disk["style"].upper() != "GPT":
        issues.append({
            "code": "mbr",
            "title": "磁盘是 MBR 分区表",
            "detail": "黑苹果要求 GPT + UEFI。Windows 自带的 mbr2gpt 可以无损转换："
                      "管理员终端执行 `mbr2gpt /validate /allowFullOS` 校验，再 `mbr2gpt /convert`。",
        })
    if disk["isOffline"]:
        issues.append({"code": "offline", "title": "磁盘处于脱机状态",
                       "detail": "磁盘管理里把它联机后再重试。"})
    if disk["isReadOnly"]:
        issues.append({"code": "readonly", "title": "磁盘被设为只读",
                       "detail": "需要用 diskpart 的 `attributes disk clear readonly` 清除只读属性。"})
    if (disk["health"] or "").lower() not in ("healthy", ""):
        warnings.append({"code": "health", "title": f"磁盘健康状态为 {disk['health']}",
                         "detail": "建议先备份数据并检查磁盘。"})

    # 磁盘尾部是否还有未分配空间可以直接用
    tail_free = 0
    if parts and parts[-1].get("offset") and size:
        last = parts[-1]
        tail_free = max(0, size - (last["offset"] + last["size"]))
        if tail_free < MiB:
            tail_free = 0

    # 估算最后一个可收缩分区能腾出多少（真实可收缩量只能问 NTFS，见 shrinkable_volume）
    shrinkable = 0
    for p in parts:
        v = p.get("volume") or {}
        rem = v.get("remaining")
        if rem and p.get("size") and not p["isSystem"]:
            pass
    volume_free = 0
    for p in parts:
        v = p.get("volume") or {}
        fs = (v.get("fs") or "").upper()
        if fs in ("NTFS", "REFS") and v.get("remaining"):
            volume_free = max(volume_free, int(v["remaining"]))

    total_available = tail_free + volume_free

    need = esp_size + install_min
    if size and need > total_available and need > size - disk["allocated"]:
        if total_available < need:
            issues.append({
                "code": "space",
                "title": "可用空间不足",
                "detail": f"安装分区建议至少 {_human(install_min)}，EFI 分区约 {_human(esp_size)}，"
                          f"当前盘上可回收的空闲空间约 {_human(max(total_available, size - disk['allocated']))}。"
                          f"先在 Windows 里清理磁盘或压缩出一个新分区。",
            })

    def _gpt(tag):
        """统一掉花括号再比大小 —— PowerShell 的 GptType 带花括号，常量定义里也带，
        两边不一致就会漏判。"""
        return tag.strip().lstrip("{").rstrip("}").lower()

    existing_esp = [p for p in parts if _gpt(p.get("gptType") or "") == _gpt(GPT_ESP)]
    if not existing_esp:
        warnings.append({"code": "no-esp", "title": "该盘上没有 EFI 分区",
                         "detail": "Windows 很可能装在另一块盘上。我们会在目标盘上新开一个 EFI 分区，"
                                   "现有的 Windows 启动不受影响。"})

    apple_parts = [p for p in parts if _gpt(p.get("gptType") or "") in
                   {_gpt(GPT_APFS), _gpt(GPT_HFS)}]
    if apple_parts:
        warnings.append({"code": "apple-exists", "title": "检测到已存在的 Apple 分区",
                         "detail": f"分区 {[p['number'] for p in apple_parts]} 已经是 Apple 文件系统，"
                                   f"可能是已有的 macOS。请确认不要覆盖它。"})

    return {
        "disk": disk["number"],
        "name": disk["name"],
        "size": size,
        "sizeHuman": _human(size),
        "allocated": disk["allocated"],
        "allocatedHuman": _human(disk["allocated"]),
        "tailFree": tail_free,
        "tailFreeHuman": _human(tail_free),
        "volumeFree": volume_free,
        "volumeFreeHuman": _human(volume_free),
        "availableHuman": _human(max(total_available, max(0, size - disk["allocated"]))),
        "gpt": disk["style"].upper() == "GPT",
        "parts": parts,
        "existingEsp": bool(existing_esp),
        "issues": issues,
        "warnings": warnings,
        "ok": not issues,
    }


_SHRINK_QUERY = r"""
$ErrorActionPreference = 'Stop'
$v = Get-Volume -DriveLetter '{letter}' -ErrorAction Stop
$p = Get-Partition -DriveLetter '{letter}' -ErrorAction Stop
$cim = Get-CimInstance -ClassName Win32_Volume -Filter "DriveLetter = '{letter}:'" -ErrorAction Stop
$c = Get-CimInstance -Class Win32_LogicalDisk -Filter "DeviceID = '{letter}:'" -ErrorAction SilentlyContinue
$min = $null
$max = $null
try {{
  $res = (Get-CimInstance -ClassName Win32_Volume -Filter "DriveLetter = '{letter}:'").InvokeMethod('QueryMaximalResize', @())
  $max = $res.ReturnValueEx
}} catch {{}}
@{{
  driveLetter = '{letter}'
  size = $p.Size
  free = $cim.FreeSpace
  fileSystem = [string]$v.FileSystem
  health = [string]$v.HealthStatus
  shrinkableFreeSpace = $(if ($null -ne $max) {{ $max }} else {{ $cim.FreeSpace }})
}} | ConvertTo-Json -Compress
"""


def volume_capacity(letter):
    """某个 NTFS 卷最多能压缩出多少空间。

    注意 Get-Volume 的 SizeRemaining 不等于可收缩量 —— NTFS 里放不下的文件
    （页面文件、MFT、不可移动的系统文件）会让实际可收缩远小于空闲量。这里用
    Win32_Volume.QueryMaximalResize 问一个准数。
    """
    letter = (letter or "").strip().rstrip(":\\").upper()
    if not letter or not re.fullmatch(r"[A-Z]", letter):
        raise DiskError(f"盘符不合法：{letter!r}")
    script = _SHRINK_QUERY.format(letter=letter)
    raw = ps.run(script, timeout=120)
    try:
        data = json.loads(raw)
    except Exception:
        return {"letter": letter, "size": 0, "free": 0, "fileSystem": "", "shrinkable": 0}
    return {
        "letter": letter,
        "size": int(data.get("size") or 0),
        "free": int(data.get("free") or 0),
        "fileSystem": (data.get("fileSystem") or "").upper(),
        "shrinkable": int(data.get("shrinkableFreeSpace") or 0),
    }


_VOLUME_LIST = r"""
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($v in @(Get-Volume -ErrorAction SilentlyContinue)) {
  $letter = $v.DriveLetter
  if (-not $letter) { continue }
  $fs = [string]$v.FileSystem
  if ($fs -ne 'NTFS') { continue }
  $p = $null
  try { $p = Get-Partition -DriveLetter $letter -ErrorAction Stop } catch { continue }
  $out += @{
    letter = [string]$letter
    label = [string]$v.FileSystemLabel
    size = $p.Size
    free = $v.SizeRemaining
    fileSystem = $fs
    isSystem = [bool]$p.IsSystem
    isBoot = [bool]$p.IsBoot
  }
}
$out | ConvertTo-Json -Compress
"""


_MB_PAREN = re.compile(r"\(\s*([\d][\d,\.]*)\s*MB\s*\)", re.IGNORECASE)
_MB_ANY = re.compile(r"([\d][\d,\.]*)\s*MB\b", re.IGNORECASE)
_GB_ANY = re.compile(r"([\d][\d,\.]*)\s*GB\b", re.IGNORECASE)


def _to_number(text):
    cleaned = re.sub(r"[,\s]", "", text or "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_shrink_max(text):
    """从 diskpart 输出里解析可回收字节数。

    diskpart 有两种输出形态，取决于分区大小：
        大分区：可回收的最大字节数为:   59 GB (61310 MB)
        小分区：可回收的最大字节数为: 3005 MB
    只认括号格式会让小分区（比如测试用的虚拟盘）一律算成 0 字节，
    界面上就会莫名其妙报"空间不足"。

    描述文字各语言不同，但"数字 + MB/GB"的写法一致，所以只认数字和单位。
    """
    text = text or ""
    m = _MB_PAREN.search(text)
    if m:
        return int(_to_number(m.group(1)) * MiB)
    m = _MB_ANY.search(text)
    if m:
        return int(_to_number(m.group(1)) * MiB)
    m = _GB_ANY.search(text)
    if m:
        return int(_to_number(m.group(1)) * GiB)
    return 0


def max_shrink_bytes(letter):
    """某个 NTFS 卷最多能压缩出多少空间（字节）。

    走 diskpart 的 `shrink querymax` 而不是 WMI 的 QueryMaximalResize —— 后者在不少
    Windows 版本上根本没有这个方法。

    别用 Get-Volume 的空闲量来替代：NTFS 上 Pagefile / MFT / 影子副本都压缩不动，
    空闲 60G 往往只能压出 20G，界面上给个虚高的数字会让用户在最后一步卡住。
    """
    letter = (letter or "").strip().rstrip(":\\").upper()
    if not re.fullmatch(r"[A-Z]", letter):
        raise DiskError(f"盘符不合法：{letter!r}")
    script = f"select volume {letter}\r\nshrink querymax\r\nexit\r\n"
    try:
        p = subprocess.run(["diskpart.exe"], input=script.encode("utf-8"),
                           capture_output=True, timeout=300,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 0
    except Exception:
        return 0
    return parse_shrink_max(ps._decode(p.stdout or b""))


def shrink_candidates():
    """列出所有 NTFS 卷及其真实可压缩量。"""
    out = []
    meta = {}
    try:
        data = json.loads(ps.run(_VOLUME_LIST, timeout=120))
        if isinstance(data, dict):
            data = [data]
        for d in data:
            meta[(d.get("letter") or "").strip().upper()] = d
    except Exception:
        pass

    for letter in sorted(meta.keys()):
        d = meta[letter]
        out.append({
            "letter": letter,
            "label": (d.get("label") or "").strip(),
            "size": int(d.get("size") or 0),
            "free": int(d.get("free") or 0),
            "fileSystem": (d.get("fileSystem") or "").upper(),
            "maximal": max_shrink_bytes(letter),
            "isSystem": bool(d.get("isSystem")),
            "isBoot": bool(d.get("isBoot")),
        })
    return out


def free_letter():
    """倒着找 Z→G 之间第一个没人用的盘符，避开系统惯用的 A-F 和光盘符。"""
    return ps.free_letter()
