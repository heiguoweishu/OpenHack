"""GlassHack 的 exe 入口（也是 PyInstaller 的打包入口）。

职责很窄：把本地服务拉起来、把浏览器打开、然后守住进程。
真正干活的是 app.server —— 这里不掺和业务逻辑，免得打包后多一层要维护的东西。

保留控制台窗口是有意的：双击之后至少能看到"服务在哪个端口、有没有报错"，
比一个静默的进程好排查得多。关掉窗口就等于停掉服务。
"""
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app import server  # noqa: E402
from app import paths  # noqa: E402

# 给浏览器一个独立的用户数据目录，强制拉起"专属于本工具"的 Edge 窗口。
# 否则本机若已有 Edge 在跑（比如 WorkBuddy 的 WebView2），`--app=<url>` 会被并进
# 现有实例，既没有独立窗口可截图/可贴 DWM 毛玻璃，也认不准标题。
EDGE_PROFILE = str(paths.DATA / "edge_profile")


def _log(msg):
    """写日志文件。无控制台模式下 print 会抛异常（sys.stdout 是 None），必须容错。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        import time as _t
        # 必须写 paths.DATA：打包成 exe 后 __file__ 指向 _MEIPASS 临时解压目录，
        # 之前写在那里的日志会随目录一起消失 —— 表现就是 launcher.log 永远是空的，
        # 出问题时查不到任何线索。
        os.makedirs(str(paths.DATA), exist_ok=True)
        with open(os.path.join(str(paths.DATA), "launcher.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def _alert(title, text):
    """无控制台时至少弹个框，别让失败静默发生。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x30)
    except Exception:
        pass


def open_browser(port):
    """优先用 Edge/Chrome 的 app 模式，得到一个没有地址栏的干净窗口。"""
    url = f"http://127.0.0.1:{port}/"
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))

    import shutil
    import subprocess

    for exe in candidates:
        if not os.path.isfile(exe):
            continue
        try:
            subprocess.Popen([exe, f"--app={url}", "--new-window",
                              f"--user-data-dir={EDGE_PROFILE}",
                              "--window-size=1320,900",
                              "--disable-features=msEdgeOnScreenKeyboard"],
                             close_fds=True)
            return True
        except Exception:
            continue

    # 浏览器都不可用：退回系统默认
    import webbrowser
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


def _capture_desktop_wallpaper():
    """抓取用户当前的桌面壁纸，存到 data/wallpaper_user.* 供前端当玻璃背景用。

    为什么这样做而不是把浏览器窗口做成透明：本机 Edge 的 --app 窗口一旦被设为分层窗口
    再加 DWM 模糊就会崩溃/消失，透明透出真桌面的路走不通。而"用你自己的桌面壁纸当背景"
    是 100% 可靠的做法——毛玻璃卡片压在你真实的桌面壁纸上，观感就是"桌面在玻璃后面"，
    且不会出现黑屏或窗口消失。抓不到（比如纯色壁纸或无文件）就返回 None，前端退回合成渐变。
    """
    try:
        import ctypes
        from app import paths
        SPI_GETDESKWALLPAPER = 0x0073
        MAX = 512
        buf = ctypes.create_unicode_buffer(MAX)
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETDESKWALLPAPER, MAX, buf, 0)
        src = buf.value or ""
        if not src or not os.path.isfile(src):
            return None
        ext = os.path.splitext(src)[1].lower() or ".jpg"
        dst = paths.DATA / ("wallpaper_user" + ext)
        paths.DATA.mkdir(parents=True, exist_ok=True)
        # 覆盖写：先删旧的，避免不同格式残留
        if dst.exists():
            try:
                os.remove(dst)
            except OSError:
                pass
        shutil.copyfile(src, dst)
        return str(dst)
    except Exception:
        return None


