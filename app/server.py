"""本地 HTTP 服务 + 事件推送。

重活（硬件扫描、EFI 构建、分区安装）都在独立子进程里跑，这里只负责转发它们的
结构化输出。worker 崩溃不会把服务带下水，UI 也能实时看到进度。
"""
import json
import os
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import disks as disks_api  # noqa: E402
from app import paths  # noqa: E402
from app import prereq  # noqa: E402
from app import ps  # noqa: E402
from app import recovery  # noqa: E402

WEB = str(paths.WEB)
PYTHON = sys.executable


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 静态资源 ----
    def translate_path(self, path):
        rel = (urlparse(path).path or "/").lstrip("/")
        if rel in ("", "/"):
            rel = "index.html"
        candidate = os.path.normpath(os.path.join(WEB, rel))
        if not candidate.startswith(os.path.abspath(WEB)):
            return candidate
        return candidate

    def log_message(self, fmt, *args):
        pass

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        route = u.path
        qs = parse_qs(u.query or "")

        try:
            if route == "/api/health":
                return self._json(self._health())
            if route == "/api/desktop-wallpaper":
                return self._wallpaper()
            if route == "/api/disks":
                return self._json({"disks": [disks_api.analyse(d) for d in disks_api.enum_disks()]})
            if route == "/api/shrink":
                return self._json({"volumes": disks_api.shrink_candidates()})
            if route == "/api/efi/list":
                return self._json({"sources": self._efi_sources()})
            if route == "/api/recovery/versions":
                return self._json({"versions": [_v(d) for d in sorted(recovery.TARGETS, reverse=True)]})
            if route == "/api/scan":
                return self._stream([PYTHON, "--worker", "app.worker_scan"])
            if route == "/api/analyze":
                report = (qs.get("report") or [""])[0]
                if not report:
                    return self._json({"error": "缺少 report 参数"}, 400)
                return self._stream([PYTHON, "--worker", "app.worker_efi", "--mode", "analyze",
                                     "--report", report])
            if route == "/api/build":
                report = (qs.get("report") or [""])[0]
                cfg = (qs.get("cfg") or ["{}"])[0]
                if not report:
                    return self._json({"error": "缺少 report 参数"}, 400)
                return self._stream([PYTHON, "--worker", "app.worker_efi", "--mode", "build",
                                     "--report", report, "--json", cfg])
            if route == "/api/install":
                cfg = (qs.get("cfg") or ["{}"])[0]
                return self._stream([PYTHON, "--worker", "app.worker_install", "--json", cfg])
            if route == "/api/open":
                target = (qs.get("path") or [""])[0] or str(paths.DATA)
                return self._json(self._open_folder(target))
            if route == "/api/shutdown":
                # 界面上的"退出"按钮走这里。先回包再停，否则连接会被自己掐断
                self._json({"ok": True})
                threading.Thread(target=self._delayed_exit, daemon=True).start()
                return None
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        return super().do_GET()

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        try:
            if u.path == "/api/open":
                return self._json(self._open_folder(payload.get("path") or str(paths.DATA)))
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        return super().do_POST()

    # ------------------------------------------------------------------ 各种处理

    def _delayed_exit(self, delay=0.6):
        """把响应发回去之后再停服务，否则客户端拿不到确认。"""
        time.sleep(delay)
        os._exit(0)

    def _wallpaper(self):
        """把启动器抓到的用户真实桌面壁纸吐给前端当玻璃背景。没有就 404（前端退回合成渐变）。"""
        import mimetypes
        for ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"):
            p = paths.DATA / ("wallpaper_user" + ext)
            if p.is_file():
                try:
                    data = p.read_bytes()
                except Exception:
                    break
                mt = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mt)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_response(404)
        self.end_headers()

    def _health(self):
        # 体检里要枚举磁盘 + 跑前置检查（都会 shell 出 PowerShell/diskpart），首请求偏慢。
        # 同一进程内缓存 30s：既让启动自检的多次轮询瞬间通过，也让界面刷新不卡。
        now = time.time()
        if _HEALTH_CACHE.get("data") is not None and now - _HEALTH_CACHE.get("ts", 0) < 30:
            return _HEALTH_CACHE["data"]
        try:
            disk = disks_api.enum_disks()
        except Exception as e:
            disk = []
            disk_error = str(e)
        else:
            disk_error = None
        try:
            check = prereq.check_all()
        except Exception as e:
            check = {"items": [], "blockers": [], "warnings": [], "ready": False,
                     "admin": False, "error": str(e)}
        result = {
            "admin": check.get("admin", False),
            "check": check,
            "disks": [{"number": d["number"], "name": d["name"], "size": d["size"],
                       "style": d["style"], "busType": d["busType"],
                       "partitions": len(d["partitions"])} for d in disk],
            "diskError": disk_error,
            "machine": _machine_brief(),
            "paths": {"data": str(paths.DATA), "results": str(paths.RESULTS),
                      "cache": str(paths.CACHE), "reports": str(paths.REPORTS)},
        }
        _HEALTH_CACHE["data"] = result
        _HEALTH_CACHE["ts"] = time.time()
        return result

    def _efi_sources(self):
        """可用的 EFI 来源：主要是构建产物，外加 EFI 分区上的候选。

        label 给完整描述，short 给下拉框用的短标签 —— 绝对路径塞进 select 一定会被截断。
        """
        out = []
        results = paths.RESULTS
        efi = os.path.join(str(results), "EFI")
        if os.path.isdir(efi):
            out.append({
                "id": str(results),
                "label": f"本次构建的 EFI（{results}）",
                "short": "本次构建的 EFI",
                "path": str(results),
                "built": True,
            })
        # 已经挂载的 EFI 分区也列出来，方便直接复用
        try:
            for d in disks_api.enum_disks():
                for p in d["partitions"]:
                    letter = (p.get("letter") or "").strip()
                    if not letter:
                        continue
                    if (p.get("gptType") or "").lower().strip("{}") != \
                            disks_api.GPT_ESP.strip("{}").lower():
                        continue
                    root = f"{letter}:\\"
                    if os.path.isdir(os.path.join(root, "EFI")):
                        out.append({
                            "id": root,
                            "label": f"磁盘 {d['number']} 的 EFI 分区 ({letter}:)",
                            "short": f"磁盘 {d['number']} 的 EFI 分区 ({letter}:)",
                            "path": root,
                            "built": True,
                        })
        except Exception:
            pass
        # 磁盘里已存在的 EFI 目录
        if os.path.isdir(results):
            for entry in sorted(os.listdir(results)):
                if entry.lower().startswith("efi"):
                    p = os.path.join(str(results), entry)
                    if os.path.isdir(p) and p not in [o["path"] for o in out]:
                        out.append({"id": p, "label": entry, "short": entry,
                                    "path": p, "built": False})
        return out

    def _open_folder(self, target):
        target = str(target or "").strip()
        if not target or not os.path.exists(target):
            return {"ok": False, "error": f"路径不存在：{target}"}
        try:
            if sys.platform == "win32":
                os.startfile(target)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _stream(self, cmd):
        """把 worker 的输出逐行转成 SSE。

        一律用子进程，两个模式都是：

          * 源码模式：引擎会动 cwd、会 print、还可能崩，隔离掉最稳；
          * 打包模式：主进程是 GUI 子系统，工作线程里的 WMI/COM 调用会挂死，
            而硬件采集非 WMI 不可。放到独立子进程里，COM 由它自己初始化，
            行为和控制台模式下完全一致。

        代价是每次多花一两秒重新解压 onefile 包 —— 可接受，而且子进程是
        GUI 子系统，不会再弹控制台窗口（以前"闪终端"的观感来源）。

        命令形如 [sys.executable, "--worker", "app.worker_scan", ...]，
        PyInstaller 的 bootloader 不认 -m，所以由 launcher 自己分发。
        """
        return self._stream_subprocess(cmd)

    def _stream_subprocess(self, cmd):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True, encoding="utf-8", errors="replace",
        )

        def pump():
            try:
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    chunk = f"data: {line}\n\n".encode("utf-8")
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionAbortedError, OSError):
                        proc.kill()
                        return
            except Exception:
                pass
            finally:
                try:
                    if proc.poll() is None:
                        proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                try:
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                except Exception:
                    pass

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        t.join()
        return None

    def _stream_inproc(self, cmd):
        """进程内执行 worker，用管道把它的输出接回来。"""
        module, rest = _split_cmd(cmd)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        read_fd, write_fd = os.pipe()
        done = threading.Event()

        def work():
            try:
                import importlib
                mod = importlib.import_module(module)
                with os.fdopen(write_fd, "w", encoding="utf-8", closefd=True) as out:
                    mod.run(rest, out)
            except Exception as e:
                import traceback
                try:
                    with os.fdopen(write_fd, "w", encoding="utf-8", closefd=True) as out:
                        out.write('{"t":"log","level":"error","msg":"worker %s 失败: %s"}\n'
                                  % (module, e))
                        out.write('{"t":"log","level":"error","msg":"%s"}\n'
                                  % " | ".join(traceback.format_exc().strip().splitlines()[-3:])[:400])
                        out.write('{"t":"done","exit":1}\n')
                except Exception:
                    pass
            finally:
                done.set()

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def pump():
            try:
                with os.fdopen(read_fd, "r", encoding="utf-8", closefd=True) as f:
                    for line in f:
                        line = line.rstrip("\r\n")
                        if not line:
                            continue
                        try:
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionAbortedError, OSError):
                            return
            except Exception:
                pass
            try:
                self.wfile.write(b"event: end\ndata: {}\n\n")
                self.wfile.flush()
            except Exception:
                pass

        p = threading.Thread(target=pump, daemon=True)
        p.start()
        p.join()
        done.wait(timeout=5)
        return None


