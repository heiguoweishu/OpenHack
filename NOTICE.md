# NOTICE · 第三方组件与归属

OpenHack 自己的代码以 [PolyForm Noncommercial 1.0.0](LICENSE) 发布：**个人、研究、教育、
非营利可用，任何商业用途都不行**。这个选择不是随手定的，主要受下面第 4 条约束。

本文件列出项目用到的每一份第三方作品、其许可证，以及它是**随仓库分发**还是**运行时就地下载**。
两者的法律含义完全不同，请区分对待。

---

## 1. 随仓库分发的源码（`vendor/`）

| 组件 | 许可证 | 说明 |
|---|---|---|
| [OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) | BSD-3-Clause | 源码副本在 `vendor/ocsimplify/`，未修改（见 `vendor/NOTICE.md`） |
| [Hardware-Sniffer](https://github.com/lzhoang2801/Hardware-Sniffer) | BSD-3-Clause | 源码副本在 `vendor/hsniff/`，修改了 1 个文件（见 `vendor/NOTICE.md`） |
| [SSDTTime](https://github.com/corpnewt/SSDTTime) | MIT | 作为 OpCore-Simplify 的一部分被引入（`acpi_guru.py` / `Scripts/run.py` 等），文件头保留原始出处 |

BSD-3-Clause 与 MIT 都允许再分发与修改，要求是**保留版权声明和许可证文本**，且不得用原作者
名义为衍生品背书。本项目由独立开发者维护，与上述作者无关，也未获其背书。

## 2. 运行时下载、不随仓库分发

这些组件由程序在你本机联网获取，**本仓库不包含它们的任何副本**：

| 组件 | 许可证 | 用途 |
|---|---|---|
| [OpenCorePkg](https://github.com/acidanthera/OpenCorePkg) 及 Acidanthera 系列 kext（Lilu、VirtualSMC、AppleALC、WhateverGreen、NVMeFix、RestrictEvents 等） | BSD-3-Clause | 引导加载器与内核扩展，生成 EFI 时下载 |
| [OpenCore Legacy Patcher](https://github.com/dortania/OpenCore-Legacy-Patcher) 提供的 kext（AMFIPass、IOSkywalkFamily、IO80211FamilyLegacy、AirPortBrcmNIC-Tahoe 等） | BSD-3-Clause | 旧硬件 / 新系统的补丁支持 |
| [macrecovery](https://github.com/acidanthera/OpenCorePkg)（vit9696） | BSD-3-Clause | 从 Apple 官方服务器获取恢复镜像，含 chunklist 的 RSA 验签 |
| Apple 恢复镜像 | Apple 的软件许可协议 | 从 Apple 官方 CDN 下载，本工具只做传输，不再分发 |
| [USBToolBox](https://github.com/USBToolBox/tool) | MIT | USB 端口映射 |

## 3. 随仓库分发的二进制 / 头文件

| 组件 | 许可证 | 说明 |
|---|---|---|
| `native/WebView2Loader.dll` | Microsoft WebView2 SDK 许可 | 微软允许随应用再分发（redistributable），版本见 NuGet 包 `Microsoft.Web.WebView2` |
| `native/include/WebView2.h`、`WebView2EnvironmentOptions.h` | 同上 | 编译期头文件，来自同一 NuGet 包 |
| `vendor/hsniff/acpidump.exe` | 随 Hardware-Sniffer 上游分发 | 用于导出 ACPI 原始表 |

`native/` 下的 C++ 代码是本项目自己写的（Win32 窗口 + DWM 材质 + WebView2 宿主），
不属于微软的示例代码。

## 4. NullMoth NVIDIA 驱动 —— 本项目采用非商用许可的直接原因

| 项目 | 许可证 | 与本项目的关系 |
|---|---|---|
| [nullmoth/nvidia-macos-driver](https://github.com/nullmoth/nvidia-macos-driver) | **PolyForm Noncommercial 1.0.0** | 运行时下载（约 256 MB 的 `nullmoth-nvidia-*.tar.gz`），**不随本仓库分发** |
| [nullmoth/1401](https://github.com/nullmoth/1401) | **PolyForm Noncommercial 1.0.0** | 其 Windows 工具的移植思路、以及 `data/nvidia_gsp_ids.json`（NVIDIA GPU 支持表）**随本仓库分发** |

**关键点**：`data/nvidia_gsp_ids.json` 是从 1401 项目复制的数据文件（235 个 GPU 设备 ID 与型号名），
它受 PolyForm Noncommercial 约束。因此本仓库整体选择同一许可证，避免出现
"代码 MIT、数据非商用"这种自相矛盾的组合。若你要把本项目用于商业用途，
**必须先把这份数据文件替换为可商用来源**（例如运行时由用户自行获取），否则不满足许可条件。

该驱动只支持 NVIDIA Turing 及更新架构，且作者仅在 RTX 5060 上做过实测；
`data/nvidia_gsp_ids.json` 里的 235 个 ID 中只有 `2D05` 被作者实测过，其余属于
"在 NVIDIA 固件支持表内、未经该驱动验证"。界面会照实区分 `tested` 与 `listed`。

## 5. Python 依赖

打包后这些库被冻结进 exe：`wmi`（MIT）、`pywin32`（PSF License）、`certifi`（MPL-2.0）。
许可证文本以各自发行包为准。

## 6. 视觉与规范参考

| 来源 | 说明 |
|---|---|
| [Dortania OpenCore Install Guide](https://dortania.github.io/OpenCore-Install-Guide/) | 补丁选择规则、机型支持矩阵、安装流程 —— 作为**技术资料**参考，未复制其正文 |
| Apple Human Interface Guidelines | 界面的"液态玻璃"观感为 CSS 自行实现，未使用 Apple 任何素材 |
| [ExplorerBlurMica](https://github.com/Maplespe/ExplorerBlurMica) | 原生毛玻璃（DWM backdrop / Acrylic）的实现思路参考 |

---

## 隐私说明

工具**只在本机运行**，不向任何服务器上传硬件信息。需要注意的一点：
`vendor/ocsimplify/Scripts/wifi_profile_extractor.py`（上游代码）会读取本机已保存的 Wi-Fi 配置
（含 SSID 与密码），目的是自动填充 AirportItlwm 的配置。这些信息**只写入你本机生成的 EFI**，
不会外发。如果你不接受这个行为，生成 EFI 时不要勾选 Wi-Fi 相关的自动配置。

联网只发生在两处：下载 OpenCore / kext / 恢复镜像，以及（可选）下载 NullMoth 驱动包。

---

如果你认为本文件遗漏了你的作品，请开 issue 说明，我会补上归属或移除相关内容。
