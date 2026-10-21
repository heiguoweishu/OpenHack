"""端到端验证原生外壳：起 exe → 找窗口 → 看内容有没有真的画出来。

判定不看肉眼（agent 看图不方便），看像素统计：
  - 空白/纯白窗口 -> 颜色数极少、标准差接近 0
  - 正常渲染的界面 -> 有文字/卡片，颜色多、标准差明显大于 0
顺带确认后端进程被拉起、窗口没崩。
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, r"E:\BaiduSyncdisk\ai测试\黑苹果\GlassHack")
from app import glass  # noqa: E402

EXE = r"E:\BaiduSyncdisk\ai测试\黑苹果\GlassHack\dist\GlassHack.exe"
TITLE = "GlassHack"


def grab(x, y, w, h):
    import ctypes
    import struct
    u32 = ctypes.windll.user32
    g32 = ctypes.windll.gdi32
    hdc = u32.GetDC(0)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mem, bmp)
    g32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)
    bmi = struct.pack("IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mem, bmp, 0, h, buf,
                  ctypes.byref(ctypes.create_string_buffer(bmi, 40)), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(0, hdc)
    return bytes(buf.raw)


def stats(px, w, h, step=3):
    vals = []
    seen = set()
    for y in range(0, h, step):
        for x in range(0, w, step):
            i = (y * w + x) * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            vals.append((r + g + b) / 3)
            seen.add((r >> 4, g >> 4, b >> 4))
    m = sum(vals) / len(vals)
    sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
    return {"mean": round(m, 1), "sd": round(sd, 2), "colors": len(seen)}


def main():
    print("启动原生 exe:", EXE)
    p = subprocess.Popen([EXE], close_fds=True)
    print("pid =", p.pid)

    hwnd = glass.find_window(TITLE, timeout=30)
    print("窗口 hwnd:", hwnd)
    if not hwnd:
        print("!! 没找到窗口")
        return 1

    x, y, w, h = glass.window_rect(hwnd)
    print(f"窗口位置: ({x},{y}) {w}x{h}")

    # 等 WebView2 把页面渲染出来
    time.sleep(6)

    # 后端有没有被拉起来
    out = subprocess.run(["tasklist"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    core = [l for l in out.splitlines() if "GlassHackCore.exe" in l]
    print("后端进程:", "已启动" if core else "未找到")
    print("  ", core[0].strip() if core else "")

    # 抓窗口中央一块看看有没有内容
    gw, gh = min(600, w - 60), min(400, h - 60)
    gx, gy = x + (w - gw) // 2, y + (h - gh) // 2
    px = grab(gx, gy, gw, gh)
    st = stats(px, gw, gh)
    print("窗口中央像素统计:", st)

    rendered = st["colors"] > 8 or st["sd"] > 6
    print("判定:", "界面已渲染出内容 ✔" if rendered
          else "看起来还是一片空白（可能 WebView2 没加载成功）")

    # 存一份截图备查
    shot_dir = r"E:\BaiduSyncdisk\ai测试\黑苹果\GlassHack\_shot"
    os.makedirs(shot_dir, exist_ok=True)
    import struct
    fh = os.path.join(shot_dir, "native_glass.bmp")
    with open(fh, "wb") as f:
        f.write(b"BM" + struct.pack("<I", 14 + 40 + len(px)) +
                struct.pack("<HHI", 0, 0, 54) +
                struct.pack("IiiHHIIiiII", 40, gw, -gh, 1, 32, 0, len(px), 0, 0, 0, 0) + px)
    print("截图:", fh)

    print("关闭…")
    subprocess.run(["taskkill", "/IM", "GlassHack.exe", "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "GlassHackCore.exe", "/F"], capture_output=True)
    return 0 if rendered else 2


if __name__ == "__main__":
    sys.exit(main())
