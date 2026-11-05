# Desktop Exporter CI 与发布

工作流：仓库根目录 `.github/workflows/exporter.yml`。桌面客户端有独立 Go module 和版本，不改动根目录 Python SDK 的构建和 `v*` 发布流程。

## 触发与检查

- main 的相关文件 push，以及涉及客户端的 pull request：执行三系统原生测试和完整安装包构建。开发分支通过 PR 验证，避免同一提交在 push 和 PR 两处重复构建。
- `workflow_dispatch`：合入默认分支后可在 Actions 页面选择目标分支手动运行。
- `exporter-v<版本>` 标签：必须与 `desktop/exporter/mygo.json` 版本完全一致，三平台通过后创建 Release 草稿。
- 普通检查只有 `contents: read`，不读取签名密钥；checkout 不保留推送凭据。仅标签触发的草稿任务有 `contents: write`。
- 任务有超时，同分支新提交取消旧构建，标签构建不取消；矩阵某个平台失败不阻止其他平台返回诊断。

macOS 15 arm64、Windows Server 2025 amd64、Ubuntu 22.04 amd64 分别运行 Go 测试、vet、Python 打包和下载校验测试；Mac/Linux 额外运行 race，Mac 额外测试安装权限脚本。媒体工具加入测试进程 PATH，不更改系统安装。

Linux `mke2fs` 来自 e2fsprogs，嵌套 ext4/大于 4GiB 稀疏文件测试应真实执行。硬件编码器在无 GPU 的 hosted runner 中不可用时使用软件编码；这不是硬件加速验收。

## 完整包

`prepare_media.py` 对 Windows/Linux 使用 `media-lock.json` 中的固定上游 URL 和 SHA256。下载只提取两个工具和许可证，不展开任意路径、不执行下载脚本。更新依赖时先检查上游版本及许可，再修改 URL/digest，重新跑三平台测试。macOS 使用 Homebrew bottle，将整个非系统动态库闭包复制、重定位并签名，保留版本及源配方；Homebrew 版本随 runner 更新，确切版本记录在包内 `Homebrew-SOURCES.json`，不是可复现的逐字节构建。

打包脚本从隔离源码副本构建，要求版本对应的安装器存在且非空。Mac 使用 PKG 安装应用和只读 helper（CI 不生成 DMG）。Windows 使用 NSIS，Ubuntu 生成 deb 和用户安装 tar.gz。

`verify_runtime.py` 直接调用包内 ffmpeg/ffprobe，编码合成一秒视频，完整数帧核对 24 帧及 AAC 音轨；Mac 额外检查 App 签名。只有该步骤成功才上传安装包、SHA256SUMS 和 BUILD-INFO.json。Actions artifacts 保留 30 天；长期分发使用维护者确认后的 Release。

## 发布

1. 更新 mygo.json 与 CHANGELOG.md，提交并确认 CI 成功。
2. 将对应提交打为 `exporter-v0.1.5` 这样的标签并推送。
3. 工作流从该标签重新测试、打包、校验三平台产物，生成草稿。任何一个平台失败都不会创建草稿。
4. 维护者在独立机器检查安装、实际读卡、权限交互和导出，再手动发布草稿。

草稿明确 `latest=false`，避免打断 Python SDK 的 `releases/latest/download/install.sh` 安装入口。普通 push 不创建标签或 Release。

当前不配置 Developer ID、公证、Windows 代码签名或生产更新密钥。包内显示未配置自动更新，不生成看似可用的更新 feed。未来配置生产签名必须通过独立 secrets/environment 配置完成，不提交私钥，不在 PR 运行中暴露密钥。
