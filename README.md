# SmartStitch

SmartStitch 是一个本地运行的 Python + FFmpeg 视频随机拼接工具。正式 macOS 应用使用原生窗口承载管理界面，源码开发模式使用浏览器。它支持：

- 按配置组合前贴、引子、可增删排序的多段利益点、结尾和尾帧。
- 为每个素材设置权重，并按整批配额分配后随机洗牌。
- 每个配置固定一张透明风险提示语图片，不参与随机抽取，并支持叠加时段、位置和透明度。
- 可选视觉去重：由总开关控制整体是否生效；同帧高斯模糊背景/缩小清晰前景与透明 QuickTime RLE、PNG/WebP 边框是两个独立子功能，可单独或同时选择；风险提示语始终位于最高层。
- 多套 YAML 配置、中文路径、外接硬盘和 macOS `._` 文件过滤。
- 网页可视化调整素材目录、模块开关、叠图、输出和批处理配置，并保留 YAML 专家模式。
- 时间线审核台会用画面转场与语音停顿粗略建议断点，在视频轨道下显示对齐的音频波形；支持人工增删、拖动和逐帧微调，并将审核结果保存为可追溯 JSON。
- 新建受管视频库时询问保存位置，自动建立原始视频、切片素材、利益点、成片和工作记录等标准目录。
- 审核后可为每个片段选择素材池并批量切割入库；新建利益点时会同步创建对应文件夹。
- 支持 CRF 或 VBR 视频码率控制；当前星广配置使用 VBR 3000 kbps。
- 可视化开启响度均衡，以滑块或数值设置目标 LUFS，并可对同一素材试听原音与均衡后效果。
- 组合预览、随机种子复现、批量并发、取消、失败重试和 JSON/CSV 清单。

详细设计见 [doc/README.md](doc/README.md)。

## 安装

需要 Python 3.11+ 和 FFmpeg/ffprobe。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## 启动

```bash
source .venv/bin/activate
smartstitch
```

然后浏览器访问：<http://127.0.0.1:8766>

也可以双击 macOS 下的 `start.command`，它会自动创建虚拟环境和安装依赖。

正式发布的 `SmartStitch.app` 自带 Python、Python 依赖、FFmpeg 和 ffprobe，普通用户不需要安装上述开发工具。应用会在随机的本机环回端口启动服务，并在自己的 macOS 原生窗口中显示 Web UI，不再打开默认浏览器。关闭主窗口或退出应用时，本地服务会同时停止；如果有成片、切片或飞书同步任务，退出前会提示确认。发布版的本机运行数据保存在：

```text
~/Library/Application Support/SmartStitch/
├── config/   # NAS 未挂载时使用；首次启动从应用内置模板初始化
├── data/     # SQLite、任务、时间线、用户和集成设置
└── logs/     # 应用启动日志
```

应用包内的 `frontend`、默认配置和媒体工具都是只读资源，不会在 `.app` 内写入数据。任务临时成片仍写在用户选择的输出目录旁，上传和分析临时文件使用 macOS 的系统临时目录。

macOS 复制文件或文件夹路径时可能附带一对单引号或双引号。路径输入框会在粘贴时自动移除最外层引号，中文、空格和 `#` 等文件名字符保持不变。

### NAS 共享配置

应用启动时会优先检测 `/Volumes/home/Smartstitch`，同时兼容 Synology 通过 `homes` 共享目录挂载产生的 `/Volumes/homes/<NAS 用户名>/Smartstitch`。后者会在运行时自动映射素材和输出路径，保存时仍写回统一的 `/Volumes/home/Smartstitch` 标准路径，避免不同电脑的挂载路径互相污染。这样其他 Mac 安装 SmartStitch 并挂载同一 NAS 后，无需复制本地配置即可看到并使用共享视频库。NAS 未挂载时应用会停止启动并提示先连接 NAS，不再回退读取项目内的 `config` 目录。

非默认挂载位置可通过命令行或环境变量指定：

```bash
smartstitch --config-directory /自定义/NAS/Smartstitch
# 或
SMARTSTITCH_CONFIG_DIRECTORY=/自定义/NAS/Smartstitch smartstitch
```

