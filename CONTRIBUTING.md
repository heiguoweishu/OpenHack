# 参与贡献

先说清楚一件事，能省掉双方的时间：**这个项目是非商用许可（PolyForm Noncommercial 1.0.0）**。
提交 PR 即表示你同意自己的贡献以同一许可证发布。如果你的代码需要商用、或来自不兼容的许可证，
请先开 issue 讨论 —— 我不会合入会让整个仓库无法合法分发的代码。

## 可以提什么

- **Bug 报告**最有价值。这个工具面对的是"每台机器都不一样"的硬件，任何一次失败都是信息。
- **兼容性数据**：某块主板 / 网卡 / 声卡在某个 macOS 版本上的实际结果。
- **界面与文案改进**，尤其是让人看不懂的提示。
- 上游引擎的新版本适配（`vendor/` 升级后 `app/autopilot.py` 的 monkey patch 可能需要跟着调）。

## 报告 bug 时请附上

1. **buildId**：`OpenHack.exe` 文件末尾 32 字节 footer 里的那个数字，或者启动日志里
   `payload: 已更新自带后端 (build ...)` 那一行。
2. **日志**：`%LOCALAPPDATA%\OpenHack\data\launcher.log` 与 `server.log`。
3. **硬件报告**：`%LOCALAPPDATA%\OpenHack\data\reports\Report.json`
   （**提交前请自己检查**：里面含主板型号、序列号、网卡 SSID 等，介意的话删掉相关字段）。
4. 屏幕上的具体表现：报错文字、卡在哪一步、进度条停在几 %。

没有这些基本没法定位 —— 同一个现象（比如"分析失败"）背后可能是显卡不支持、缺依赖、杀软占文件。

## 开发环境

```powershell
git clone <repo>
cd GlassHack          # 目录名沿用旧名，产品名是 OpenHack
python -m venv runtime
runtime\Scripts\python -m pip install -r requirements.txt
runtime\Scripts\python launcher.py
```

改前端（`web/`）直接刷新浏览器即可，不用重启后端。改后端 worker（`app/worker_*.py`）需要重启。

## 代码地图

| 路径 | 说明 |
|---|---|
| `launcher.py` | 入口：分发 worker 模式、起服务、自检、无窗口补丁 |
| `app/server.py` | HTTP + SSE 服务，把 worker 的 stdout 转成事件流 |
| `app/bus.py` | 事件协议（`log` / `step` / `progress` / `result` / `done`）与进度分摊 |
| `app/autopilot.py` | 对上游引擎的运行时 monkey patch（交互改默认值、N 卡认领、删除降级） |
| `app/nullmoth.py` | NullMoth N 卡驱动的接入（支持表、boot-args、SIP、驱动包落盘） |
| `app/install.py` | 无损安装：压缩 → 建分区 → 写 EFI → 写恢复镜像 |
| `app/worker_*.py` | 三个 worker，各自跑在独立子进程 |
| `native/main.cpp` | Win32 窗口 + DWM 毛玻璃 + WebView2 宿主 + 单文件自解压 |
| `web/` | 前端，原生 HTML/CSS/JS，无框架无外部依赖 |
| `_test/` | 开发期的诊断脚本（验证构建、抓窗口、对比 EFI），不参与发布 |

## 几个约定

- **不要直接改 `vendor/` 里的上游代码**。需要适配就用 `app/autopilot.py` 里的 monkey patch ——
  这样上游升级时 diff 一目了然。确有必要改上游时，必须同步更新 `vendor/NOTICE.md`。
- **进度条是一条连续线**：扫描占 0–30%，分析 32→100。新增步骤时别让进度倒退。
- **不要引入前端构建链**。整个 UI 就几十 KB，加了打包器反而让 exe 变大、贡献门槛变高。
- **Windows 上所有子进程都要带 `CREATE_NO_WINDOW`**。这是无控制台 GUI 程序，漏一个就闪黑窗。
  `os.system` / `os.popen` 也要走 `launcher._install_no_window()` 的补丁，它已经处理好了。
- **不要提交** `data/`、`runtime/`、`dist/`、`_shot/`（已在 `.gitignore` 里）。
  尤其不要提交别人的硬件报告。

## 提交信息

```
模块: 做了什么

为什么这么做（尤其是"看起来可以更简单"的地方）
```

例：`worker_efi: 分析结束时补 100% 进度` / `为什么：之前只推到 95，界面像卡住`。
