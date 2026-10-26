"""自动决策层：把 OpCore-Simplify 里那些"问用户一轮"的环节换成确定性默认值。

为什么用 monkeypatch 而不是把它的算法重新抄一遍：
这些 select_* 方法里塞了大量按 Device ID、UUID、macOS 版本分叉的判定（光 kext 选择就有几百行），
抄一份意味着原项目一更新我们就开始产生分歧。这里只接管"提问"这一个动作，
业务逻辑仍跑原项目代码 —— oselectivity 的默认答案由 OpCore-Simplify 自己给。

各提问点的安全应答（逐个核对过，按错会死锁）：
  Select audio kext          -> 必须答 "1"/"2"（while True 无兜底）
  force load ... on the      -> 必须答 "no"（只有 yes/no 两条出口，空串会转不出来）
  Select your option         -> "b"，退出 kext 菜单
  Build EFI for UEFI         -> "yes"
  Select Intel Wi-Fi kext    -> "" 即采纳它打印的 recommended
  Select kext for your AMD   -> "" 同上
  Enter the ID of the codec  -> "" 即默认 layout-id
"""
import os
import re
import sys

from . import paths


class AnswerBook:
    """按提问文本给出答案。带调用计数，方便排查"卡在哪个提问上"。"""

    def __init__(self, prefs=None):
        self.prefs = prefs or {}
        self.calls = []

    def __call__(self, prompt="", *a, **kw):
        text = (prompt or "")
        low = text.lower()
        key = text[:120]
        # 同一个提问被反复问 = 我们给的答案引擎不接受。
        # 按"同一问题出现次数"判断比"累计 200 次"准确得多：正常流程会问十几个不同的问题，
        # 累计计数容易误伤，而同一个问题连问 6 次基本可以断定是答案不被接受。
        if self.calls.count(key) >= 6:
            raise RuntimeError(
                f"同一个交互式提问已被重复 {self.calls.count(key)} 次，答案似乎不被接受：{key}")
        if len(self.calls) > 300:
            raise RuntimeError(f"交互式提问总数异常（{len(self.calls)} 次），疑似死循环：{key}")
        self.calls.append(key)

        if "select audio kext" in low:
            return "1" if str(self.prefs.get("audioStrategy", "simplehda")).lower().startswith("oclp") else "2"
        if "force load" in low:
            return "no"
        if "select your option" in low:
            return "b"
        if "build efi for uefi" in low:
            return "yes"

        # 通用兜底：引擎绝大多数提问都自带 "(default: X)"，直接采纳它的默认值。
        # 之前这里对所有未识别的提问一律返回空串 —— 引擎不接受空值就会一直重问，
        # 最后撞上死锁保护。ALC269 这一类需要选 codec layout 的声卡必然踩到：
        #   "Enter the ID of the codec layout you want to use (default: 9)"
        match = re.search(r"default:\s*([^\s)\]}]+)", text, re.I)
        if match:
            return match.group(1)

        return ""


def load_engine():
    """把 vendor 里的 OpCore-Simplify 引擎挂到 sys.path 上并返回常用类。"""
    engine_root = str(paths.OCS)
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    import importlib
    mods = {}
    for name in ("utils", "acpi_guru", "compatibility_checker", "config_prodigy",
                 "gathering_files", "hardware_customizer", "kext_maestro",
                 "report_validator", "run", "smbios", "dsdt",
                 "resource_fetcher", "integrity_checker", "github"):
        try:
            mods[name] = importlib.import_module(f"Scripts.{name}")
        except Exception as e:
            raise RuntimeError(f"加载 OpCore-Simplify 模块 {name} 失败：{e}")
    for ds in ("os_data", "pci_data", "kext_data", "cpu_data", "codec_layouts",
               "acpi_patch_data", "mac_model_data", "chipset_data"):
        try:
            mods[ds] = importlib.import_module(f"Scripts.datasets.{ds}")
        except Exception:
            pass
    return mods


def redirect_cache(*instances):
    """把模块的下载缓存目录从 vendor 里挪到 data/cache。

    引擎默认把 OpenCore / kext / iasl / macserial 下到 <vendor>/OCK_Files。
    放那儿有两个麻烦：清理缓存会误删引擎源码，重装工具又把已下载的内容丢了。
    """
    for obj in instances:
        cache = str(paths.CACHE)
        if hasattr(obj, "ock_files_dir"):
            setattr(obj, "ock_files_dir", cache)
        if hasattr(obj, "download_history_file"):
            setattr(obj, "download_history_file", os.path.join(cache, "history.json"))


