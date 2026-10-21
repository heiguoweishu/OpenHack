"""诊断：accent acrylic 为什么看不出效果。

先做对照实验，排除"抓图抓错了地方"这种低级可能：
  A. 把窗口涂成红色 → 抓到的应该是红色（证明 grab 看的就是这个窗口）
  B. 不涂色 + 施加 acrylic → 抓到的应该接近桌面的颜色层次

再扫几种常见的组合（是否 ExtendFrameIntoClientArea、不同 accent state、
不同 alpha），看哪一种真的能让窗口透出桌面。

判定标准：窗口内区域的平均色向"窗口外的桌面"靠拢，且标准差变大（出现桌面纹理）。
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
PAINT_MODE = {"mode": 0}   # 0=红底 1=不画

WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014

PAINTSTRUCT = ctypes.create_string_buffer(72)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                             ctypes.c_ulonglong, ctypes.c_ulonglong)

_defproc = u32.DefWindowProcW
_defproc.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ulonglong, ctypes.c_ulonglong]
_defproc.restype = ctypes.c_longlong


def _proc(hwnd, msg, wp, lp):
    if msg == WM_ERASEBKGND:
        return 1
    if msg == WM_PAINT and PAINT_MODE["mode"] == 0:
        ps = PAINTSTRUCT
        hdc = u32.BeginPaint(hwnd, ps)
        rect = wintypes.RECT(0, 0, W, H)
        # 注意：FillRect 在 user32 里，不在 gdi32 —— 之前调 g32.FillRect 会直接报找不到函数
        brush = g32.CreateSolidBrush(0x0000FF)   # COLORREF: BGR → 纯红 (0,0,255)
        u32.FillRect(hdc, ctypes.byref(rect), brush)
        g32.DeleteObject(brush)
        u32.EndPaint(hwnd, ps)
        return 0
    return _defproc(hwnd, msg, wp, lp)


_cb = WNDPROC(_proc)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def make_window():
    wc = WNDCLASS()
    wc.lpfnWndProc = _cb
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.hbrBackground = 0
    wc.lpszClassName = "AcrylicDiagCls"
    wc.hCursor = u32.LoadCursorW(0, 32512)
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "AcrylicDiagCls", "Diag", 0x00CF0000,
                               X, Y, W, H, 0, 0, k32.GetModuleHandleW(None), 0)
    u32.ShowWindow(hwnd, 5)
    u32.SetForegroundWindow(hwnd)
    return hwnd


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


def stats(px, w, h, step=4):
    rs = gs = bs = 0
    vals = []
    n = 0
    for y in range(0, h, step):
        row = y * w * 4
        for x in range(0, w, step):
            i = row + x * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            rs += r
            gs += g
            bs += b
            vals.append((r + g + b) / 3)
            n += 1
    mean = (rs / n, gs / n, bs / n)
    lum = sum(vals) / len(vals)
    sd = (sum((v - lum) ** 2 for v in vals) / len(vals)) ** 0.5
    return {"mean": tuple(round(v) for v in mean), "sd": round(sd, 2)}


def invalidate(hwnd):
    u32.InvalidateRect(hwnd, None, 1)
    u32.UpdateWindow(hwnd)


def main():
    hwnd = make_window()
    pump(1.0)
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"窗口 hwnd={hwnd} 可见={u32.IsWindowVisible(hwnd)} "
          f"位置=({r.left},{r.top})-({r.right},{r.bottom}) 期望=({X},{Y})-({X+W},{Y+H})")
    outside = stats(grab(X + W + 30, Y + 40, 400, 300), 400, 300)
    print("桌面(窗口外):", outside)

    # ---- A. 对照：涂红色，看抓图是否反映这个窗口 ----
    PAINT_MODE["mode"] = 0
    invalidate(hwnd)
    pump(1.0)
    a = stats(grab(X + 40, Y + 40, 400, 300), 400, 300)
    print("A 涂红后窗口内:", a, "  ← 应该是红色(R 高, G/B 低)")
    grab_ok = a["mean"][0] > 200 and a["mean"][2] < 60
    print("   抓取通道可信:", grab_ok)
    if not grab_ok:
        print("   !! 抓到的不是我们的窗口，后续结论不可信")

    # ---- 切换到"不画任何东西" ----
    PAINT_MODE["mode"] = 1
    invalidate(hwnd)
    pump(1.0)
    print("B 不画色(无 accent):", stats(grab(X + 40, Y + 40, 400, 300), 400, 300))

    combos = [
        ("accent acrylic, alpha=CC", dict(extend=False, state=4, tint=0xCCFFFFFF)),
        ("accent acrylic, alpha=40", dict(extend=False, state=4, tint=0x40FFFFFF)),
        ("accent acrylic + ExtendFrame(-1)", dict(extend=True, state=4, tint=0x40FFFFFF)),
        ("accent transparentgradient", dict(extend=True, state=2, tint=0x40FFFFFF)),
        ("accent blurbehind(legacy)", dict(extend=True, state=3, tint=0x40000000)),
    ]
    for name, cfg in combos:
        if cfg["extend"]:
            m = wintypes.INT(-1)
            try:
                ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                    hwnd, ctypes.byref(ctypes.c_int(-1) * 0 + m))  # 占位，下面用 MARGINS
            except Exception:
                pass
            try:
                class MARGINS(ctypes.Structure):
                    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]
                mm = MARGINS(-1, -1, -1, -1)
                ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(mm))
            except Exception as e:
                print("   ExtendFrame 失败:", e)
        ok = glass.set_accent(hwnd, cfg["state"], cfg["tint"])
        invalidate(hwnd)
        pump(1.2)
        time.sleep(0.4)
        st = stats(grab(X + 40, Y + 40, 400, 300), 400, 300)
        diff = max(abs(p - q) for p, q in zip(st["mean"], outside["mean"]))
        print(f"   {name:34s} set={ok} 窗口内={st} 与桌面差={diff}")

    u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    pump(0.4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
