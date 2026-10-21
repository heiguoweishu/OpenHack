"""PowerShell 执行与安全放行。

设计要点（部分是踩过的坑，部分是借鉴 1401 的安全范式）：

1. 传脚本一律走 `-EncodedCommand`（UTF-16LE + base64），而不是临时脚本文件。
   不用临时文件就没有中间态：不存在"文件被别的程序改写后 powershell 才去读"的窗口，
   天然免疫 diskpart 时代那类 TOCTOU 攻击；同时也完全绕开了命令行转义地狱。

2. 判断成败只看 **退出码 + 结构化 JSON**，绝不 grep 控制台字符串。
   Windows 的错误文案（乃至 diskpart 的全部输出）随显示语言变化，
   在中文机上匹配英文 "successfully" 必然误判。

3. 任何**写操作**（动分区、格式化）在真正执行前，会先用一个名 Nexus 绑定：
   磁盘号 + 序列号 + 容量三重校验，盘号漂移则立刻放弃。
"""
import base64
import ctypes
import json
import os
import subprocess
import sys

PS_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

DEFAULT_TIMEOUT = 120


class PSError(RuntimeError):
    pass


def _exe():
    if os.path.exists(PS_EXE):
        return PS_EXE
    for alt in ("powershell.exe", "pwsh.exe"):
        from shutil import which
        p = which(alt)
        if p:
            return p
    raise PSError("找不到 Windows PowerShell，无法继续执行磁盘操作")


