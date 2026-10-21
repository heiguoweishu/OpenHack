# OpenHack

**Windows 上的黑苹果（Hackintosh）图形化装机工具。** 扫描本机硬件 → 判定 macOS 兼容性 →
生成 OpenCore EFI → 可选地在现有硬盘上无损划出安装分区并写入恢复镜像。

全程本机运行，不上传任何硬件信息。界面是本地网页（液态玻璃风格），外壳是原生 Win32 窗口。

![概览](docs/screenshots/overview.png)

> **非商业项目。** 以 [PolyForm Noncommercial 1.0.0](LICENSE) 发布：个人、研究、教育、非营利可用，
> **任何商业用途都不行**。原因见 [NOTICE.md](NOTICE.md) 第 4 节（依赖的 NVIDIA 驱动生态是非商用许可）。

> ⚠️ 黑苹果本身违反 Apple 的软件许可协议，且涉及分区操作。请自行评估风险，**动手前务必备份**。

---

## 能做什么

### 1. 判断这台机器到底能不能装

- 扫描 CPU、主板、BIOS、显卡、声卡、网卡、蓝牙、存储控制器、USB 控制器，并导出 **36 份 ACPI 原始表**（DSDT 解析成功才继续）。
- **逐设备**给出结论：原生支持到哪个 macOS 版本 / 需要 OCLP 根补丁 / 完全不支持 —— 不是一个笼统的"能装"。
- 开机前的硬门槛体检：UEFI 启动模式、Secure Boot、BitLocker/设备加密、**Intel VMD / RST**（开着就看不到 NVMe）、Above 4G、CSM。
- 每一项都给出**具体操作**（比如 VMD 要在 BIOS 里关掉哪个控制器、`bcdedit` 怎么改），不是只报个红叉。

### 2. 生成一份能直接用的 OpenCore EFI

