# NAS 更新分发与 DMG 安装引导设计

> 状态：首期实现于 2026-09-23。发布包由现有构建流程生成，发布者用 `packaging/publish_nas_update.py` 将 DMG 发布到 NAS；客户端不查询 GitHub 更新。首期的“自动分发”指自动发现、下载和校验，安装仍由用户在 DMG 中拖拽完成。真实 NAS 双机验收仍待执行。

## 1. 现状与目标

改造前 `smartstitch/updater.py` 向 GitHub Releases 查询版本，前端启动检查已暂停；`packaging/create_dmg.sh` 使用 `hdiutil -srcfolder` 生成压缩 DMG，内含 `SmartStitch.app`、`Applications` 符号链接和开源许可目录，但没有固定的 Finder 窗口布局或拖拽箭头。发布工作流只上传 GitHub Release，不向 NAS 投递。

目标是由发布者将已构建的 `SmartStitch-vX.Y.Z-macOS-arm64.dmg` 手动放到 NAS。每台已安装的 Mac 从同一 NAS 目录自动发现新版本，下载到本机并验证后显示更新入口。点击安装入口打开带“拖到 Applications”引导的 DMG。整个更新检查与获取流程不访问 GitHub，也不要求 NAS 常驻服务。

首期不在应用运行时替换 `/Applications/SmartStitch.app`，不自动结束正在运行的成片、切片或同步任务。真正的无人值守安装是独立的后续功能，需要退出后的安装助手和回滚机制。

## 2. NAS 目录与手动发布协议

更新目录独立于业务 YAML、素材库和本机 `data`：

```text
<实际挂载的 Smartstitch 配置根>/updates/
├── latest.json
├── releases/
│   ├── SmartStitch-v0.2.0-macOS-arm64.dmg
│   └── SmartStitch-v0.2.1-macOS-arm64.dmg
└── incoming/                 # 上传中的暂存区，不供客户端扫描
```

`updates` 根据本机**实际解析出的 NAS 配置根**定位：既支持 `/Volumes/home/Smartstitch`，也支持 `/Volumes/homes/<用户>/Smartstitch` 和明确指定的共享配置目录。不得把显示用的 canonical 路径反向用于文件读取。正式包回退到本机 Application Support 配置时，更新源标记为 `nas_unavailable`；不能在本机回退目录寻找或创建 `updates`。

发布者操作：

1. 从已通过打包校验的产物中取 DMG，上传到 `updates/incoming/`。复制中断时，`releases/` 和 `latest.json` 不受影响。
2. 在 NAS 同一文件系统内将文件移入 `releases/`，读取最终文件并计算字节数、SHA-256，同时核对文件名中的版本、DMG 可挂载性、应用包的版本和 arm64 架构。发布脚本应在执行这些检查后生成清单，避免人工抄写摘要。
3. 在 `updates/` 写入唯一临时清单，完成写入并关闭文件后，同目录原子重命名为 `latest.json`。**清单是唯一的“发布完成”标志**；客户端不按目录里的最大文件名自行选择更新。
4. 保留旧版本以便回退。回退时重发指向旧版的清单，但客户端默认只接受高于自身版本的更新；已升级的客户端不会被自动降级。

如果 NAS 的 SMB/NFS 挂载不能可靠提供同目录 rename 的可见性，必须在实际双机环境验证；不满足时改用带不可变版本号的清单文件和一个小型发布指针，或使用中心发布服务。单纯等待文件大小稳定不能证明上传完成。

`latest.json` 示例：

```json
{
  "schema_version": 1,
  "version": "0.2.1",
  "platform": "macos",
  "architecture": "arm64",
  "filename": "SmartStitch-v0.2.1-macOS-arm64.dmg",
  "size_bytes": 123456789,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "published_at": "2026-09-23T10:00:00+08:00",
  "notes": "本次更新摘要"
}
```

