"""判决实验：用自控的条纹背景，验证 acrylic 是否真的采样窗口背后的画面。

之前拿"窗口外的桌面"当参照不可靠 —— 那个区域可能压着别的白色窗口，
材质采到的本来就是白的，于是看起来像"没透"。

这次自己铺一块确定的参照物：
  背景窗画红/绿/蓝三条纯色竖条纹，探针窗盖在它正上方。
  accent 的叠加公式：结果 = alpha*色调 + (1-alpha)*模糊后的背景
  用白色调 alpha=0x40(25%) 时：
      红条纹理论值 = (255, ~64, ~64)     不是 (255,0,0)
      完全透明(无材质) 才会是        (255,   0,   0)
  只要 G/B 通道明显被抬起来，就说明材质确实在采样背后的画面。
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

X, Y, W, H = 150, 150, 720, 450
WM_PAINT = 0x000F
WM_CLOSE = 0x0010

MODE = {"bg": None, "probe": None}   # probe: 0=白底 1=不画

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             ctypes.c_ulonglong, ctypes.c_ulonglong)
_defproc = u32.DefWindowProcW
_defproc.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ulonglong, ctypes.c_ulonglong]
_defproc.restype = ctypes.c_longlong

PS = ctypes.create_string_buffer(72)


def paint_stripes(hdc):
    """三条竖条纹：红 / 绿 / 蓝"""
    colors = [0x0000FF, 0x00FF00, 0xFF0000]   # COLORREF=BGR → 红,绿,蓝
    bw = W // 3
    for i, c in enumerate(colors):
        rect = wintypes.RECT(i * bw, 0, (i + 1) * bw, H)
        br = g32.CreateSolidBrush(c)
        u32.FillRect(hdc, ctypes.byref(rect), br)
        g32.DeleteObject(br)


def _bg_proc(hwnd, msg, wp, lp):
    if msg == WM_PAINT:
        hdc = u32.BeginPaint(hwnd, PS)
        paint_stripes(hdc)
        u32.EndPaint(hwnd, PS)
        return 0
    return _defproc(hwnd, msg, wp, lp)


def _probe_proc(hwnd, msg, wp, lp):
    if msg == WM_PAINT and MODE["probe"] == 0:
        hdc = u32.BeginPaint(hwnd, PS)
        rect = wintypes.RECT(0, 0, W, H)
        br = g32.CreateSolidBrush(0xFFFFFF)
        u32.FillRect(hdc, ctypes.byref(rect), br)
        g32.DeleteObject(br)
        u32.EndPaint(hwnd, PS)
        return 0
    return _defproc(hwnd, msg, wp, lp)


_bg_cb = WNDPROC(_bg_proc)
_pr_cb = WNDPROC(_probe_proc)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def reg(cls, cb):
    wc = WNDCLASS()
    wc.lpfnWndProc = cb
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.hbrBackground = 0
    wc.lpszClassName = cls
    wc.hCursor = u32.LoadCursorW(0, 32512)
    u32.RegisterClassW(ctypes.byref(wc))


def make(cls, style=0x80000000):     # WS_POPUP
    return u32.CreateWindowExW(0, cls, "x", style | 0x10000000,  # WS_VISIBLE
                               X, Y, W, H, 0, 0, k32.GetModuleHandleW(None), 0)


def pump(sec=0.7):
    end = time.time() + sec
    msg = wintypes.MSG()
    while time.time() < end:
        while u32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def grab(x, y, w, h):
    import struct
    hdc = u32.GetDC(0)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mem, bmp)
    g32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    bmi = struct.pack("IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(ctypes.create_string_buffer(bmi, 40)), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)
    return bytes(buf.raw)


def mean_rgb(px, w, h):
    rs = gs = bs = 0
    n = 0
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            i = (y * w + x) * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            rs += r
            gs += g
            bs += b
            n += 1
    return (round(rs / n), round(gs / n), round(bs / n))


def stripe_centers():
    bw = W // 3
    return [(X + int(bw * (i + 0.5)) - 30, Y + 150, 60, 120) for i in range(3)]


def main():
    reg("StripeBgCls", _bg_cb)
    reg("ProbeCls2", _pr_cb)

    bg = make("StripeBgCls")
    pump(1.0)
    centers = stripe_centers()
    ref = [mean_rgb(grab(*c), c[2], c[3]) for c in centers]
    print("背景条纹参照  红/绿/蓝:", ref)

    probe = make("ProbeCls2")
    MODE["probe"] = 0                      # 先画不透明白底，证明它确实盖住了背景
    u32.InvalidateRect(probe, None, 1)
    pump(1.0)
    white = [mean_rgb(grab(*c), c[2], c[3]) for c in centers]
    print("探针画白底   :", white, " ← 应是接近 (255,255,255)")

    MODE["probe"] = 1                      # 不再绘制 → 露出材质
    for alpha, label in [(0x40, "alpha=0x40(25%白)"), (0xCC, "alpha=0xCC(80%白)")]:
        glass.set_accent(probe, glass.ACCENT_ENABLE_ACRYLICBLURBEHIND,
                         (alpha << 24) | 0x00FFFFFF)   # 0xAABBGGRR 白色调
        u32.InvalidateRect(probe, None, 1)
        pump(1.0)
        time.sleep(0.3)
        got = [mean_rgb(grab(*c), c[2], c[3]) for c in centers]
        print(f"{label}: {got}")

        pred = []
        a = alpha / 255
        for c in ref:
            pred.append(tuple(round(a * 255 + (1 - a) * v) for v in c))
        print(f"   混入白色后的理论值: {pred}")
        # 条纹能否被区分（相邻条纹差异）＝有没有真的采到背景
        spread = max(max(abs(g[i] - g2[i]) for i in range(3))
                     for g in got for g2 in got)
        print(f"   三条纹之间的最大通道差: {spread}（越大说明越能分辨出背景条纹）")

    u32.PostMessageW(probe, WM_CLOSE, 0, 0)
    u32.PostMessageW(bg, WM_CLOSE, 0, 0)
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
