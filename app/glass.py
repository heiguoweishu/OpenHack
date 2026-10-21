"""给窗口施加真正的系统级毛玻璃（Acrylic / Mica）。

参照 ExplorerBlurMica 的做法
----------------------------
那个项目给资源管理器上材质，靠的是两套 DWM 接口，而不是"自己画一层半透明"：

  * Windows 11 22H2+：DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE)
    2=Mica / 3=Acrylic(Transient) / 4=MicaAlt   —— 官方接口
  * Windows 10：未文档化但稳定的 SetWindowCompositionAttribute(WCA_ACCENT_POLICY)
    AccentState=4 → ACCENT_ENABLE_ACRYLICBLURBEHIND  —— ExplorerBlurMica 在 Win10 上就用这个

两者都是**由 DWM 合成器**实时模糊窗口背后的真实桌面：拖动窗口、切换壁纸都会跟着变，
和以前"截一张壁纸图片当背景"有本质区别 —— 那是静态贴图，这是操作系统给的材质。

为什么不用 WS_EX_LAYERED
------------------------
之前尝试过给 Edge 的 --app 窗口加 WS_EX_LAYERED + DwmEnableBlurBehindWindow，
结果是窗口直接消失（Chromium 的自绘 layer 与分层窗口冲突）。
ExplorerBlurMica 恰恰**不使用**分层窗口 —— 窗口保持普通窗口，只由 DWM 给背景材质。
所以这里也不动窗口样式，只调 DWM/accent 接口。
"""
import sys

import ctypes
from ctypes import wintypes

_IS_WIN = sys.platform == "win32"

# ------------------------------------------------------------------ 常量

# Windows 11 官方 backdrop（dwmapi）
DWMWA_SYSTEMBACKDROP_TYPE = 1029
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2        # Mica
DWMSBT_TRANSIENTWINDOW = 3   # Acrylic —— 最接近"液态玻璃"
DWMSBT_TABBEDWINDOW = 4      # Mica Alt

# Windows 10 accent（user32 未文档化导出）
WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_GRADIENT = 1
ACCENT_ENABLE_TRANSPARENTGRADIENT = 2
ACCENT_ENABLE_BLURBEHIND = 3          # Win7 那套老霜玻璃
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4   # Win10 Acrylic ← ExplorerBlurMica 用这个
ACCENT_ENABLE_HOSTBACKDROP = 5        # Win11 专用

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMCP_ROUND = 2

_WIN11_BUILD = 22621  # 22H2 起才有 SYSTEMBACKDROP_TYPE


class ACCENT_POLICY(ctypes.Structure):
    # nAccentState / nFlags / nColor / nAnimationId 的官方叫法
    _fields_ = [
        ("AccentState", wintypes.DWORD),
        ("AccentFlags", wintypes.DWORD),
        ("GradientColor", wintypes.DWORD),   # 0xAABBGGRR
        ("AnimationId", wintypes.DWORD),
    ]


class WINCOMPATTRDATA(ctypes.Structure):
    # cbData 实际是 SIZE_T：64 位下是 8 字节，写成 ULONG(4) 会让结构体尺寸算错，
    # 系统调用读到的 SizeOfData 就不对了 —— 这里是 ctypes 调用最容易翻车的地方
    _fields_ = [
        ("Attribute", wintypes.DWORD),
        ("Data", ctypes.POINTER(ACCENT_POLICY)),
        ("SizeOfData", ctypes.c_size_t),
    ]


# ------------------------------------------------------------------ 底层绑定

def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def _dwmapi():
    return ctypes.WinDLL("dwmapi", use_last_error=True)


def _get_swca():
    """拿到 SetWindowCompositionAttribute。

    它是未文档化接口，直接按名字取属性不一定成功，稳妥起见退到 GetProcAddress。
    """
    u = _user32()
    fn = getattr(u, "SetWindowCompositionAttribute", None)
    if fn:
        fn.restype = wintypes.BOOL
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
        return fn
    addr = ctypes.windll.kernel32.GetProcAddress(
        ctypes.windll.kernel32.GetModuleHandleW("user32"),
        b"SetWindowCompositionAttribute")
    if not addr:
        return None
    fn = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                            ctypes.POINTER(WINCOMPATTRDATA))(addr)
    return fn


