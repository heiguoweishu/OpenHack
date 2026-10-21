"""编译 GlassHack 原生外壳（Win32 + DWM 毛玻璃 + 透明 WebView2）。

工具链用 llvm-mingw（放在 D:\\DevTools\\llvm-mingw，不在项目里，免得被 PyInstaller 打进包）。
产物：dist\\GlassHack.exe，运行时需要同目录下有
  - GlassHackCore.exe   （Python 后端，PyInstaller 打的）
  - WebView2Loader.dll  （从 WebView2 NuGet 包里取）
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 工具链装在 D:\DevTools，目录名带版本号（如 llvm-mingw-20261020-ucrt-x86_64），
# 且解压后可能因为占用而没改成 llvm-mingw —— 所以按通配符找，别写死。
TOOLCHAIN_ROOT = r"D:\DevTools"


def find_compiler():
    for d in glob.glob(os.path.join(TOOLCHAIN_ROOT, "llvm-mingw*")):
        for rel in ("bin/clang++.exe",
                    "bin/x86_64-w64-mingw32-clang++.exe",
                    "bin/g++.exe"):
            c = os.path.join(d, rel.replace("/", os.sep))
            if os.path.isfile(c):
                return c
    return None


def main():
    cc = find_compiler()
    if not cc:
        print("找不到编译器。期望其一：")
        for c in CANDIDATES:
            print("  ", c)
        return 1
    print("编译器:", cc)

    src = os.path.join(HERE, "main.cpp")
    inc = os.path.join(HERE, "include")
    # 输出到 D 盘的独立目录：避开百度网盘同步句柄，也避开 360 对 dist 里 exe 的占用。
    # 360 的实时防护会持续拎着新生成的 exe，导致链接器覆盖失败，
    # 所以目录可用 OPENHACK_OUT 环境变量换（每次构建换一个新目录最省事）。
    out_dir = os.environ.get("OPENHACK_OUT", r"D:\OpenHack-release")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "OpenHack.exe")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    cmd = [
        cc, "-std=c++17", "-O2",
        "-municode",                # 用 wWinMain 入口
        "-mwindows",                # GUI 子系统，不弹控制台
        "-D_UNICODE", "-DUNICODE",
        f"-I{inc}",
        src, "-o", out,
        "-lole32", "-ldwmapi", "-lshlwapi",
        "-luser32", "-lgdi32", "-lcomdlg32", "-luuid",
        "-fno-exceptions",
        # std::thread 依赖 winpthreads，不显式带 -pthread 会链接报 undefined reference
        "-pthread",
        # 静态链 C++ 运行库，免得目标机器还要带一份 DLL
        "-static-libstdc++", "-static-libgcc",
    ]
    print("命令:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print("---- stdout ----")
    print(r.stdout[-4000:] if r.stdout else "(空)")
    print("---- stderr ----")
    print(r.stderr[-8000:] if r.stderr else "(空)")
    print("返回码:", r.returncode)

    if r.returncode == 0:
        dist = os.path.join(os.environ.get("OPENHACK_BUILD", r"D:\OpenHack-build"), "dist")
        dll = os.path.join(HERE, "WebView2Loader.dll")
        core = os.path.join(dist, "OpenHackCore.exe")

        # 分体模式也要能跑：dll 放一份到 release 目录
        if os.path.isfile(dll):
            shutil.copyfile(dll, os.path.join(os.path.dirname(out),
                                              "WebView2Loader.dll"))

        # ---- 打成单文件：把 core + dll 追加到 exe 末尾 ----
        # 用户要求"一个 exe 到哪台电脑都能用"，旁边的文件越少越好。
        if os.path.isfile(core) and os.path.isfile(dll):
            import struct

            def has_footer(p):
                if os.path.getsize(p) < 24:
                    return False
                with open(p, "rb") as f:
                    f.seek(-8, os.SEEK_END)
                    return f.read(8) == b"OHPK0001"

            if has_footer(out):
                print("exe 已内嵌过 payload，跳过（重新编译会自动覆盖）")
            else:
                with open(core, "rb") as f:
                    cb = f.read()
                with open(dll, "rb") as f:
                    db = f.read()
                # buildId 取后端文件的 mtime：重新打包过就一定会变，
                # 自解压端据此决定要不要覆盖 bin 里的旧后端。
                # 只比大小不可靠 —— 两次构建可能刚好一样大。
                build_id = int(os.path.getmtime(core))
                with open(out, "ab") as f:
                    f.write(cb)
                    f.write(db)
                    f.write(struct.pack("<QQQ8s", len(cb), len(db), build_id,
                                        b"OHPK0001"))
                print("已内嵌后端与 WebView2Loader → 单文件 %.1f MB"
                      % (os.path.getsize(out) / 1048576))
        print("产物:", out, os.path.getsize(out), "bytes")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