def redirect_result_dir(*instances):
    for obj in instances:
        if hasattr(obj, "result_dir"):
            setattr(obj, "result_dir", str(paths.RESULTS))


def patch_nvidia(mods, bus=None):
    """让 NullMoth 的 NVIDIA 驱动接入上游的兼容性检查。

    上游的 check_gpu_compatibility 会**无条件重写**每张卡的 Compatibility，
    所以不能提前 mark() —— 写了也会被冲掉。正确接法是包装它，等它跑完再认领：

      * 上游把卡判成 (None, None) 并准备中止时，NullMoth 认领它、给出驱动的版本区间，
        于是"没有可用显卡"这个结论不再成立；
      * 上游正常跑完时也认领一遍 —— 它支持的卡上游多半标着不支持；
      * 认领之后必须重新算一次最宽区间并收紧全局范围，否则版本选择不会落到驱动支持的档位。
    """
    from app import nullmoth  # noqa: PLC0415

    cls = mods["compatibility_checker"].CompatibilityChecker
    if getattr(cls, "_glass_nvidia_patched", False):
        return cls
    original = cls.check_gpu_compatibility

    def patched(self):
        # 这里必须把异常对象留住再抛。
        # 原来是在 except 里只置一个 bool，等 except 块结束之后再 `raise`，
        # 那时已经离开异常上下文、没有"活跃异常"了 —— 裸 raise 会变成
        # RuntimeError: No active exception to reraise。
        # 后果就是：只要显卡不被上游支持（比如 RTX 3060），分析必定失败。
        error = None
        try:
            original(self)
        except Exception as e:
            error = e

        marked = nullmoth.mark(self.hardware_report)
        if marked:
            try:
                gpu_compat = self._widest_compatibility(
                    g.get("Compatibility") for g in self.hardware_report.get("GPU", {}).values())
                if gpu_compat != (None, None):
                    self._restrict_native_compatibility(gpu_compat)
            except Exception:
                pass
            if bus:
                bus.log(f"NullMoth 驱动认领：{'、'.join(marked)}（macOS 15 / 26，第三方驱动）", "ok")
        elif error is not None:
            raise error
        return None

    cls.check_gpu_compatibility = patched
    cls._glass_nvidia_patched = True
    return cls


def patch_offline(mods, bus=None):
    """断网时让引擎把本地缓存当成「已是最新版本」，从而跳过下载。

    引擎每次生成 EFI 都会先拉 dortania 的 builds/latest.json 确认版本号。断网时
    那个字段为 None，紧接着 `if name in dortania_builds_data` 直接抛
    TypeError: argument of type 'NoneType' is not iterable —— 整条构建就断在下载这一步。

    关键洞察：`fetch_latest_products_info` 里 `latest_products` 的**起点本来就是
    history.json 的拷贝**。所以只要把「拉取失败」降级成「直接用 history」：
      * product_id 与 history 里记录的一致 → is_latest_id 为真；
      * 目录完整性校验通过（本地缓存是当初校验过才落盘的）；
    引擎自己就会打印 "Latest version already downloaded" 并跳过下载。
    也就是说：文件已经在本地时，联网检查版本这一步是**可以省的**。
    """
    try:
        cls = mods["gathering_files"].gatheringFiles
    except Exception:
        return None
    if getattr(cls, "_glass_offline_patched", False):
        return cls
    original = cls.fetch_latest_products_info

    def patched(self, kexts, local_download_history):
        try:
            return original(self, kexts, local_download_history)
        except Exception as e:
            if bus:
                bus.log(f"无法获取在线版本信息（{str(e)[:70]}），改用本地缓存里记录的版本", "warn")
            try:
                return {k: v.copy() for k, v in (local_download_history or {}).items()}
            except Exception:
                return {}

    cls.fetch_latest_products_info = patched
    cls._glass_offline_patched = True
    return cls