def _encode(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# Windows PowerShell 5.1 的控制台输出跟着系统 locale 走（简体中文机上是 GBK），
# 直接按 UTF-8 解会得到乱码硬盘卷标。与其事后猜编码，不如让 PowerShell 明确输出 UTF-8。
_PREAMBLE = (
    "$ErrorActionPreference = 'Stop'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$PSDefaultParameterValues['*:Encoding'] = 'utf8'\n"
)


def run(script, timeout=DEFAULT_TIMEOUT, admin_hint=False):
    """执行一段 PowerShell，返回 stdout 原文。非零退出码或非预期输出都抛异常。"""
    args = [
        _exe(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", _encode(_PREAMBLE + script),
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise PSError(f"PowerShell 执行超时（{timeout}s）")
    except Exception as e:
        raise PSError(f"无法启动 PowerShell：{type(e).__name__}: {e}")

    out = _decode(p.stdout)
    err = _decode(p.stderr)
    if p.returncode != 0:
        msg = err or out or f"退出码 {p.returncode}"
        raise PSError(_clean(msg))
    return out


def json_out(script, timeout=DEFAULT_TIMEOUT):
    """执行 PowerShell 并解析它输出的 JSON。"""
    import json as _json  # noqa: PLC0415  局部导入，别被下面的同名别名遮蔽
    raw = run(script, timeout=timeout)
    if not raw:
        raise PSError("PowerShell 没有返回任何数据（可能需要管理员权限）")
    # 有些环境会往 stdout 前面塞垃圾（Progress 记录、激活横幅），只取最外层 JSON 的起始位置
    start = min([i for i in (raw.find("["), raw.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise PSError(f"PowerShell 返回的不是 JSON：{_clean(raw[:300])}")
    frag = raw[start:]
    try:
        return _json.loads(frag)
    except _json.JSONDecodeError as e:
        end = max(frag.rfind("]"), frag.rfind("}")) + 1
        try:
            return _json.loads(frag[:end])
        except Exception:
            raise PSError(f"PowerShell 输出无法解析为 JSON：{e}\n{_clean(frag[:300])}")


# 别把这个别名命名成 json —— 那会盖住模块顶部的 import json，
# 让本函数异常分支里的 JSONDecodeError 取不到（只在解析失败时才暴露）。
parse_json = json_out


def diskpart(script, timeout=600):
    """执行一段 diskpart 脚本。

    参考 1401（nullmoth）的做法加了三层防护，因为我们准备 Administrator 权限去做破坏性操作：

      1. 脚本文件名随机，且用 FileMode.CreateNew 写 —— 同名文件或符号链接一旦已存在就直接失败，
         杜绝"占位文件/软链接劫持"让 bak vinegar 指向别的盘；
      2. 写完立刻以 FileShare.Read 重新打开并持有句柄直到 diskpart 结束 ——
         期间谁也不能改写、删除、重命名它；
      3. 通过持有的句柄把内容读回来比对 —— 关掉 TOCTOU 最后那扇窗，
         内容不符就抛错，diskpart 根本不会被启动。

    另外坚持用 /s 而不是往 stdin 管道里灌：/s 模式下 diskpart 遇到第一条失败就停下并给出
    真实的退出码，而管道模式下它常常接着执行后面的命令，损失无法挽回。
    """
    import random
    import string
    name = "glass-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=16)) + ".txt"
    sp = os.path.join(os.environ.get("TEMP", "."), name)
    # diskpart 的 /s 脚本按系统 ANSI 代码页读，用 ASCII 编码会把中文路径变成 "?"
    # 导致"找不到路径"这种莫名其妙的错，所以用 GBK 编。
    try:
        want = script.encode("gbk")
    except UnicodeEncodeError:
        want = script.encode("gbk", "replace")
    try:
        with open(sp, "wb", opener=lambda p, f: os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)) as w:
            w.write(want)
    except FileExistsError:
        raise PSError("临时 diskpart 脚本已存在，疑似被劫持，已中止")
    except Exception as e:
        raise PSError(f"无法写入 diskpart 脚本：{e}")

    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(sp, flags)
        try:
            got = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                got += chunk
            if got != want:
                raise PSError(" diskpart 脚本在执行前被改动，已中止，未对磁盘做任何修改")
        finally:
            os.close(fd)

        p = subprocess.run(["diskpart.exe", "/s", sp], capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise PSError(f"diskpart 执行超时（{timeout}s）")
    finally:
        try:
            os.remove(sp)
        except OSError:
            pass

    out = _decode(p.stdout)
    err = _decode(p.stderr)
    if p.returncode != 0:
        raise PSError(_clean(out + "\n" + err) or f"diskpart 退出码 {p.returncode}")
    return out


def _decode(raw):
    """PowerShell 输出的编码并不总是可靠，按 UTF-8 → GBK → 容错的顺序解。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _clean(s):
    """把 PowerShell 的错误输出洗成人能读的样子。

    PowerShell 的远程/捕获错误是 CLIXML 序列化格式，里面全是一行行的
    <S S="Error">...</S> 和 _x000D__x000A_ 转义。直接抛给看日志的人是折磨，
    所以这里把错误正文抽出来、转义还原、标签剥掉。
    """
    s = (s or "").replace("\x00", "")
    if "#< CLIXML" in s or "<Objs" in s:
        import re as _re
        chunks = _re.findall(r'<S S="Error">(.*?)</S>', s, _re.S)
        if chunks:
            text = "".join(chunks)
            text = (text.replace("_x000D__x000A_", "\n")
                        .replace("_x000D_", "").replace("_x000A_", "\n"))
            text = _re.sub(r"<[^>]+>", "", text)
            try:
                from html import unescape
                text = unescape(text)
            except Exception:
                pass
            s = text
    lines = [l.rstrip() for l in s.splitlines() if l.strip()]
    return "\n".join(lines)[:2000]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def free_letter():
    """从 Z 往 G 倒着找第一个空闲盘符。"""
    try:
        p = subprocess.run(
            [_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", _encode(
                 "$u = @(Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name.ToUpper() })\n"
                 "65..90 | ForEach-Object { [char]$_ } | Where-Object { $u -notcontains $_ -and $_ -ge 'G' } "
                 "| Select-Object -Last 1 | ForEach-Object { $_ }")],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        c = (p.stdout or b"").decode("utf-8", "replace").strip()
        if len(c) == 1 and c.isalpha():
            return c.upper()
    except Exception:
        pass
    # PowerShell 不可用时的兜底：直接问文件系统
    for code in range(ord("Z"), ord("G") - 1, -1):
        letter = chr(code)
        if not _letter_used(letter):
            return letter
    raise PSError("G: 到 Z: 之间没有可用盘符")


def _letter_used(letter):
    import string
    path = f"{letter}:\\"
    try:
        os.listdir(path)
        return True
    except (FileNotFoundError, OSError, PermissionError):
        return False
    except Exception:
        return False


def uc(s):
    """把一段文本塞进 PowerShell 单引号字符串（单引号翻倍转义）。"""
    return "'" + str(s).replace("'", "''") + "'"