def abgr(alpha, r, g, b):
    """拼成 accent 需要的 0xAABBGGRR（注意字节序和 ARGB 相反）。"""
    return ((alpha & 0xFF) << 24) | ((b & 0xFF) << 16) | \
           ((g & 0xFF) << 8) | (r & 0xFF)


def build_number():
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


# ------------------------------------------------------------------ 施加 / 清除

def set_accent(hwnd, accent_state=ACCENT_ENABLE_ACRYLICBLURBEHIND, color=0xCCFFFFFF):
    """Windows 10 的 Acrylic：由 DWM 实时模糊窗口背后的真实桌面。

    color 是叠加的色调，格式为 0xAABBGGRR。alpha 越小越透（桌面越明显），
    完全 0 会让浅色界面上的深色文字难以阅读，所以默认给一层接近白的淡色调
    （0xCCFFFFFF ≈ 80% 白），既透出桌面又保住可读性。
    """
    if not _IS_WIN:
        return False
    fn = _get_swca()
    if not fn:
        return False
    policy = ACCENT_POLICY()
    policy.AccentState = accent_state
    policy.AccentFlags = 2          # 2 = 所有边框都参与合成
    policy.GradientColor = color
    policy.AnimationId = 0
    data = WINCOMPATTRDATA()
    data.Attribute = WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(policy)
    data.SizeOfData = ctypes.sizeof(policy)
    try:
        return bool(fn(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def set_backdrop(hwnd, kind=DWMSBT_TRANSIENTWINDOW):
    """Windows 11 的官方 backdrop。kind: 2=Mica / 3=Acrylic / 4=MicaAlt。"""
    if not _IS_WIN:
        return False
    val = wintypes.INT(kind)
    try:
        hr = _dwmapi().DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0
    except Exception:
        return False


def round_corners(hwnd, enable=True):
    """顺手让圆角跟着系统走，无边框窗口不然会显得方。"""
    if not _IS_WIN:
        return False
    val = wintypes.INT(DWMCP_ROUND if enable else 1)
    try:
        return _dwmapi().DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(val), ctypes.sizeof(val)) == 0
    except Exception:
        return False


def apply_glass(hwnd, tint=0xCCFFFFFF):
    """给窗口上真毛玻璃。返回实际生效的方式，方便写日志排查。

    顺序：Win11 官方 backdrop → Win10 accent acrylic → 老版 blur。
    三条路都试一遍，谁成功用谁；全失败也不影响界面（前端会自动保留不透明底色）。
    """
    if not _IS_WIN:
        return "not-windows"

    if build_number() >= _WIN11_BUILD:
        if set_backdrop(hwnd, DWMSBT_TRANSIENTWINDOW):
            return "win11-acrylic(backdrop)"
        # Win11 上也可以用 host backdrop 的 accent
        if set_accent(hwnd, ACCENT_ENABLE_HOSTBACKDROP, tint):
            return "win11-hostbackdrop"

    if set_accent(hwnd, ACCENT_ENABLE_ACRYLICBLURBEHIND, tint):
        return "win10-acrylic"
    if set_accent(hwnd, ACCENT_ENABLE_BLURBEHIND, tint):
        return "legacy-blur"
    return "failed"


def clear_glass(hwnd):
    """恢复普通不透明窗口。"""
    if not _IS_WIN:
        return False
    ok = False
    ok |= set_accent(hwnd, ACCENT_DISABLED, 0)
    try:
        val = wintypes.INT(DWMSBT_AUTO)
        ok |= _dwmapi().DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(val), ctypes.sizeof(val)) == 0
    except Exception:
        pass
    return ok


# ------------------------------------------------------------------ 找窗口

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def enum_windows():
    out = []

    def cb(hwnd, _):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            out.append(int(hwnd))
        return True

    try:
        ctypes.windll.user32.EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception:
        pass
    return out


def window_title(hwnd):
    n = ctypes.windll.user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def find_window(substr, timeout=25.0, poll=0.4):
    """按标题找窗口（Edge --app 的窗口标题就是页面的 <title>）。"""
    import time
    end = time.time() + timeout
    while time.time() < end:
        for hwnd in enum_windows():
            try:
                if substr.lower() in window_title(hwnd).lower():
                    return hwnd
            except Exception:
                continue
        time.sleep(poll)
    return None


def window_rect(hwnd):
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def set_foreground(hwnd):
    try:
        ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(hwnd))
        return True
    except Exception:
        return False