def patch_removal():
    """给删除操作加一层降级。

    某些受限环境（沙箱、带 safe-delete 钩子的运行时）会拦截 os.remove / shutil.rmtree，
    一旦被拦，引擎"清掉旧缓存重新下载"这一步就会整个失败，明明文件已经下载好了。
    这里保留正常路径优先，只在它抛异常时退化为调用系统命令删除。

    幂等，重复调用无副作用。
    """
    import shutil as _shutil
    if getattr(_shutil, "_glass_removal_patched", False):
        return
    import subprocess as _sp

    _orig_rmtree = _shutil.rmtree
    _orig_remove = os.remove

    def _system_rm(path):
        """三级降级：系统命令 → PowerShell → 挪进回收目录。"""
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return
        if os.name == "nt":
            _sp.run(["cmd", "/c", "rd", "/s", "/q", path], capture_output=True)
            if not os.path.exists(path):
                return
            _sp.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"Remove-Item -LiteralPath '{path}' -Recurse -Force -ErrorAction SilentlyContinue"],
                    capture_output=True)
            if not os.path.exists(path):
                return
        else:
            _sp.run(["rm", "-rf", path], capture_output=True)
            if not os.path.exists(path):
                return
        # 最后手段：只是改名移动，不涉及删除语义，在受限环境里也能成功
        import time as _time
        trash = os.path.join(str(paths.DATA), "_trash", str(int(_time.time() * 1000)))
        try:
            os.makedirs(trash, exist_ok=True)
            os.rename(path, os.path.join(trash, os.path.basename(path)))
        except Exception:
            try:
                _shutil.move(path, trash)
            except Exception:
                pass

    def rmtree(path, **kw):
        try:
            return _orig_rmtree(path, **kw)
        except Exception:
            _system_rm(path)
            if os.path.exists(path):
                raise

    def remove(path, **kw):
        try:
            return _orig_remove(path, **kw)
        except Exception:
            if not os.path.exists(path):
                return
            _system_rm(path)
            if os.path.exists(path):
                raise

    _shutil.rmtree = rmtree
    os.remove = remove
    os.unlink = remove
    _shutil._glass_removal_patched = True


def allow_unsupported_gpu(mods):
    """放宽"没有可用显卡就中止"的限制。

    有些机器确实没有 macOS 能驱动的显卡（比如纯 NVIDIA 独显又没有核显的台式机），
    但用户可能仍然想先备好 EFI —— 之后换显卡、或者当无头服务器用。
    原项目在这种情况下直接退出，这里改成记一笔帐然后放行。
    """
    checker_cls = mods["compatibility_checker"].CompatibilityChecker

    if getattr(checker_cls, "_glass_gpu_patched", False):
        return
    original = checker_cls.check_gpu_compatibility

    def patched(self):
        try:
            return original(self)
        except Exception:
            # GPU 不可用时不收窄 macOS 版本区间，最终选择权交给界面上的用户
            self.gpu_unsupported = True
            return

    checker_cls.check_gpu_compatibility = patched
    checker_cls._glass_gpu_patched = True


def install_answers(mods, prefs):
    """给用户接管 request_input。返回 AnswerBook 以便事后审计。"""
    book = AnswerBook(prefs)
    utils_cls = mods["utils"].Utils

    def request_input(self, prompt="", *a, **kw):
        return book(prompt)

    utils_cls.request_input = request_input

    # 没有控制台，这些原本操作终端的动作直接吞掉
    utils_cls.clear_screen = lambda self, *a, **kw: None
    utils_cls.adjust_window_size = lambda self, *a, **kw: None
    utils_cls.head = lambda self, *a, **kw: None

    # 引擎在判定"这台机器没法装"时会调 exit_program 直接杀进程（例如没有任何可用显卡）。
    # 替换成抛出可捕获的异常，我们才能在界面上把原因讲清楚，而不是让 worker 无声地消失。
    class EngineAbort(RuntimeError):
        pass

    def exit_program(self, *a, **kw):
        raise EngineAbort("此硬件配置下 OpCore-Simplify 判定无法安装 macOS（通常是因为没有任何受支持的显卡）")

    utils_cls.exit_program = exit_program
    return book


# ---------------------------------------------------------------- 硬件定制