_MACHINE_CACHE = {"value": None, "lock": threading.Lock()}

# 体检结果缓存：首请求会枚举磁盘 + 跑前置检查（shell 出 PowerShell/diskpart），偏慢；
# 同一进程内缓存 30s，避免启动自检的多次轮询和界面刷新被拖慢。
_HEALTH_CACHE = {"data": None, "ts": 0}


def _split_cmd(cmd):
    """把 [exe, "--worker", "app.worker_xxx", *args] 拆成 (模块名, 参数列表)。

    兼容旧的 "-m" 形式：源码模式下两种都出现过。
    """
    cmd = list(cmd or [])
    for flag in ("--worker", "-m"):
        if flag in cmd:
            i = cmd.index(flag)
            return cmd[i + 1], cmd[i + 2:]
    return cmd[0], cmd[1:]


def _machine_brief():
    """采集本机概要（结果缓存，一次开机内不会变）。

    走 PowerShell 而不是 wmi 模块：打包成 GUI 子系统的 exe 之后，
    工作线程里的 WMI/COM 调用会直接挂死（连接能建立、响应永远不来），
    而 PowerShell 是独立进程，源码模式与打包模式下行为完全一致。
    """
    with _MACHINE_CACHE["lock"]:
        if _MACHINE_CACHE["value"] is not None:
            return _MACHINE_CACHE["value"]
        script = r"""
$ErrorActionPreference = 'Stop'
$cs = Get-CimInstance Win32_ComputerSystem
$bb = Get-CimInstance Win32_BaseBoard
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$bios = Get-CimInstance Win32_BIOS | Select-Object -First 1
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })
@{
  manufacturer = $cs.Manufacturer
  model        = $cs.Model
  board        = ("" + $bb.Manufacturer + " " + $bb.Product).Trim()
  cpu          = ("" + $cpu.Name).Trim()
  cores        = $cpu.NumberOfCores
  threads      = $cpu.NumberOfLogicalProcessors
  memory       = [int64]$cs.TotalPhysicalMemory
  bios         = ("" + $bios.SMBIOSBIOSVersion)
  gpus         = $gpus
} | ConvertTo-Json -Compress
"""
        brief = {}
        try:
            data = ps.parse_json(script, timeout=60)
            brief = {
                "manufacturer": data.get("manufacturer") or "",
                "model": data.get("model") or "",
                "board": (data.get("board") or "").strip(),
                "cpu": (data.get("cpu") or "").strip(),
                "cores": data.get("cores"),
                "threads": data.get("threads"),
                "memory": int(data.get("memory") or 0),
                "memoryText": f"{int(data.get('memory') or 0) / 1024 ** 3:.0f} GB",
                "bios": (data.get("bios") or "").strip(),
                "gpus": list(data.get("gpus") or []),
            }
        except Exception as e:
            brief = {"error": str(e)}
        _MACHINE_CACHE["value"] = brief
        return brief