def _install_no_window():
    """让本进程里所有 subprocess 调用都不弹控制台窗口。

    OpenHackCore.exe 是无控制台的 GUI 程序。它派生控制台子程序（powershell /
    diskpart / acpidump / wmic 等）时，如果没带 CREATE_NO_WINDOW，Windows 会为
    每条命令新建一个黑色控制台窗口 —— 用户看到的就是"扫描时跳出一堆终端窗口"。

    直接替换 subprocess.Popen 最省事：vendor 引擎（Hardware-Sniffer /
    OpCore-Simplify）内部的调用也一并覆盖，不用逐个去改。
    """
    if sys.platform != "win32":
        return
    import os as _os
    import subprocess
    if getattr(subprocess.Popen, "_open_hack_no_window", False):
        return

    _orig = subprocess.Popen
    CREATE_NO_WINDOW = 0x08000000

    class _NoWindowPopen(_orig):
        _open_hack_no_window = True

        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

    # os.system 走的是 cmd /c，**不受 Popen 补丁影响**，照样会闪一个黑窗。
    # 上游引擎（Hardware-Sniffer 的 Utils.head）开场会执行 os.system('cls') 清屏，
    # 在 GUI 程序里这就是"扫描时窗口闪退一下"的来源。
    # 处理办法：清屏类调用直接忽略（GUI 下没有控制台可清），其余命令改走
    # 带 CREATE_NO_WINDOW 的 subprocess，彻底不弹窗。
    if not getattr(_os.system, "_open_hack_no_window", False):
        _orig_system = _os.system

        def _no_window_system(command):
            text = str(command).strip().lower()
            if text in ("cls", "clear", "cls.exe", "cmd /c cls", "cmd /c clear"):
                return 0
            try:
                return subprocess.call(text, shell=True,
                                       creationflags=CREATE_NO_WINDOW,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            except Exception:
                return 1

        _no_window_system._open_hack_no_window = True
        _os.system = _no_window_system


def _run_worker(args):
    """worker 分发：`GlassHack.exe --worker app.worker_scan --out ...`。

    PyInstaller 的 bootloader 不认 `-m`，所以不能用 `exe -m app.worker_scan`
    的方式起子进程；入口脚本自己按参数分发。

    另一个原因更重要：主进程是 GUI 子系统，工作线程里的 WMI/COM 调用会挂死，
    而硬件采集必须用 WMI。放到独立子进程里跑就绕开了这个问题 ——
    子进程自己初始化 COM，行为和控制台模式下一致。
    """
    if not args:
        return 2

    _install_no_window()

    # 打包成 GUI 子系统的 exe 后，sys.stdout 的编码会落到系统代码页（简中是 cp936/GBK），
    # 而父进程（server）是按 UTF-8 读的 → 中文全变乱码。
    # 这里强制把两个流统一成 UTF-8，这是乱码的唯一根因。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    import importlib

    try:
        mod = importlib.import_module(args[0])
    except Exception as e:
        sys.stderr.write(f"无法加载 worker {args[0]}: {e}\n")
        return 2

    out = sys.stdout
    if out is None:
        # GUI 子系统启动时可能没有标准输出；退到 stderr，父进程那边是合并捕获的
        out = sys.stderr
    return mod.run(args[1:], out)


def _wv2_runtime_version():
    """当前安装的 WebView2 Runtime 版本（取 EdgeWebView 下最大的版本目录名）。"""
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        d = os.path.join(base, "Microsoft", "EdgeWebView", "Application")
        if not os.path.isdir(d):
            continue
        vers = [n for n in os.listdir(d) if n and n[0].isdigit()]
        if vers:
            return max(vers)
    return ""


def _mark_wv2_broken():
    """记下"这台机器当前这个版本的 WebView2 渲染会崩"。

    原生外壳下次启动看到标记就不再去试（否则每次双击都要先闪一个窗口再跳浏览器）。
    标记里带着版本号：用户哪天升级了 WebView2，版本一变标记自动失效，会重新尝试原生。
    """
    try:
        v = _wv2_runtime_version()
        if not v:
            return
        p = os.path.join(str(paths.BASE), "wv2_unsupported.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(v)
        _log("已记录 WebView2 不可用（Runtime %s），下次直接走浏览器" % v)
    except Exception as e:
        _log("记录 WebView2 标记失败：%s" % e)


def _watch_native_shell(pid, port, timeout=60.0):
    """盯着原生外壳进程，崩了就兜底把界面打开。

    背景：WebView2 的渲染进程在部分机器上起不来，一崩就是整个原生 exe 消失
    （"双击之后什么都没有"）。此时后端还活着，所以由后端来兜底：
      · 页面真的跑起来时，原生外壳会写 native.ready（内容是它自己的 pid）
      · 如果在那之前进程就没了 —— 说明它崩了，改用系统浏览器打开界面
    这样用户最多是"玻璃没出来"，不会变成"软件打不开"。
    """
    import ctypes

    def alive(p):
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(p))  # SYNCHRONIZE
            if not h:
                return False
            rc = ctypes.windll.kernel32.WaitForSingleObject(h, 0)
            ctypes.windll.kernel32.CloseHandle(h)
            return rc == 0x102          # WAIT_TIMEOUT = 还在跑
        except Exception:
            return False

    # 原生外壳把就绪标记写在 BASE 下（不是 data 子目录），文件名带它自己的 pid
    ready_file = os.path.join(str(paths.BASE), "native_%s.ready" % pid)

    def ready():
        try:
            if os.path.isfile(ready_file):
                # 原生外壳用 fwprintf 写的，是 UTF-16LE 无 BOM
                with open(ready_file, encoding="utf-16-le") as f:
                    return (f.read() or "").strip() == str(pid)
        except Exception:
            pass
        return False

    # 先清掉残留，否则可能拿旧内容误判成"已经就绪"
    try:
        with open(ready_file, "w"):
            pass
    except Exception:
        pass

    end = time.time() + timeout
    while time.time() < end:
        if ready():
            _log("原生外壳页面已就绪，取消回退监视")
            return
        if not alive(pid):
            _log(f"原生外壳进程 {pid} 在页面就绪之前就退出了 —— 回退：改用系统浏览器打开界面")
            _mark_wv2_broken()
            try:
                ok = open_browser(port)
                _log("回退已打开浏览器" if ok else "回退也没能打开浏览器")
            except Exception as e:
                _log(f"回退失败：{e}")
            return
        time.sleep(0.5)

    # 超时也要兜：可能进程还活着，但页面始终没起来（例如窗口一片空白卡在那儿）
    if not ready():
        _log("超时仍未确认页面就绪 —— 回退：改用系统浏览器打开界面")
        try:
            ok = open_browser(port)
            _log("回退已打开浏览器" if ok else "回退也没能打开浏览器")
        except Exception as e:
            _log(f"回退失败：{e}")
        return
    _log("回退监视结束（页面已就绪）")


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":
        return _run_worker(argv[1:])

    _install_no_window()

    # --no-browser：被原生外壳（native/GlassHack.exe）当后端调用时使用。
    # 那种模式下窗口由原生 exe 自己建、自己上系统级毛玻璃，
    # 这里再开一个 Edge 窗口只会多出一个空白壳子，所以跳过开浏览器。
    headless = "--no-browser" in argv

    # --watch <pid>：由原生外壳（GlassHack.exe）传入它自己的进程号。
    # 用途见下面 _watch_native_shell 的说明。
    watch_pid = None
    if "--watch" in argv:
        try:
            watch_pid = int(argv[argv.index("--watch") + 1])
        except (IndexError, ValueError):
            watch_pid = None

    port = server.pick_port(int(os.environ.get("GLASSHACK_PORT", "8733")))
    url = f"http://127.0.0.1:{port}/"
    _log("启动中，端口 %d%s" % (port, "（无界面模式，供原生外壳调用）" if headless else ""))

    t = threading.Thread(target=lambda: server.main(["--port", str(port)]), daemon=True)
    t.start()

    # 等服务真正能响应再开浏览器，否则首屏可能白一下
    import urllib.request
    # 必须显式绕开代理：本机自检的地址是 127.0.0.1，走系统代理会被转出去，
    # 轻则多绕一圈，重则永远连不上 —— 而这里的失败会被当成"启动失败"直接退出。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ready = False
    # 冷启动（首次请求会触发硬件/磁盘枚举，本机可能要几秒）也要等得起：
    # 单次探测超时放宽到 15s，总体最多等 60s 再放弃。
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with opener.open(url + "api/health", timeout=15) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        # 无控制台模式下失败是静默的，至少弹个框说一声
        _alert("OpenHack 启动失败",
               "本地服务没有在预期时间内就绪。\n\n"
               "请查看 data\\launcher.log 与 data\\server.log 里的报错，"
               "或尝试以管理员身份运行。")
        _log("服务未就绪，已放弃")
        return 1

    if watch_pid:
        threading.Thread(target=_watch_native_shell, args=(watch_pid, port),
                         daemon=True).start()
        _log("已启动原生外壳回退监视（pid=%s）" % watch_pid)

    # 抓取用户真实桌面壁纸，作为玻璃背景（比假渐变更像"桌面在玻璃后"）。
    # 这一步与浏览器能否拉起无关，先备好再说：失败（纯色壁纸/无文件）就留空，
    # 前端退回合成渐变兜底。
    wp = _capture_desktop_wallpaper()
    _log("桌面壁纸背景：%s" % ("已抓取" if wp else "不可用，退回合成渐变"))

    ok = True
    if not headless:
        ok = open_browser(port)
        if not ok:
            _alert("OpenHack", f"没能自动打开浏览器，请手动访问：\n\n{url}")
            _log("未能打开浏览器")
        else:
            _log("界面已打开：%s" % url)

    # 界面里的"退出"按钮会调 /api/shutdown；这里守着服务线程，它一退就收尾
    try:
        while t.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    _log("服务已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
