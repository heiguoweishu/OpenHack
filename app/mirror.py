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

REQUEST_TIMEOUT = 180

# 部分产物（OcBinaryData 之类的整仓 zip）在国内常常只有几十 KB/s，
# 一次 read() 卡住几十秒很常见 —— 超时给得太小就会前功尽弃。

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
        use = timeout if (timeout and timeout >= 30) else REQUEST_TIMEOUT
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
