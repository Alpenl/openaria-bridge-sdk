# Open Aria 录像导出

Go + MyGo 原生中文桌面客户端，从 OpenAria 内存卡读取录像并导出左右拼接 MP4。支持 Apple Silicon macOS、Windows amd64 和 Ubuntu amd64；与仓库根目录的 Python Bridge / SDK 独立构建、独立版本、独立保存导出历史。

## 获取安装包

在仓库 **Actions → Desktop Exporter** 中选择成功的运行，下载对应系统的 `exporter-<平台>-<commit>` artifact，解压后按 `SHA256SUMS` 校验。推送 `exporter-v<版本>` 标签时，所有系统测试和打包成功后自动创建 **Release 草稿**，由维护者核对后发布；不会替换 Python SDK 的 latest release。

| 平台 | 完整安装包 | 设备权限 |
| --- | --- | --- |
| macOS Apple Silicon | `open-aria-exporter-<版本>-arm64.pkg` | 安装独立只读 helper，按应用提示授权完整磁盘访问 |
| Windows 11 amd64 | `open-aria-exporter-setup-<版本>-amd64.exe` | 安装、启动使用系统 UAC |
| Ubuntu 22.04+ amd64 | `.deb` 或 `.tar.gz` + `install.sh` | GTK 3、util-linux、pkexec、桌面 polkit 授权代理 |

完整包附带 FFmpeg、ffprobe 和许可/来源说明，无需另装媒体工具。macOS 仅 ad-hoc 签名，尚未配置 Developer ID / 公证；Windows 尚未配置代码签名证书，系统可能显示未知开发者提示。请核对来源与校验和，不关闭系统安全机制。macOS 媒体运行时基于构建机 Homebrew bottle，其系统版本要求可能高于 App 的基础最低版本；当前 CI 在 macOS 15 构建和验证。

Ubuntu 用户级安装（在解压出 `install.sh` 的目录运行）：

```sh
sh install.sh open-aria-exporter-<版本>-linux-amd64.tar.gz
```

用户级安装进入 `~/.local/open-aria-exporter.app`，注册应用菜单和 `~/.local/bin/open-aria-exporter`。系统安装可使用 `sudo apt install ./open-aria-exporter_<版本>_amd64.deb`。

## 使用

1. 插入内存卡。程序自动检测外置 OpenAria 录制卷，选择第一张有效卡。Ubuntu 如提示权限不足，点击“重新检测内存卡”触发授权。
2. 选择保存位置和录像，点击“开始导出”。输出自动放到选定目录下，例如 `OpenAriaExporter/2026-11-09 17时30分28秒/`。按录制开始时间命名，不使用导出当天时间；同秒不同录像加 `（2）` 等编号，时间无效或校时证据冲突用 `录制时间未知（1）`。
3. 成功后得到 `video.mp4`、`export.json` 和 `source/` 完整录制数据，列表显示绿色“本机已导出”。可取消进行中的任务，已完成条目保留。

`source/` 保留原录制目录结构：IMU 的实际路径见 `export.json` 的 `imu_file`（设备常见 `source/imu/imu.jsonl` 或压缩版本），独立音频路径见 `audio_files`，全部文件见 `source_files`。包括原始左右眼视频、音频分段、IMU、帧时间戳、标定等录制目录内文件及 `manifest.json`。IMU JSONL/Zstd 原样保存，不转换单位/坐标、不插值；无 IMU 或音频时相应路径为空，不制造不存在的数据。同步分析请使用 `source/` 原始数据及清单，观看用 MP4 不是 SDK 同步认证结果。

录制时间以 manifest 为准，不被 SQLite 显示摘要覆盖。保留原始 UTC 偏移，不随导出电脑时区改变目录；毫秒、时区、校时来源和推算说明见 `export.json.recording_time`，实际导出时间另记 `exported_at`。Pi 0.2.12/0.2.13 的 `clock-calibration.json` 会核对小文件摘要、录像身份、起止时间及来源；冲突时使用“录制时间未知”。旧录像无校时证明则说明来源未知；有效结束时间与有限时长支持的旧时钟跳变可推算，界面标注“推算”。合成 MP4 写入 UTC `creation_time`，文件系统创建时间仍是导出时间，不用于推断拍摄日期。

同一保存目录重复导出完整录像时，核对身份、清单和文件大小后复用已有结果，不重写视频；这不是全媒体摘要复验。已有结果缺失文件或结构异常则报错保留，用户可选新目录重导。旧 UUID 目录不自动重命名：完整 0.1.9 成品可识别复用，0.1.8 及以前仅视频成品不算完整导出。若需要新的时间目录，请选择新的保存位置。新命名锁集中到隐藏 `.openaria/`，旧锁保留以兼容旧客户端，勿在导出/删除任务中手动移除锁目录。

GUI 不接受任意本机文件夹、镜像或内置磁盘作为源；CLI 也会校验外置设备成员关系和录制卷协议。扫描使用生产 SQLite 元数据，支持外层 ext4 中嵌套的数据卷镜像。

本应用以只读方式访问卡，不修复文件系统、不回放 journal、不修改挂载状态。操作系统可能已经以读写方式挂载卡，这不属于本应用的只读句柄保证。出现待恢复日志时会提示最近数据可能不可见。

## 导出与本机历史

