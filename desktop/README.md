# Agent Hub macOS 桌面版

打开 `Agent Hub.app` 即可使用独立窗口，无需终端、Python 或 Xcode。关闭主窗口或按 `⌘Q` 会退出应用，同时停止它启动的服务和灵动岛。支持搜索、按状态/项目查看、对话跳转，以及 `⌘C`、`⌘V`、`⌘R` 等快捷键。菜单栏的「Agent Hub → 显示灵动岛」可以开关灵动岛。

## 安装和数据

打开 DMG，将 Agent Hub 拖入 Applications。当前构建适用于 Apple Silicon（M 系列）Mac，macOS 13 及以上。v1.0.1 起的发布安装包已完成 Developer ID 正式签名和 Apple 公证，DMG 已附加并验证公证票据。初始发布的 v1.0.0 为本地 ad-hoc 签名；自行构建时请以实际签名和公证结果为准。

会话继续从原应用的本地数据中只读获取。已读记录保存在 `~/Library/Application Support/Agent Hub/agent_hub_state.json`，日志保存在 `~/Library/Logs/Agent Hub/desktop.log`。应用升级不会覆盖这些数据。安装包不包含任何个人会话、已读记录或凭据。卸载时将应用移到废纸篓即可；保留的数据不会影响重新安装。

## 构建

构建机需要 macOS、Xcode 命令行工具、Python 3.12 和 `desktop/requirements-build.txt` 中的依赖。建议使用可分发的 CPython 运行环境，如 uv 管理的 Python。

```bash
uv venv --python 3.12 --managed-python .build/packaging-env
uv pip install --python .build/packaging-env/bin/python -r desktop/requirements-build.txt
.build/packaging-env/bin/python desktop/build.py --dmg
```

产物位于 `dist/Agent Hub.app` 和 `dist/Agent-Hub-1.0.2-macOS-arm64.dmg`。构建脚本保留 Python/PyInstaller 许可及上游 Apache-2.0 许可。重新构建会替换 `dist` 内的同名产物，不会改动已安装的应用。

Swift/AppKit 和系统 WebKit 提供原生窗口，PyInstaller 封装后端及 Python 运行环境，灵动岛预先编译。桌面服务优先监听本机 `127.0.0.1:47322`，占用时自动换端口。主进程异常退出后，后端也会自动停止。

## Developer ID 签名与 Apple 公证

正式分发需要有效的 Apple Developer Program 会员、已安装在本机钥匙串的 Developer ID Application 证书及对应私钥。不要将私钥、专用密码或 API 密钥提交到仓库。

```bash
security find-identity -v -p codesigning
.build/packaging-env/bin/python desktop/build.py --dmg \
  --version 1.0.2 --build-number 3 \
  --sign-identity "Developer ID Application: YOUR NAME (TEAMID)"
```

签名构建会从内向外签署所有内嵌原生代码，启用 Hardened Runtime，取得安全时间戳，并签署 DMG。未传入 `--sign-identity` 时仍生成本地 ad-hoc 签名版本。

在自己的终端安全输入 Apple 账号专用密码，将认证保存到本机钥匙串；不需要把密码写入命令或源码：

```bash
xcrun notarytool store-credentials agent-hub-notary \
  --apple-id YOUR_APPLE_ACCOUNT --team-id YOUR_TEAM_ID \
  --keychain "$HOME/Library/Keychains/login.keychain-db"
```

随后提交公证：

```bash
.build/packaging-env/bin/python desktop/notarize.py \
  dist/Agent-Hub-1.0.2-macOS-arm64.dmg --keychain-profile agent-hub-notary \
  --keychain "$HOME/Library/Keychains/login.keychain-db"
```

保存认证与提交公证时使用相同的钥匙串路径，避免不同运行会话找不到认证记录。公证脚本会保存 submission ID 与结果至 `.build/notarization/`；只有 Apple 返回 `Accepted` 后才附加并验证票据，然后重新生成最终 DMG 的 SHA-256 文件。若中途断开，可加 `--submission-id ID` 继续等待已有任务，避免重复上传。发布前检查日志并测试下载后的安装体验；仅完成 Developer ID 签名不代表已通过公证。