共享配置被编辑时，旧版本会备份到共享目录下的 `backups`。多台电脑应避免同时编辑同一个配置文件；扫描和生成可以各自在本机运行。源码开发启动仍保持“NAS 未挂载就停止”的现有行为；正式 `.app` 在没有 NAS 时会回退到上述 Application Support 配置目录，确保应用仍能启动并使用 Web UI。

## 首次使用

本机已配置 `config/taobao-xingguang.yaml`。该文件包含真实素材路径，已设置为仅本地使用，不会提交到 Git：

- 主素材：本机业务素材目录下的 `星广一口价/25元/分割后`
- 前贴：本机业务素材目录下的 `前贴/260908同步/0.1`

风险提示语图片在每个配置中是唯一固定图片。受管标准库会自动识别 `风险提示语图片` 目录中的唯一图片；普通配置也可在“高级配置”中填写具体文件路径。将 `mode` 设为 `required` 时，缺图或多图会阻止生成。尾帧仍按素材目录配置。

视觉去重边框统一保存在 SmartStitch 配置根下的 `全局素材库/视觉去重边框`，所有项目和挂载同一 NAS 的电脑复用同一批素材。简单模式支持流式添加透明 MOV/PNG/WebP，并可随机或固定使用；动态 MOV 支持 QuickTime Animation（`qtrle`）和 ProRes 4444，且必须实际包含透明像素。旧项目内单边框路径继续兼容读取，详见[视觉去重设计](doc/18-视觉去重边框与模糊背景设计.md)。

## 权重规则

同一类别内的权重表示相对占比。例如三段素材权重为 `6 / 3 / 1`，生成 10 条时默认安排 `6 / 3 / 1` 次，然后随机打乱顺序。权重为 0 的素材不参与本批抽取。

## 输出

每批任务生成独立目录，包含：

- 成片 MP4
- `manifest.json`
- `manifest.csv`
- `config.snapshot.yaml`

任务历史保存在 `data/smartstitch.db`。运行数据与成片默认不提交版本管理。

时间线审核结果保存在 `data/timelines/<analysis-id>.json`。断点以原视频帧序号保存，
JSON 同时记录机器候选依据、人工确认断点以及由断点形成的片段范围。

受管视频库的切片清单保存在 `视频库/工作记录/切片清单`，每个切片可追溯到原视频、帧区间和入库类别。

## 测试

```bash
source .venv/bin/activate
pytest
```

## macOS arm64 内部发布

发布只由属于 `main` 历史的 `v*` Tag 触发。GitHub Actions 使用标准 Apple Silicon `macos-15` runner，在干净环境中安装锁定依赖、从固定且经过 SHA-256 校验的源码构建 FFmpeg/x264、运行测试、用 ad-hoc 签名构建 `SmartStitch.app`、执行打包后黑盒测试、生成并校验 DMG，最后创建带自动 Release Notes 的 GitHub Release。

这是公司内部使用的未公证版本，不包含 Developer ID Application 签名，也不会提交 Apple notarization。首次在其他 Mac 打开时可能受到 macOS Gatekeeper 提示。确认 DMG 来自公司的 GitHub Release 后，可以在 Finder 中按住 Control 点按应用并选择“打开”，或在“系统设置 → 隐私与安全性”中选择“仍要打开”。

工作流不需要配置任何 Apple Developer GitHub Secrets。创建 Release 和上传 DMG 使用 GitHub Actions 自动提供的 `GITHUB_TOKEN`。

发布前运行版本脚本，它会自动递增补丁版本（例如 `0.1.3` → `0.1.4`），并一次性更新 `pyproject.toml`、`smartstitch/__init__.py` 和 `uv.lock`：

```bash
packaging/bump_version.py
```

需要升级次版本或主版本时，可以显式指定，例如 `packaging/bump_version.py 0.2.0`。指定版本接受带或不带 `v` 的格式，并且必须高于当前版本。

检查并提交脚本产生的三个文件，将提交合入 `main`；Tag 必须与项目版本完全一致。之后执行：

```bash
git switch main
git pull --ff-only
git tag v0.1.4
git push origin v0.1.4
```

成功后 Release 附件名为 `SmartStitch-v0.1.4-macOS-arm64.dmg`。工作流会拒绝非 `main` 历史的 Tag、非 arm64 runner、版本不匹配、签名验证失败或包含 Homebrew 本地动态库依赖的构建。
