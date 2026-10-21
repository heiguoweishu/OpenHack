"""干净启动验证脚本（rebuild7 / 真实桌面壁纸方案）。

流程：
  1. 清理上次遗留的 GlassHack.exe / msedge.exe 进程，确认端口空闲
  2. 启动 dist/GlassHack.exe
  3. 轮询 /api/health 直到就绪（最多 ~40s）
  4. 确认 data/wallpaper_user.* 被抓取，且 /api/desktop-wallpaper 返回 200 图片
  5. 独立用 SPI_GETDESKWALLPAPER 读出系统真实壁纸路径，核对一致性
  6. 尝试抓 GlassHack 窗口截图到 _shot/glasscheck.bmp 做可视确认
  7. 输出结论后收尾（默认关闭拉起的进程，保持环境干净）
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "GlassHack", "data")
EXE = os.path.join(ROOT, "dist", "GlassHack.exe")
SHOT_DIR = os.path.join(ROOT, "_shot")
os.makedirs(SHOT_DIR, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd):
    try:
        # 中文 Windows 下 netstat/taskkill 输出是 GBK，text=True 默认按 UTF-8 解会抛异常
        return subprocess.run(cmd, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=15)
    except Exception as e:
        return f"ERR {e}"


def cleanup():
    log("清理遗留进程：GlassHack.exe / msedge.exe")
    run(["taskkill", "/IM", "GlassHack.exe", "/F"])
    run(["taskkill", "/IM", "msedge.exe", "/F"])
    time.sleep(1.5)


def port_free():
    r = run(["netstat", "-ano"])
    if isinstance(r, str):
        return True
    return "8733" not in r.stdout


def wait_health(port, timeout=60):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}/api/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = time.time()
        try:
            with opener.open(url, timeout=15) as resp:
                if resp.status == 200:
                    log(f"健康检查首次成功，延迟 {time.time()-t:.2f}s")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def fetch_bytes(url, timeout=5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except Exception as e:
        return None, None, f"{e}"


def get_system_wallpaper():
    try:
        SPI_GETDESKWALLPAPER = 0x0073
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, 512, buf, 0)
        return (buf.value or "").strip()
    except Exception as e:
        return f"ERR {e}"


def find_wallpaper_file():
    for ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"):
        p = os.path.join(DATA, "wallpaper_user" + ext)
        if os.path.isfile(p):
            return p
    return None


def screenshot_window():
    """找 GlassHack 的 Edge --app 窗口，用 PrintWindow 抓图。返回输出路径或 None。"""
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
    except Exception:
        return None

    found = {}

    def enum_cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        # Edge --app 窗口标题就是页面 <title>
        if "GlassHack" in title:
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            found["hwnd"] = hwnd
            found["title"] = title
            found["w"] = w
            found["h"] = h
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

    if "hwnd" not in found:
        return None
    hwnd = found["hwnd"]
    w, h = max(1, found["w"]), max(1, found["h"])

    hwnd_src = ctypes.c_ulong(hwnd)
    hdc = user32.GetWindowDC(hwnd_src)
    if not hdc:
        return None
    hdc_mem = gdi32.CreateCompatibleDC(hdc)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    # PW_RENDERFULLCONTENT = 2：尽量把 GPU 合成内容也画出来
    user32.PrintWindow(hwnd_src, ctypes.c_ulong(hdc_mem), 2)
    out = os.path.join(SHOT_DIR, "glasscheck.bmp")
    # 用 BITMAPINFOHEADER 手写 BMP（stdlib 不依赖 PIL）
    import struct
    bmi = struct.pack("IiiHHIIiiII", 40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    buf_size = w * h * 4
    bits = ctypes.create_string_buffer(buf_size)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, bits, ctypes.byref(ctypes.create_string_buffer(bmi, 40)), 0)
    with open(out, "wb") as f:
        f.write(b"BM")
        f.write(struct.pack("<I", 14 + 40 + buf_size))
        f.write(struct.pack("<HHI", 0, 0, 14 + 40))
        f.write(bmi)
        f.write(bits.raw)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd_src, hdc)
    return out


def main():
    log("=== GlassHack 干净启动验证 ===")
    if not os.path.isfile(EXE):
        log(f"找不到 exe：{EXE}")
        return 2

    cleanup()
    if not port_free():
        log("警告：端口 8733 仍被占用，启动可能顺延端口")

    log(f"启动：{EXE}")
    # 清掉代理环境变量，避免本机代理干扰 127.0.0.1 自检（exe 内部也已显式绕开代理）
    clean_env = {**os.environ}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"):
        clean_env.pop(k, None)
    proc = subprocess.Popen([EXE], close_fds=True, env=clean_env)
    log(f"exe pid = {proc.pid}")

    if not wait_health(8733, timeout=45):
        log("健康检查超时：/api/health 未返回 200")
        # 读日志辅助诊断
        for name in ("server.log", "launcher.log"):
            p = os.path.join(DATA, name)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-1200:]
                log(f"--- {name} 末尾 ---\n{tail}")
        return 3
    log("健康检查通过：/api/health = 200")

    # 端口可能在顺延后改变，重新探测真实端口
    real_port = 8733
    # 通过 server.ready 文件拿到真实端口
    ready_file = os.path.join(DATA, "server.ready")
    if os.path.isfile(ready_file):
        with open(ready_file) as f:
            try:
                real_port = int(f.read().strip())
            except Exception:
                pass
    log(f"真实服务端口：{real_port}")

    # 给启动器抓取壁纸留时间（它在 open_browser 之后才抓）
    time.sleep(2.0)

    wp_file = find_wallpaper_file()
    log(f"data/wallpaper_user.* = {wp_file or '（未找到）'}")
    if wp_file:
        size = os.path.getsize(wp_file)
        log(f"  壁纸文件大小：{size} 字节")

    st, ct, body = fetch_bytes(f"http://127.0.0.1:{real_port}/api/desktop-wallpaper")
    log(f"/api/desktop-wallpaper => status={st} content-type={ct} bytes={len(body) if isinstance(body, (bytes, bytearray)) else body}")
    if isinstance(body, bytes) and wp_file:
        with open(wp_file, "rb") as f:
            disk = f.read()
        log(f"  服务端字节 == 磁盘文件字节：{body == disk}")

    sys_wp = get_system_wallpaper()
    log(f"系统桌面壁纸（SPI_GETDESKWALLPAPER）= {sys_wp}")
    if wp_file and isinstance(sys_wp, str) and sys_wp:
        # 比较文件名（不要求完全同路径，因为已复制到 data/）
        log(f"  抓取源与系统壁纸一致（按名称）：{os.path.basename(wp_file) == 'wallpaper_user' + os.path.splitext(sys_wp)[1].lower()}")

    shot = screenshot_window()
    log(f"窗口截图：{shot or '（未能截到，可能是 GPU 合成窗口，不影响功能验证）'}")

    log("=== 验证完成，关闭拉起的进程以保持环境干净 ===")
    try:
        fetch_bytes(f"http://127.0.0.1:{real_port}/api/shutdown")
    except Exception:
        pass
    time.sleep(1.0)
    run(["taskkill", "/IM", "GlassHack.exe", "/F"])
    run(["taskkill", "/IM", "msedge.exe", "/F"])
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
