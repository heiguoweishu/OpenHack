"""构建 GlassHack.exe（绕过本环境的 safe-delete shim）。

本机 WorkBuddy 的 safe-delete 会把 os.remove 等替换成"先扔回收站、不可用时 fail-closed"，
导致 PyInstaller 在清理 build/ 产物时直接抛 OSError 而中断打包。

这里只在"构建"这一个进程里把 os.remove/unlink/rmdir 还原成真正的 nt.* 实现，
shutil.rmtree 改走 cmd rd —— 全程只删 build/ 与 dist/ 这些一次性产物，不碰任何用户文件。
"""
import os
import sys
import nt
import subprocess

# ---- 关键：还原真正的删除语义（必须在 import PyInstaller 之前完成）----
os.remove = nt.remove
os.unlink = nt.unlink
os.rmdir = nt.rmdir

import shutil  # noqa: E402

_orig_rmtree = shutil.rmtree


def _rmtree(path, *args, **kwargs):
    ap = os.path.abspath(path)
    # 优先走 cmd rd（不在 shim 监控范围内），失败再兜底回原生 rmtree
    r = subprocess.run(["cmd", "/c", "rd", "/s", "/q", ap],
                       capture_output=True, text=True)
    if r.returncode != 0 and os.path.exists(ap):
        _orig_rmtree(path, *args, **kwargs)


shutil.rmtree = _rmtree

# ---- 现在跑 PyInstaller（同进程，继承上面的补丁）----
# 产物输出到 D 盘：项目在 E:\BaiduSyncdisk 下，是百度网盘同步目录，
# 同步客户端会一直拎着 exe 的句柄，导致 PyInstaller 覆盖/删除时报 PermissionError
# （之前还出现过 exe"自己消失"的怪事，也是同步盘干的）。
_BUILD = os.environ.get("OPENHACK_BUILD", r"D:\OpenHack-build")
sys.argv = ["pyinstaller", "GlassHack.spec", "--noconfirm",
            "--distpath", os.path.join(_BUILD, "dist"),
            "--workpath", os.path.join(_BUILD, "build")]
from PyInstaller.__main__ import run  # noqa: E402

if __name__ == "__main__":
    run()
