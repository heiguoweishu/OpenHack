"""无损安装 worker（独立子进程）。

把危险操作单独隔离到一个进程里：主服务即使被用户中途关掉 UI，
这一步也已经在该走的路径上 —— 反过来也一样，它自己炸了不会拖垮整个工具。
"""
import argparse
import json
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import bus as busmod  # noqa: E402
from app import install as install_api  # noqa: E402
from app import paths  # noqa: E402


def run(argv, out):
    """见 worker_efi.run 的说明：frozen 模式下不能起子进程，必须进程内执行。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="{}")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    bus = busmod.EventStream(out)
    saved = busmod.install_tee(bus)
    exit_code = 0
    try:
        cfg = json.loads(args.json or "{}")
        _guard_administrator(bus)
        if args.dry_run:
            _dry_run(cfg, bus)
        else:
            install_api.run_install(cfg, bus)
    except Exception as e:
        bus.log(f"{type(e).__name__}: {e}", "error")
        if not args.dry_run:
            bus.log(" | ".join(traceback.format_exc().strip().splitlines()[-4:])[:600], "error")
            bus.log("分区过程中止。已经完成的部分改动会保留（它们本身是安全的），"
                    "请在 Windows 磁盘管理里确认当前状态后再决定是否重试。", "warn")
        else:
            bus.log("预检未通过，未对磁盘做任何修改。", "warn")
        exit_code = 1
    finally:
        busmod.restore_tee(saved)
    bus.done(exit_code)
    return exit_code


def _guard_administrator(bus):
    """分区操作必须有管理员。早点说清楚，别让用户等到压缩那一步才失败。"""
    from app import ps
    if ps.is_admin():
        bus.log("已获得管理员权限", "ok")
        return
    bus.log("当前不是管理员身份，无法创建或修改分区。", "error")
    bus.log("请关闭本工具，右键选择「以管理员身份运行」，再执行这一步。", "error")
    raise PermissionError("需要管理员权限")


def _dry_run(cfg, bus):
    """只做检查不落盘。UI 在用户点确认之前可以先用它验证方案是否成立。"""
    from app import disks as disks_api
    bus.step("预检", 0, 1)
    n = cfg.get("diskNumber")
    target = None
    for d in disks_api.enum_disks():
        if d["number"] == n:
            target = d
            break
    if not target:
        raise RuntimeError(f"找不到磁盘 {n}")
    report = disks_api.analyse(target)
    bus.log(f"磁盘 {report['name']}（{report['sizeHuman']}）", "info")
    for issue in report["issues"]:
        bus.log(f"阻塞项：{issue['title']} — {issue['detail']}", "error")
    for w in report["warnings"]:
        bus.log(f"提示：{w['title']} — {w['detail']}", "warn")
    if report["issues"]:
        raise RuntimeError("预检未通过，请先看阻塞项")
    esp = int(cfg.get("espSizeMb", 1536))
    need_mb = esp + int(cfg.get("installSizeMb") or 0)
    shrink = cfg.get("shrink")
    have_mb = 0
    if shrink:
        have_mb = int(shrink.get("mb", 0))
    else:
        have_mb = int(report["tailFree"] / 1024 / 1024)
    bus.log(f"需要 {need_mb} MB，可提供约 {have_mb} MB", "info")
    if need_mb > have_mb:
        raise RuntimeError(f"空间不足：需要 {need_mb} MB，只有 {have_mb} MB")
    bus.log("预检通过，可以执行", "ok")
    bus.result({"ok": True, "need": need_mb, "have": have_mb})


def main():
    return run(sys.argv[1:], sys.__stdout__)


if __name__ == "__main__":
    sys.exit(main())