def _v(darwin):
    name, board = recovery.TARGETS[darwin]
    return {"darwin": darwin, "name": name, "major": recovery.MACOS_NAME.get(darwin, str(darwin))}


def pick_port(preferred):
    """端口被占用就顺延，直到拿到一个能绑定的。"""
    import socket
    for offset in range(0, 40):
        port = preferred + offset
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"端口 {preferred} 及之后 40 个都被占用")


def _reap_stale():
    """上一次没退干净的实例会占着端口，先收拾掉。

    写 pid 文件方便定位：Windows 上"找不到是谁占着端口"是常事，
    netstat 的输出还带本地化编码，解析起来很容易翻车。
    """
    pidfile = os.path.join(str(paths.DATA), "server.pid")
    try:
        if os.path.isfile(pidfile):
            with open(pidfile) as f:
                old = int((f.read() or "0").strip() or 0)
            if old and old != os.getpid():
                import subprocess as _sp
                _sp.run(["taskkill", "/PID", str(old), "/F"], capture_output=True)
    except Exception:
        pass
    try:
        os.makedirs(str(paths.DATA), exist_ok=True)
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _log(msg):
    """同时往控制台和日志文件写。

    用 pythonw.exe 启动时 sys.stdout 是 None，直接 print 会抛 AttributeError，
    表现为"双击后什么都没发生"——所以控制台输出必须容错。
    """
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        import time as _t
        with open(os.path.join(str(paths.DATA), "server.log"), "a", encoding="utf-8") as f:
            f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port_arg = 0
    if "--port" in argv:
        i = argv.index("--port")
        try:
            port_arg = int(argv[i + 1])
        except (IndexError, ValueError):
            port_arg = 0
    preferred = port_arg or int(os.environ.get("GLASSHACK_PORT", "8733"))
    _reap_stale()
    port = pick_port(preferred)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"

    # 用"就绪文件"告诉启动脚本服务已经能接受连接了。
    # 比在 bat 里做端口探测可靠得多 —— cmd 和 PowerShell 在处理编码/引号时都太容易翻车。
    ready_file = os.path.join(str(paths.DATA), "server.ready")
    try:
        os.makedirs(str(paths.DATA), exist_ok=True)
        with open(ready_file, "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass

    _log(f"GlassHack 服务已启动：{url}")

    # 启动自检：确认 PowerShell 这条采集通道是通的。
    # 主进程刻意不用 WMI —— GUI 子系统的 exe 里 COM 调用会挂死（连接能建立、响应永远不来），
    # PowerShell 是独立进程，源码模式与打包模式行为一致。这里先探一次，坏了日志里能直接看出来。
    _t0 = time.time()
    try:
        _info = _machine_brief()
        if _info.get("cpu"):
            _log(f"硬件通道就绪：{_info['cpu'][:40]}（{time.time() - _t0:.2f}s）")
        else:
            _log(f"硬件通道异常：{_info.get('error', '未返回 CPU 信息')}")
    except Exception as _e:
        _log(f"硬件通道异常：{type(_e).__name__}: {_e}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.remove(ready_file)
        except Exception:
            pass


if __name__ == "__main__":
    main()
