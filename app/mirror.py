"""下载层的健壮性补丁。

OpCore-Simplify 原本的两个硬伤（在国内容易踩到）：

  1. `ResourceFetcher._make_request` 的 timeout 写死为 **10 秒**。这是个 socket 级别超时，
     意味着一次 read() 超过 10 秒就整条连断。GitHub Release 常常只有几十 KB/s，
     下 10 MB 的 OpenCorePkg 必然在半路超时，然后重试重头再来，永远下不完。

  2. github.com 主站与 objects.githubusercontent.com 在网络高峰会被 RST。
     原项目也没有任何换源能力。

这里的做法：
  * 把超时提到 60 秒（按 socket 操作算，慢速下载也能连续读）；
  * 直连优先；直连失败后依次尝试公共镜像 gh-proxy 系列，成功经验记进 state.json，
    下次优先用它，省掉每次都先撞一次南墙。

注意所有下载最终都要过 SHA256 校验（引擎自带 digest 检查），
所以走镜像不会降低安全性 —— 文件本身是被 duty 验证的。
"""
import json
import os
import time

from . import paths

# 这是 socket 级超时（每次 read 的上限），不是总时长。
# 给太大（曾经是 180）会让一条坏连接空转 3 分钟：实测 SSL 握手能卡到 196 秒，
# 界面上表现为"进度条一动不动"，用户完全不知道是在下载还是死了。
# 45 秒足够慢速连接读一段数据，超时就换下一个源，整体反而更快。
REQUEST_TIMEOUT = 45

# 公共 GitHub 加速镜像。顺序即为尝试顺序。
MIRRORS = [
    "https://gh-proxy.com/",
    "https://edgeone.gh-proxy.com/",
    "https://hk.gh-proxy.com/",
    "https://ghfast.top/",
]

_MIRROR_HOSTS = ("github.com", "githubusercontent.com", "raw.githubusercontent.com",
                 "objects.githubusercontent.com", "gist.github.com", "codeload.github.com")

_state_path = os.path.join(str(paths.DATA), "mirror.json")
_memory = {"preferred": None}


def _load_state():
    if _memory.get("loaded"):
        return _memory
    try:
        with open(_state_path, encoding="utf-8") as f:
            data = json.load(f)
        _memory["preferred"] = data.get("preferred")
    except Exception:
        pass
    _memory["loaded"] = True
    return _memory


def remember(mirror):
    """记住这次成功的镜像，下次优先试它。"""
    if not mirror:
        return
    _memory["preferred"] = mirror
    try:
        with open(_state_path, "w", encoding="utf-8") as f:
            json.dump({"preferred": mirror, "at": time.time()}, f)
    except Exception:
        pass


def candidates(url):
    """生成候选 URL：直连 → 上次成功的镜像 → 其余镜像。"""
    yield url
    if not _is_github(url):
        return
    st = _load_state()
    ordered = [m for m in MIRRORS if m != st.get("preferred")]
    if st.get("preferred"):
        ordered.insert(0, st["preferred"])
    for base in ordered:
        yield base.rstrip("/") + "/" + url


def _is_github(url):
    low = (url or "").lower()
    return any(h in low for h in _MIRROR_HOSTS)


def patch(mods, bus=None):
    """给 ResourceFetcher 打补丁。幂等，重复调用无副作用。"""
    cls = mods["resource_fetcher"].ResourceFetcher
    if getattr(cls, "_glass_patched", False):
        return cls
    original = cls._make_request

    def patched(self, resource_url, timeout=None):
        """逐个候选源尝试，全失败才返回 None（调用方会继续自己的重试逻辑）。"""
        # 不采纳引擎传进来的 timeout：它写死 180 秒，一条坏连接能空转 3 分钟
        # （实测 SSL 握手卡到 196 秒，界面完全看不出是死了还是在下载）。
        # 这里统一用我们自己的值，超时就换下一个源，整体反而更快。
        use = REQUEST_TIMEOUT
        last = None
        for i, candidate in enumerate(candidates(resource_url)):
            try:
                r = original(self, candidate, timeout=use)
            except TypeError:
                r = original(self, candidate)
            if r is not None:
                if i > 0:
                    # 第 0 个候选是直连，>0 说明走了镜像
                    if bus:
                        bus.log(f"直连失败，已通过镜像取回资源", "info")
                    remember(_base_of(candidate, resource_url))
                return r
            last = candidate
        return None

    cls._make_request = patched
    cls._glass_patched = True

    # 只改 _make_request 还不够：慢速下载的超时常常发生在流式读取阶段，
    # 那时请求早已返回、换源逻辑根本不会触发。所以下载函数本身也要能轮换候选源。
    original_download = getattr(cls, "download_and_save_file", None)
    if original_download:
        def download(self, resource_url, destination_path, sha256_hash=None):
            last_error = None
            for i, candidate in enumerate(candidates(resource_url)):
                result = None
                try:
                    result = original_download(self, candidate, destination_path, sha256_hash)
                except Exception as e:
                    last_error = e
                if result:
                    if i > 0:
                        if bus:
                            bus.log("直连读取超时，已改由镜像完成下载", "info")
                        remember(_base_of(candidate, resource_url))
                    return result
                if i == 0 and bus:
                    bus.log("直连下载未成功，正在尝试备用源…", "warn")
            # 全部源都失败：必须把"为什么"说清楚。
            # 这类失败在界面上原本只表现为进度条卡住，用户会以为是软件坏了，
            # 实际是网络到不了 GitHub。
            if bus:
                reason = str(last_error)[:120] if last_error else "未知原因"
                bus.log(f"资源下载失败（{reason}）。已尝试直连与 {len(MIRRORS)} 个镜像。", "error")
                bus.log("这是网络问题，不是硬件或配置问题。可选做法：", "warn")
                bus.log("  1) 换个网络 / 挂代理后重试（生成一次后资源会缓存到本地）；", "warn")
                bus.log("  2) 手动下载对应资源放进 " + os.path.join(str(paths.CACHE), "OCK_Files")
                        + " 目录（文件名需与引擎期望的一致）；", "warn")
                bus.log("  3) 若只想要 EFI 的骨架，可改选较低版本或稍后再试。", "warn")
            if last_error:
                raise last_error
            return None

        cls.download_and_save_file = download

    # SSL：现在 certifi 已随工具安装，可以走验证过的上下文而不是 _create_unverified_context
    def create_ssl_context(self):
        import ssl
        try:
            import certifi  # noqa: PLC0415
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            try:
                return ssl.create_default_context()
            except Exception:
                return None

    cls.create_ssl_context = create_ssl_context
    return cls


def _base_of(candidate_url, original_url):
    """候选 URL 里剥出镜像前缀。"""
    if candidate_url == original_url:
        return None
    idx = candidate_url.find("https://", 1)
    return candidate_url[:idx] if idx > 0 else candidate_url[: candidate_url.find("/", 8) + 1]
