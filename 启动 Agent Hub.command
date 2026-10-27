#!/bin/bash
# 双击启动：打开浏览器面板；关闭这个终端窗口即退出
cd "$(dirname "$0")"
exec python3 agent_hub.py
