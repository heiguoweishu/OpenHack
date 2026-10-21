# vendor/ 第三方源码说明

`vendor/` 下是**上游项目的源码副本**，放在这里是为了离线运行 —— 本工具不发 npm/pip 包，
需要把两个引擎的代码一起打包进单个 exe。它们**不是本项目的代码**，各自保留原许可证。

| 目录 | 上游项目 | 许可证 | 在本项目中的职责 |
|---|---|---|---|
| `ocsimplify/` | [lzhoang2801/OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) | BSD-3-Clause（见 `ocsimplify/LICENSE`） | 硬件兼容性判定、SMBIOS 机型匹配、kext 与 ACPI 补丁选择、`config.plist` 生成 |
| `hsniff/` | [lzhoang2801/Hardware-Sniffer](https://github.com/lzhoang2801/Hardware-Sniffer) | BSD-3-Clause（见 `hsniff/LICENSE`） | 硬件报告采集、ACPI 原始表导出 |

BSD-3-Clause 允许修改与再分发，但要求保留版权声明与许可证文本，且不得用原作者名义为衍生品背书。
本项目**不以原作者名义发布**，也不代表他们认可本项目。

---

## 我们对上游源码做了哪些修改

为了让上游在「无控制台的 GUI 进程 + 中文 Windows + 带杀毒软件」的环境下跑通，
**只改了一个文件**：

### `vendor/hsniff/Scripts/utils.py`

| 位置 | 改动 | 原因 |
|---|---|---|
| `Utils.write_file()` | 写入失败时重试（最多 6 次，间隔递增） | 360 等实时防护会瞬时独占刚生成的 `.json` / `.aml`，导致 `PermissionError: [WinError 5]`，采集明明成功却报"导出报告失败" |
| `Utils.create_folder(remove_content=True)` | `shutil.rmtree` 改为 `ignore_errors=True` 并逐项容错 | 同样是被占用的文件让整个清理步骤抛异常，进而中断后续流程 |

其余文件（包括 `vendor/ocsimplify/` 全部源码）**保持上游原样**。

## 其它适配都在本项目代码里，没碰上游源码

`app/autopilot.py` 用运行时 monkey patch 完成这些事，好处是升级上游时能看出差异：

- 把上游那些"问用户一轮"的交互换成确定性默认值（`install_answers`）；
- 包装 `check_gpu_compatibility`，在它跑完后把 NullMoth 支持的 N 卡重新标为可用（`patch_nvidia`）；
- 给下载加镜像兜底与更长的超时（`app/mirror.py`）；
- 删除操作在受限环境下降级（`patch_removal`）。

## 上游自身的第三方依赖

`vendor/ocsimplify/acpi_guru.py` 等文件源自 [corpnewt/SSDTTime](https://github.com/corpnewt/SSDTTime)（MIT），
其中的 SSDT 模板来自 [acidanthera/OpenCorePkg](https://github.com/acidanthera/OpenCorePkg)（BSD-3-Clause）
与 [5T33Z0/OC-Little-Translated](https://github.com/5T33Z0/OC-Little-Translated) 等社区资料。
这些归属在上游源码的文件头注释里逐条保留，未做删改。