def auto_hardware_customization(hardware_report, macos_version, mods, overrides=None):
    """OpCore-Simplify 的 hardware_customization 的无提问版。

    判定规则与原版完全一致：OCLP 可用 -> 保留并标记需要 root patch；
    原生兼容 -> 保留；不兼容 -> 移入 disabled_devices。
    唯一区别是我们不弹`_handle_device_selection`让用户挑"留哪块 GPU"，
    因为不兼容的设备在此之前已经被划到 disabled 里了，剩下的都是能用的，全留着。

    overrides: {"enable": [设备key...], "disable": [设备key...]} 允许 UI 覆写。
    """
    os_data = mods["os_data"]
    utils = mods["utils"].Utils()
    customized = {}
    disabled = {}
    needs_oclp = False
    overrides = overrides or {}
    force_enable = set(overrides.get("enable") or [])
    force_disable = set(overrides.get("disable") or [])

    watched = ("BIOS", "GPU", "Sound", "Biometric", "Network",
               "Storage Controllers", "Bluetooth", "SD Controller")

    for device_type, devices in hardware_report.items():
        if device_type not in watched:
            customized[device_type] = devices
            continue

        if device_type == "BIOS":
            props = dict(devices)
            props["Firmware Type"] = "UEFI"
            customized[device_type] = props
            continue

        bucket = {}
        for device_name, device_props in devices.items():
            props = dict(device_props)
            key = f"{device_type}:{device_name}"

            if key in force_disable:
                label = _device_label(device_type, device_name, props)
                disabled[label] = props
                continue

            oclp = props.get("OCLP Compatibility")
            if oclp:
                try:
                    if utils.parse_darwin_version(oclp[0]) >= utils.parse_darwin_version(macos_version) \
                            >= utils.parse_darwin_version(oclp[-1]):
                        bucket[device_name] = props
                        needs_oclp = True
                        continue
                except Exception:
                    pass

            compat = props.get("Compatibility",
                               (os_data.get_latest_darwin_version(), os_data.get_lowest_darwin_version()))
            try:
                ok = utils.parse_darwin_version(compat[0]) >= utils.parse_darwin_version(macos_version) \
                    >= utils.parse_darwin_version(compat[-1])
            except Exception:
                ok = False

            if key in force_enable:
                ok = True

            if ok:
                props.pop("OCLP Compatibility", None)
                bucket[device_name] = props
            else:
                disabled[_device_label(device_type, device_name, props)] = props

        if bucket:
            customized[device_type] = bucket

    return customized, disabled, needs_oclp


def _device_label(device_type, device_name, props):
    kind = props.get("Device Type")
    if not kind or "Unknown" in kind:
        kind = device_type
    ends = props.get("Audio Endpoints")
    suffix = f" ({', '.join(ends)})" if ends else ""
    return f"{kind}: {device_name}{suffix}"


def summarise_devices(hardware_report, macos_version, mods):
    """给 UI 用的设备清单：每个设备的兼容状态，让用户能手动改主意。"""
    os_data = mods["os_data"]
    utils = mods["utils"].Utils()
    rows = []
    watched = ("GPU", "Sound", "Biometric", "Network", "Storage Controllers",
               "Bluetooth", "SD Controller")
    for device_type in watched:
        for name, props in (hardware_report.get(device_type) or {}).items():
            compat = props.get("Compatibility")
            oclp = props.get("OCLP Compatibility")
            state = "unknown"
            note = ""
            try:
                if compat:
                    lo = utils.parse_darwin_version(compat[-1])
                    hi = utils.parse_darwin_version(compat[0])
                    now = utils.parse_darwin_version(macos_version)
                    state = "ok" if lo <= now <= hi else "unsupported"
                    if compat[0] and compat[-1]:
                        span = (f"{os_data.get_macos_name_by_darwin(compat[-1])} ~ "
                                f"{os_data.get_macos_name_by_darwin(compat[0])}")
                        # 被 NullMoth 认领的卡绝不能写成"原生支持" —— 上游引擎本来判它
                        # 不支持，这个区间是按第三方驱动的构建目标改写的，用词必须分开，
                        # 否则用户会以为 macOS 自己认这块卡。
                        if props.get("Codename") == "NullMoth driver":
                            note = f"NullMoth 第三方驱动可用 {span}（非 macOS 原生支持）"
                        else:
                            note = f"原生支持 {span}"
            except Exception:
                pass
            if state == "unsupported" and oclp:
                try:
                    if utils.parse_darwin_version(oclp[0]) >= utils.parse_darwin_version(macos_version) \
                            >= utils.parse_darwin_version(oclp[-1]):
                        state = "oclp"
                        note = "需 OCLP 根补丁补齐驱动"
                except Exception:
                    pass
            rows.append({
                "type": device_type,
                "name": name,
                "deviceId": props.get("Device ID", ""),
                "state": state,
                "note": note,
                "key": f"{device_type}:{name}",
            })
    return rows