- 自动匹配 SMBIOS 机型（如 `MacPro7,1`）、自动挑选 kext（典型 18 个：Lilu、VirtualSMC、AppleALC、WhateverGreen、NVMeFix、RestrictEvents、USBToolBox…）、自动挑选 ACPI 补丁（`SSDT-PLUG` / `EC` / `USBX` / `SBUS` / `RTCAWAC` / `USB-Reset`）。
- 生成完整 `config.plist`：boot-args、SIP、SecureBootModel、Kernel/Block、机型专属设置都落到位。
- **产物可核查**：生成的 EFI 会列出完整的 kext 与 ACPI 清单，每一步判定都有日志。这个工具不做黑箱——你随时能拿产物和 [Dortania 指南](https://dortania.github.io/OpenCore-Install-Guide/) 对照。
- 版本可选，且**选项是按你的硬件算出来的**：原生支持区间内可选，走 N 卡驱动时只给 macOS 15 / 26。

![一键生成 EFI](docs/screenshots/efi.png)

### 3. 在你现有硬盘上无损装（不动数据）

- 压缩现有 NTFS 分区腾出空间，**卷内数据不变**；也可以直接用磁盘尾部已有的空闲空间。
- 新建 EFI 分区（ESP）+ macOS 安装分区，写入 OpenCore EFI。
- 下载并写入 **Apple 官方恢复镜像**（macrecovery，含 chunklist 的 RSA 验签）。
- **不删除、不格式化任何现有分区**；检测到已有的 Apple 分区会明确提醒"请确认不要覆盖"。
- 每一步都有前置校验，空间不够会在动手前拦下来，而不是做到一半报错。

![无损安装](docs/screenshots/install.png)

### 4. 让 NVIDIA 新卡也能用（这是很多工具做不到的）

- 接入 [NullMoth](https://github.com/nullmoth/nvidia-macos-driver) 第三方驱动：Turing 及更新架构（GTX 16 / RTX 20 / 30 / 40 / 50、TITAN RTX、部分 Quadro）可在 **macOS 15 Sequoia / macOS 26 Tahoe** 上获得图形加速。
- 自动写入驱动要求的全部硬性配置：7 条 boot-args、SIP `430A0000`、`SecureBootModel = Disabled`、`Kernel/Block` 屏蔽 `IONDRVSupport`、拉起 AMFIPass。
- 驱动包（约 256 MB）落到 ESP 的 `NullMoth/` 目录，附 README 说明 macOS 装好后怎么装驱动。
- **如实区分两档**：`tested`（作者实测过，目前只有 `2D05`）与 `listed`（仅在 NVIDIA 固件支持表内、未实测）。界面不会把 `listed` 说成"保证能用"。

### 5. 不折腾的工程细节

- **国内网络能下载**：OpenCore、kext、恢复镜像都有镜像兜底，不会卡在 `raw.githubusercontent.com`。
- **不乱弹窗**：无控制台 GUI 程序，所有子进程带 `CREATE_NO_WINDOW`，`os.system('cls')` 这类调用也被接管 —— 扫描时不会满屏黑窗。
- **单文件分发**：17.8 MB 一个 exe，后端与 WebView2 加载器都嵌在尾部，放到任意目录都能跑。
- **可排查**：所有判定与失败原因都写进 `%LOCALAPPDATA%\OpenHack\data\` 下的日志。

## 它**不**做什么

避免误会，这几件事它不做：

- **不改你的 BIOS**。只告诉你该改哪一项，改还是你自己动手。
- **不提供 macOS 系统本体**，只从 Apple 官方 CDN 拉恢复镜像。
- **不支持 Apple Silicon / ARM**，面向 x86_64 台式机与笔记本。
- **不保证每张 N 卡都能用**。`listed` 状态就是"没被实测过"，装不上是有可能的。
- **不做代码签名**。exe 会触发"未知发布者"，个人项目负担不起证书。
- **不替代官方文档**。生成的 EFI 可以用，但你应该知道自己在装什么 —— 出问题时 Dortania 指南才是第一手资料。

## 两条路线

| 路线 | 做什么 | 会不会动你的硬盘 |
|---|---|---|
| **① 生成 EFI** | 扫描硬件 → 兼容性判定 → 选机型 / kext / ACPI 补丁 → 生成 OpenCore EFI | **不会**，只写文件 |
| **② 无损安装** | 压缩现有分区腾出空间 → 建 EFI 分区 + macOS 安装分区 → 写入 EFI 与 Apple 恢复镜像 | **会**，但只压缩已有分区，**不删除、不格式化任何现有分区** |

两条线可以只用 ①（自己写 U 盘），也可以 ①→② 一条龙。

## 系统要求

- Windows 10 19045+ 或 Windows 11，64 位
- 无损安装与 ACPI 导出**需要管理员权限**
- WebView2 Runtime（Win11 自带；Win10 若缺失，程序会自动回退到系统浏览器打开界面）
- 生成 EFI / 下载恢复镜像需要联网

## 使用

从 [Releases](../../releases) 下载 `OpenHack.exe`，双击即可。

它是**单文件**：Python 后端和 WebView2 加载器都嵌在 exe 尾部，首次运行自动解压到
`%LOCALAPPDATA%\OpenHack\bin`，之后按 buildId 校验版本，不会重复解压。

产物位置：

| 内容 | 路径 |
|---|---|
| 硬件报告 + ACPI 原始表 | `%LOCALAPPDATA%\OpenHack\data\reports` |
| 生成的 EFI | `%LOCALAPPDATA%\OpenHack\data\results\EFI` |
| 下载缓存（OpenCore / kext） | `%LOCALAPPDATA%\OpenHack\data\cache` |
| 运行日志 | `%LOCALAPPDATA%\OpenHack\data\launcher.log`、`server.log` |

## 从源码运行

```powershell
python -m venv runtime
runtime\Scripts\python -m pip install -r requirements.txt
runtime\Scripts\python launcher.py
```

浏览器会自动打开 `http://127.0.0.1:8733/`。开发时可直接访问该地址，改前端后刷新即可。

## 构建单文件 exe

需要 **Python 3.13** + **PyInstaller** +（编译原生外壳时）**LLVM-MinGW** 与 **WebView2 SDK**。

```powershell
# 1) 后端（PyInstaller onefile）→ dist\OpenHackCore.exe
python _test/build_exe.py

# 2) 原生外壳（C++，含内嵌后端）→ 单文件
python native/build_native.py
```

`native/build_native.py` 会把 `OpenHackCore.exe` 与 `WebView2Loader.dll` 附加到 `OpenHack.exe` 尾部，
并在文件末尾写 32 字节 footer（`coreLen / dllLen / buildId / "OHPK0001"`）。
运行时按这个 footer 定位并自解压，buildId 不一致才覆盖。

> 仓库目录名沿用早期的 `GlassHack`，`GlassHack.spec` 也是那时候的名字；产品名已全部改为 OpenHack。

## 架构

```
OpenHack.exe            原生外壳：C++ / Win32 无边框窗口 + DWM 毛玻璃 + WebView2
  └─ 内嵌 OpenHackCore.exe   Python 后端：HTTP + SSE，派发各 worker 子进程
       ├─ app/worker_scan.py    硬件采集（Hardware-Sniffer）
       ├─ app/worker_efi.py     兼容性分析 / EFI 生成（OpCore-Simplify）
       └─ app/worker_install.py 分区与镜像写入（diskpart / macrecovery）
  └─ web/                前端：原生 HTML/CSS/JS，无框架、无外部依赖
```

设计上的几个取舍：

- **worker 一律跑在独立子进程**：WMI/COM 在 GUI 主进程的工作线程里容易挂死，隔离后稳定得多。
- **不改上游源码**：对 OpCore-Simplify 的裁剪全部用运行时 monkey patch（`app/autopilot.py`），
  升级上游时差异一目了然。唯一例外见 [vendor/NOTICE.md](vendor/NOTICE.md)。
- **进度条是一条连续线**：扫描占 0–30%，分析接着 32%→100%，不会出现"检测完了却卡在 9x%"。
- **前端不用框架**：整个 UI 约 100 KB，省掉构建链，也让 exe 保持小体积。

## 已知限制

- **WebView2 起不来时自动回退浏览器**：少数机器（含部分虚拟机 / 无 GPU 会话）WebView2 渲染进程
  会崩，程序会记住该状态并用系统浏览器打开界面 —— 功能相同，只是没有原生毛玻璃外壳。
- **N 卡只支持 macOS 15 / 26**：NullMoth 驱动只对这两个版本构建了 NVAccel，版本下拉里不会出现
  macOS 14 及更早。硬传其它版本会得到明确警告。
- **N 卡支持分两档**：`tested`（作者实测过）与 `listed`（仅在 NVIDIA 固件支持表内）。
- **Apple Silicon / 非 x86 不支持**：这个工具面向 x86_64 台式 / 笔记本。

## 常见问题

**扫描时进度条不动 / 中文乱码？**
已修。根因分别是缺少 `wmi` 依赖与 worker 的 stdout 编码退化为 cp936。若仍出现，看
`%LOCALAPPDATA%\OpenHack\data\launcher.log`。

**生成的 EFI 里 ACPI 是空的？**
已修。早期版本把界面上的"自动"预设误传成"全部关闭"。现在默认由引擎按 CPU / 主板挑选。

**扫描时闪一下黑窗？**
已修。上游会执行 `os.system('cls')` 清屏，它不走 `subprocess`，需要单独接管。

**Windows 提示"未知发布者"？**
exe 未做代码签名。校验方式：从本仓库 Releases 下载，比对 `SHA256SUMS.txt`。

## 许可证

[PolyForm Noncommercial 1.0.0](LICENSE)。第三方组件归属与"哪些随仓库分发、哪些运行时下载"见
[NOTICE.md](NOTICE.md)，`vendor/` 的修改说明见 [vendor/NOTICE.md](vendor/NOTICE.md)。

## 致谢

这个项目站在一堆开源工作的肩膀上：OpCore-Simplify、Hardware-Sniffer、OpenCorePkg / Acidanthera、
OpenCore Legacy Patcher、Dortania 的安装指南、NullMoth 的 NVIDIA 驱动，以及 SSDTTime、USBToolBox 等。
完整清单与许可证在 [NOTICE.md](NOTICE.md)。
