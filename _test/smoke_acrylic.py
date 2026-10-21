"""冒烟测试：验证 accent acrylic 是否真的把桌面透到我们的窗口背后。

做法（尽量客观，不依赖肉眼）：
  1. 用纯 ctypes 建一个普通 Win32 窗口（不用 WS_EX_LAYERED），且不绘制任何底色
  2. 先抓一次屏幕：窗口区域 vs 窗口外紧贴的桌面条带，算平均 RGB 与标准差
  3. 施加 ACCENT_ENABLE_ACRYLICBLURBEHIND
  4. 再抓一次，同样对比
  5. 判定：施加后「窗口内」应该向「桌面」靠拢（颜色出现层次、均值接近），
     且不再是施加前的那种死板单色

注意用的是 GetDC(NULL) 的屏幕 BitBlt —— 抓的是 DWM 合成后的真实画面，
比 PrintWindow 更接近人眼所见（PrintWindow 对 GPU 合成窗口常常是空白）。
"""
import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, r"E:\BaiduSyncdisk\ai测试\黑苹果\GlassHack")
from app import glass  # noqa: E402

u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32
k32 = ctypes.windll.kernel32

X, Y, W, H = 120, 120, 640, 420


# ---------------------------------------------------------------- 抓像素
def grab(x, y, w, h):
    """抓一块屏幕区域的原始像素，返回 bytes（BGRA, top-down）。"""
    hdc = u32.GetDC(0)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mem, bmp)
    g32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)  # SRCCOPY

    import struct
    bmi = struct.pack("IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(ctypes.create_string_buffer(bmi, 40)), 0)

    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)
    return bytes(buf.raw)


def stats(pixels, w, h, step=4):
    """平均色 + 标准差 + 颜色数：用来判断"有没有真的透出桌面"。"""
    rs = gs = bs = 0
    vals = []
    n = 0
    seen = set()
    for y in range(0, h, step):
        row = y * w * 4
        for x in range(0, w, step):
            i = row + x * 4
            b, g, r = pixels[i], pixels[i + 1], pixels[i + 2]
            rs += r
            gs += g
            bs += b
            vals.append((r + g + b) / 3)
            seen.add((r >> 3, g >> 3, b >> 3))
            n += 1
    mean = (rs / n, gs / n, bs / n)
    lum = sum(vals) / len(vals)
    var = sum((v - lum) ** 2 for v in vals) / len(vals)
    return {
        "mean": tuple(round(v, 1) for v in mean),
        "stddev": round(var ** 0.5, 2),
        "colors": len(seen),
    }


# ---------------------------------------------------------------- 建窗口
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DESTROY = 0x0002

_defproc = u32.DefWindowProcW
# 必须用 64 位无符号接住 lp：某些消息的 LPARAM 是大于 2^63 的指针值，
# 默认类型转换会抛 "int too long to convert"
_defproc.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ulonglong, ctypes.c_ulonglong]
_defproc.restype = ctypes.c_longlong


def _proc(hwnd, msg, wp, lp):
    if msg == WM_ERASEBKGND:
        # 不擦背景 —— 让 DWM 的材质露出来，这也是 ExplorerBlurMica 那一类做法的前提
        return 1
    return _defproc(hwnd, msg, wp, lp)


_cb = WNDPROC(_proc)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def make_window():
    wc = WNDCLASS()
    wc.lpfnWndProc = _cb
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.hbrBackground = 0                      # 没有背景画刷 → 不自己铺底色
    wc.lpszClassName = "GlassHackAcrylicProbe"
    wc.hCursor = u32.LoadCursorW(0, 32512)     # IDC_ARROW
    u32.RegisterClassW(ctypes.byref(wc))

    hwnd = u32.CreateWindowExW(
        0,                                     # ← 关键：不加 WS_EX_LAYERED
        "GlassHackAcrylicProbe", "Acrylic Probe",
        0x00CF0000,                            # WS_OVERLAPPEDWINDOW
        X, Y, W, H, 0, 0, k32.GetModuleHandleW(None), 0)
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    return hwnd


def pump(seconds=0.8):
    end = time.time() + seconds
    msg = wintypes.MSG()
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def main():
    print("build:", glass.build_number())
    print("win11 官方 backdrop 可用:", glass.build_number() >= glass._WIN11_BUILD)

    hwnd = make_window()
    print("窗口 hwnd:", hwnd)
    pump(1.0)

    # 窗口内 vs 窗口外（紧贴右侧的一块桌面）
    inside_before = stats(grab(X + 40, Y + 40, 400, 300), 400, 300)
    outside = stats(grab(X + W + 30, Y + 40, 400, 300), 400, 300)
    print("\n[施加前]")
    print("  窗口内:", inside_before)
    print("  窗口外(桌面):", outside)

    which = glass.apply_glass(hwnd, tint=0x66FFFFFF)   # 偏透的白色调
    print("\napply_glass ->", which)
    pump(2.0)
    time.sleep(1.0)

    inside_after = stats(grab(X + 40, Y + 40, 400, 300), 400, 300)
    print("\n[施加后]")
    print("  窗口内:", inside_after)
    print("  窗口外(桌面):", outside)

    db = max(abs(a - b) for a, b in zip(inside_before["mean"], outside["mean"]))
    da = max(abs(a - b) for a, b in zip(inside_after["mean"], outside["mean"]))
    print("\n=== 判定 ===")
    print(f"  施加前 窗口内与桌面的平均色最大差: {db}")
    print(f"  施加后 窗口内与桌面的平均色最大差: {da}")
    print(f"  颜色数 {inside_before['colors']} -> {inside_after['colors']}  "
          f"标准差 {inside_before['stddev']} -> {inside_after['stddev']}")

    transparent = da < db or inside_after["stddev"] > inside_before["stddev"] + 2
    print("\n结论:", "ACRYLIC 生效（桌面透出来了）" if transparent
          else "看不出透出 —— accent 可能没起作用")

    u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    pump(0.5)
    return 0 if transparent else 1


if __name__ == "__main__":
    sys.exit(main())