`filename` 只接受固定模式的文件名，不接受绝对路径、`..`、斜杠、符号链接或 URL。清单和 DMG 都必须处于已解析的 `updates` 根下。清单设置大小上限；版本、平台、架构、大小、SHA-256 的格式均严格校验。SHA-256 用于发现复制损坏；若要防范有 NAS 写入权限的人同时替换 DMG 和清单，后续须使用发布密钥签名清单，并在应用内固定公钥。NAS 更新目录应只允许发布者写入，普通使用者只读。

## 3. 客户端状态与交互

```text
NAS 不可用 → 正常进入应用，首页提示“未连接 NAS，请连接 NAS 后检查更新”
NAS 可用 → 读取 latest.json → 验证清单 → 比较版本
  ├── 无更新 → 不打扰用户
  └── 有更新 → 显示版本、摘要及“获取更新”
                    → 复制 DMG 到本机临时文件
                    → 校验大小和 SHA-256
                    → 原子改名为本机可打开的 DMG
                    → 显示“打开安装包”
                    → Finder 打开 DMG，用户将 App 拖到 Applications
```

- 只在正式 macOS arm64 打包版提供安装入口；源码开发模式可显示检查诊断，但不触发安装。客户端读取的是实际 NAS 路径，不再调用 GitHub API 或打开 Releases 页面。
- 启动后异步检查一次，窗口重新获得焦点时按限流间隔复查；提供手动“检查更新”。NAS 慢或断开时必须设置短超时，不能卡住主窗口、素材扫描或视频任务。NAS 不可用时，首页显示非模态提示“未连接 NAS，请连接 NAS 后检查更新”，更新区域也显示同样状态并提供“重试”按钮；恢复连接后可立即重试。避免只显示“暂无更新”或笼统的网络错误，也避免每次焦点切换都弹出模态框。
- 用现有版本比较函数判断是否有新版本。相同、较旧或架构不符的包不提示；无法解析或损坏的清单显示可诊断错误，不把它当作“没有更新”。
- 下载到本机 Application Support 的更新缓存或系统临时目录；使用唯一 `.part` 文件，流式复制并计算 SHA-256。复制期间 NAS 掉线、空间不足或校验失败时删除 `.part`，保留当前应用，不打开安装包。缓存应有容量上限，旧包可清理。
- 点击“打开安装包”只打开已完成校验的本机 DMG。若下载后清单又变化，已缓存版本仍可安装，但 UI 应标明其版本并在下一次检查提示更新版本。
- 不能在活动任务中自动退出。安装提示说明用户应先完成任务、退出 SmartStitch，再将 DMG 中的应用拖到 Applications 并确认替换。安装后本机 SQLite、用户设置和 NAS 业务配置继续位于应用包外；升级不能清空这些目录。
- 旧版没有 NAS 更新客户端时，不会被 NAS 文件自动唤醒；需要一次人工安装含新更新功能的版本。此后才能使用 NAS 自动发现。

| 情况 | 客户端行为 |
|---|---|
| `latest.json` 不存在 | 视为尚未发布更新，不报业务错误 |
| NAS 未挂载或不可访问 | 不阻止启动，首页和更新区域提示“未连接 NAS，请连接 NAS 后检查更新”，提供“重试” |
| NAS 已挂载但更新目录无读取权限 | 显示“无法读取 NAS 更新目录，请检查权限”，提供“重试”；不误报为未连接 |
| 清单字段错误、文件缺失或摘要不符 | 拒绝该版本，显示错误并允许重试 |
| 上传中断，文件只在 `incoming/` | 客户端完全不可见 |
| 新版本复制到一半 NAS 掉线 | 清理本机 `.part`，继续运行旧版 |
| 本机版本高于清单 | 不降级，保留手动安装途径 |
| 正在渲染或切片 | 可检查、下载；不自动退出或覆盖 App |

接口可延续 `/api/v1/system/updates`，返回 `source: "nas"`、可用版本、状态和错误码；将现有 `/updates/open` 改为只打开**已校验的本地 DMG**，不再打开 GitHub。不要通过 HTTP 接口接受任意 NAS 路径或通用文件打开命令。

