# Agent Hub macOS 桌面版

打开 `Agent Hub.app` 即可使用独立窗口，无需终端、Python 或 Xcode。关闭主窗口或按 `⌘Q` 会退出应用，同时停止它启动的服务和灵动岛。支持搜索、按状态/项目查看、对话跳转，以及 `⌘C`、`⌘V`、`⌘R` 等快捷键。菜单栏的「Agent Hub → 显示灵动岛」可以开关灵动岛。

## 安装和数据

打开 DMG，将 Agent Hub 拖入 Applications。当前构建适用于 Apple Silicon（M 系列）Mac，macOS 13 及以上。应用采用本地 ad-hoc 签名，尚未使用 Apple Developer ID 签名或公证；若要公开分发为无系统拦截的安装包，还需开发者证书和公证。

会话继续从原应用的本地数据中只读获取。已读记录保存在 `~/Library/Application Support/Agent Hub/agent_hub_state.json`，日志保存在 `~/Library/Logs/Agent Hub/desktop.log`。应用升级不会覆盖这些数据。安装包不包含任何个人会话、已读记录或凭据。卸载时将应用移到废纸篓即可；保留的数据不会影响重新安装。

## 构建

构建机需要 macOS、Xcode 命令行工具、Python 3.12 和 `desktop/requirements-build.txt` 中的依赖。建议使用可分发的 CPython 运行环境，如 uv 管理的 Python。

```bash
uv venv --python 3.12 --managed-python .build/packaging-env
uv pip install --python .build/packaging-env/bin/python -r desktop/requirements-build.txt
.build/packaging-env/bin/python desktop/build.py --dmg
```

产物位于 `dist/Agent Hub.app` 和 `dist/Agent-Hub-1.0.0-macOS-arm64.dmg`。构建脚本保留 Python/PyInstaller 许可及上游 Apache-2.0 许可。重新构建会替换 `dist` 内的同名产物，不会改动已安装的应用。

Swift/AppKit 和系统 WebKit 提供原生窗口，PyInstaller 封装后端及 Python 运行环境，灵动岛预先编译。桌面服务优先监听本机 `127.0.0.1:47322`，占用时自动换端口。主进程异常退出后，后端也会自动停止。
