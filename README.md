# SmartStitch

SmartStitch 是一个本地运行的 Python + FFmpeg 视频随机拼接工具，提供浏览器管理界面。它支持：

- 按配置组合前贴、引子、利益点视频、结尾和尾帧。
- 为每个素材设置权重，并按整批配额分配后随机洗牌。
- 叠加透明利益点图片，支持叠加时段、位置和透明度。
- 多套 YAML 配置、中文路径、外接硬盘和 macOS `._` 文件过滤。
- 网页可视化调整素材目录、模块开关、叠图、输出和批处理配置，并保留 YAML 专家模式。
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

## 首次使用

本机已配置 `config/taobao-xingguang.yaml`。该文件包含真实素材路径，已设置为仅本地使用，不会提交到 Git：

- 主素材：本机业务素材目录下的 `星广一口价/25元/分割后`
- 前贴：本机业务素材目录下的 `前贴/260908同步/0.1`

利益点图片和尾帧因为尚未提供实际目录，默认停用。打开网页后点击“高级配置”，填写对应目录，并将 `mode: disabled` 改成 `mode: required` 即可启用。

## 权重规则

同一类别内的权重表示相对占比。例如三段素材权重为 `6 / 3 / 1`，生成 10 条时默认安排 `6 / 3 / 1` 次，然后随机打乱顺序。权重为 0 的素材不参与本批抽取。

## 输出

每批任务生成独立目录，包含：

- 成片 MP4
- `manifest.json`
- `manifest.csv`
- `config.snapshot.yaml`

任务历史保存在 `data/smartstitch.db`。运行数据与成片默认不提交版本管理。

## 测试

```bash
source .venv/bin/activate
pytest
```