## 4. DMG 拖拽引导页研究与方案

常见拖拽页本质上是 **Finder 打开磁盘映像根目录时的视图**，而不是 DMG 内的网页或安装向导。左侧是真实的 `SmartStitch.app`，右侧是真实的 `/Applications` 链接；背景图只负责品牌、箭头和“拖到应用程序文件夹”的文字。Finder 窗口尺寸、图标大小和位置作为映像内的视图元数据保存，最后再转成只读压缩 DMG。`create-dmg` 提供 `--background`、`--window-size`、`--icon`、`--app-drop-link` 等参数；它仍依赖 Finder/AppleScript 来调整外观，CI 中必须实测稳定性。

SmartStitch 建议布局：

```text
约 660 × 400 pt Finder 内容区
┌────────────────────────────────────────────┐
│ SmartStitch                  安装到 Mac      │
│                                            │
│   [SmartStitch.app]   ─────→   [Applications]│
│                                            │
│          将 SmartStitch 拖到“应用程序”       │
│                             开源许可 →      │
└────────────────────────────────────────────┘
```

- 背景图提供 1× 和 2× 清晰资源；不要把 App 或 Applications 图标画进背景，避免真实 Finder 图标与图片错位或重叠。
- 两个真实图标保持同一视觉基线，图标大小约 100–120 pt，箭头放在二者之间；背景上的说明文字不能遮挡 Finder 自动绘制的文件名。
- 保留当前 DMG 中的开源许可内容，放在底部可见入口或一个清晰的许可文件夹中；不要为追求只有两个大图标而丢弃许可材料。
- 卷名包含版本，App 名称仍为 `SmartStitch.app`。打开 DMG 后应一眼看到拖拽方向；复制完成后提示退出旧版再打开 Applications 中的新应用，不鼓励直接从已挂载的 DMG 运行。
- 当前应用采用 ad-hoc 签名、没有 Developer ID 公证。拖拽背景不会改变 Gatekeeper 的行为；NAS 获取的包仍要在目标 Mac 上做首次打开验收。未来若升级为 Developer ID 签名和公证，按 Apple 的分发流程另行处理。

实现上可在 `packaging/create_dmg.sh` 引入固定版本的 `create-dmg`，或自维护 `hdiutil + Finder AppleScript` 步骤。优先评估前者以减少布局脚本；必须固定工具版本、保留现有签名/FFmpeg/许可验证，并为 Finder 自动化设置超时和明确失败信息。仅运行 `hdiutil verify` 能验证映像结构，**不能验证背景、图标和箭头是否呈现正确**；正式验收需在 macOS Finder 中挂载后截图检查。CI 若无可操作的 Finder 会话，不能以降级成无引导页的 DMG 冒充成功，应把视觉验收放在具备图形会话的发布机上。

## 5. 落地顺序与验收

1. 实现发布脚本：验证本地 DMG，上传或接收用户传入的 `incoming` 文件，生成清单并最后发布；在实际 NAS 上验证原子可见性。
2. 替换 GitHub 更新检查为 NAS 清单读取，补充路径、安全校验、状态 API 和前端提示。
3. 实现本机流式下载、哈希验证和打开 DMG；保留任务运行中不自动退出的约束。
4. 制作背景资源并更新 DMG 脚本，验证签名、内容、许可证、DMG 完整性和 Finder 实际布局。
5. 两台不同挂载方式的 Mac 验证：正常升级、上传中断、NAS 断线、无权限、损坏清单、摘要不符、活动任务、替换旧 App，以及升级后配置和任务历史保留。

参考资料：

- [create-dmg 项目及布局参数](https://github.com/create-dmg/create-dmg)
- [Apple：macOS 软件分发与 Gatekeeper](https://developer.apple.com/macos/distribution/)
- [Apple：打包 Mac 软件用于分发](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
- [hdiutil 手册：映像创建、转换与校验](https://keith.github.io/xcode-man-pages/hdiutil.1.html)