- 正常导出从卡上流式生成 H.264 SBS 视频和 AAC 音频，再逐文件保存完整录制原件到 `source/`；不复制整个嵌套镜像。需要原始录制加合成视频的空间，耗时会比只导出 MP4 更长。
- 视频处理失败时，自动将录制目录全部原始文件（包括 IMU、独立音频、视频及辅助文件）保存到 `2026-11-09 17时30分28秒（未合成）/source/`（示例），附带 manifest、复制说明和 `UNPROCESSED.txt`，并弹窗明确提示“未拼接、未转码”和保存位置。兜底复制只核对字节数，不做全媒体 SHA256；不会标记为合并导出成功。批量任务继续处理后续条目，结束后汇总提示。兜底的 `（未合成）` 文件夹不纳入“删除本机成品”，需在保存目录手动管理；正常成品中的 `source/` 会随本机成品一起删除，确认弹窗明确说明。
- 取消、源读取失败、目标冲突等不触发兜底；空间不足、断卡或复制失败会明确报错，不能保证这些情况下仍可保存原始文件。现有文件不会覆盖。
- 自动试用可用硬件编码器：macOS VideoToolbox，Windows NVENC/QSV/AMF，Linux NVENC/QSV/VAAPI；不可用时回退软件编码。界面无需配置硬件选项。
- 正常导出不做媒体 SHA256 或全片数帧复查；执行媒体头、文件大小、尺寸、音轨、时长及编码错误检查。遇到重复 DTS 时才额外流式解码检查时间轴，受限修复重复/轻微倒退 PTS，再使用无 B 帧软件编码并核对输出包时间戳。修复不强制固定帧率，记录在 `export.json`；不安全的时钟重置、尺寸变化等转为原始兜底。该异常恢复会比正常导出慢。小型 manifest 摘要用于身份和暂存所有权，不能保证发现所有静默媒体损坏。
- 最多两路编码，共享有上限的缓存；同一目标持有稳定父目录锁，不覆盖既有成品。
- 本机历史按录制卷、录像和全部 artifact 描述版本保存。重启、拔插、盘符变化不丢标记；同一张卡换到另一台电脑不会同步标记。0.1.9 使用完整数据导出标记，旧版仅 MP4 的标记不会冒充完整导出；旧成品和删除记录仍保留，重新导出请选新目录以避免覆盖冲突。
- **删除本机成品**：确认具体路径后删除本客户端记录的成品，保留卡内录像及导出标记。含额外文件、符号链接或已变化的成品拒绝删除。
- **清除标记**：仅清本机标记，保留成品及卡内录像。旧版未记录的成品不自动接管。

**卡内原始录像删除尚未实现。** 根目录 Python SDK 的源删除功能不代表本桌面客户端也支持。当前也没有生产自动更新 feed；手动检查会说明未配置。

设置和历史保存在各系统用户目录的 `OpenAriaExporter` 下：macOS Application Support、Windows LocalAppData、Linux XDG config。测试通过独立临时目录隔离，不应把测试数据库作为用户历史分发。

## 开发与构建

使用 `go.mod` 指定的 Go 版本和固定 MyGo 模块；Python 构建脚本需要 3.11+，CI 使用 3.13。进入本目录执行：

```sh
go test -count=1 ./...
go vet ./...
python3 -m unittest discover -s scripts -p 'test_package.py'
```

macOS 另运行 `test_permissions.py`；Linux 的嵌套 ext4 测试需要 `mke2fs`（e2fsprogs）。媒体测试需要 FFmpeg/ffprobe 6+，建议使用随包工具准备脚本，并把其目录加入 PATH 及 `OPENARIA_FFMPEG_DIR`。

```sh
# Windows 媒体工具可在任一构建机下载；Linux 换为 linux-amd64。
python3 scripts/prepare_media.py --platform windows-amd64 --out build/media-windows
python3 scripts/package.py --platform windows/amd64 \
  --windows-media-dir build/media-windows

# macOS 原生构建；先 brew install ffmpeg e2fsprogs。
python3 scripts/prepare_media.py --platform darwin-arm64 --out build/media-macos
python3 scripts/package.py --platform darwin/arm64 \
  --macos-media-dir build/media-macos --skip-dmg

# Linux 原生构建；需要 GTK 3 开发库、pkg-config、e2fsprogs。
python3 scripts/prepare_media.py --platform linux-amd64 --out build/media-linux
python3 scripts/package.py --platform linux/amd64 \
  --linux-media-dir build/media-linux
```

Windows 安装器需要 NSIS（`makensis` 在 PATH）。`prepare_media.py` 要求输出目录尚不存在；Windows/Linux 下载版本和 SHA256 在 `scripts/media-lock.json` 固定。上游资产变化或失效会使构建失败，不自动接受新字节。macOS 复制 Homebrew 的完整非系统 dylib 依赖闭包，重定位为相对 loader 路径，附带可取得的许可证、构建配方及版本/源地址清单；不会修改 Homebrew 中的原文件。

生成后执行 `python3 scripts/verify_runtime.py --platform <darwin-arm64|windows-amd64|linux-amd64>`。它使用包内媒体工具合成一秒短片，核对 24 帧和 AAC 音轨，验证 Mac 签名，再将安装包、校验和与构建信息放入 `build/deliverables`。验证失败不上传 artifact。发布流程见 [CI.md](CI.md)。

## 验证边界

桌面客户端此前已在 macOS、Windows 11 和 Ubuntu 22.04 实机验证扫描、导出和本机管理。Ubuntu 真实卡短片验证过 3840×1080、132 帧、4.4 秒及 AAC，普通用户与只读 helper 的描述符传递已测；桌面 polkit 人工密码对话框仍需交互验收。CI 使用合成素材，不替代真实读卡器、UAC/FDA/polkit、人为拔插或大容量录像验收。新媒体分发版本的驱动兼容性可能不同，硬件不可用时回退软件编码。
