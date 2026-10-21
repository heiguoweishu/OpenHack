"""Worker 与宿主之间唯一的通信协议：一行一个 JSON 事件，写在 stdout 上。

之所以用独立子进程跑重活（而不是在 http server 里 import 过来直接跑），是因为：
  * OpCore-Simplify 与 Hardware-Sniffer 都会开子进程、动 cwd、打印大量日志，
    一旦它们崩了或者乱写全局状态，宿主进程不受影响；
  * py cpu 密集 + 网络下载不会阻塞 HTTP 服务。

事件类型：
  {"t":"log",    "level":"info|warn|error|ok", "msg":"..."}
  {"t":"step",   "name":"...", "index":1, "total":5}
  {"t":"progress","value":42, "label":"下载 WhateverGreen"}
  {"t":"result", "ok":true, "data":{...}}
  {"t":"done",   "exit":0}
"""
import json
import re
import sys
import threading

# ANSI 转义 CSI 序列。OpCore-Simplify 大量用 \033[1;36m 之类做彩色输出，
# 这里既要剥掉带 ESC 前缀的正规形式，也要处理 ESC 已被吃掉后残留的裸 "[0;31m"。
_ANSI = re.compile(r"(?:\x1b|\033)?\[[0-?]*[ -/]*[@-~]")
_ANSI_BARE = re.compile(r"\[[0-9;]*[a-zA-Z]")
_ANSI_LEFT = re.compile(r"\[[0-9;]*")


def strip_ansi(text):
    text = _ANSI.sub("", text)
    text = _ANSI_BARE.sub("", text)
    text = _ANSI_LEFT.sub("", text)
    return text


class EventStream:
    def __init__(self, stream=None):
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def _write(self, obj):
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (UnicodeEncodeError, ValueError):
                # Windows 控制台碰到奇怪字符时会炸，ASCII 兜底继续走
                safe = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
                self._stream.write(safe + "\n")
                self._stream.flush()

    def log(self, msg, level="info"):
        self._write({"t": "log", "level": level, "msg": str(msg)})

    def step(self, name, index=None, total=None):
        self._write({"t": "step", "name": name, "index": index, "total": total})

    def progress(self, value, label=""):
        # value 统一约束到 0-100，前端进度条才不会被撑爆
        try:
            v = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            v = 0
        self._write({"t": "progress", "value": v, "label": str(label)})

    def result(self, data, ok=True):
        self._write({"t": "result", "ok": bool(ok), "data": data})

    def done(self, exit_code=0):
        self._write({"t": "done", "exit": int(exit_code)})


class Steps:
    """把一串步骤按 0-100 摊开，每完成一步自动推进进度。"""

    def __init__(self, bus, names, offset=0, span=100):
        self.bus = bus
        self.names = names
        self.total = len(names)
        self.offset = offset
        self.span = span
        self.i = 0

    def enter(self, i=None):
        if i is not None:
            self.i = i
        name = self.names[self.i] if self.i < self.total else ""
        self.bus.step(name, self.i, self.total)
        self.bus.progress(self.offset + int(self.span * self.i / max(1, self.total)), name)

    def finish_all(self):
        self.bus.progress(self.offset + self.span, "")


class Tee:
    """把 library 里的 print/stdout 输出接回来，转成 log 事件推给前端。

    OpCore-Simplify 大量用 print 汇报进度，与其改它的代码，不如在 stdout 上架一层。
    """

    def __init__(self, bus, real):
        self.bus = bus
        self.real = real
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def _emit(self, raw):
        # 控制台里的进度条是靠 \r 刷同一行的，逐行转发会刷屏，同类文本要合并
        text = strip_ansi(raw.replace("\r", "")).strip()
        if not text:
            return
        # 某些受限运行环境会注入删除钩子，即使我们已经成功绕过它，它仍会往 stderr 打一行
        # "safe-delete ... FAIL_CLOSED"。这不是失败，是噪音，不该出现在用户看到的日志里。
        if "safe-delete" in text.lower() or "safe_delete" in text.lower():
            return
        level = "info"
        low = text.lower()
        if any(k in low for k in ("error", "fail", "exception", "traceback")):
            level = "error"
        elif any(k in low for k in ("warn", "note:", "deprecat")):
            level = "warn"
        elif text.startswith("  -") or text.startswith("-"):
            level = "info"
        self.bus.log(text[:400], level)

    def flush(self):
        if self._buf.strip():
            self._emit(self._buf)
            self._buf = ""
        try:
            self.real.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def fileno(self):
        try:
            return self.real.fileno()
        except Exception:
            raise ValueError("no fileno")

    def readline(self, *a):
        return self.real.readline(*a)


def install_tee(bus):
    """把 stdout/stderr 接管到 bus 上，并返回恢复函数。"""
    real_out, real_err = sys.stdout, sys.stderr
    tee_out = Tee(bus, real_out)
    tee_err = Tee(bus, real_err)
    sys.stdout, sys.stderr = tee_out, tee_err
    return real_out, real_err


def restore_tee(saved):
    real_out, real_err = saved
    try:
        sys.stdout.flush()
    except Exception:
        pass
    sys.stdout, sys.stderr = real_out, real_err
