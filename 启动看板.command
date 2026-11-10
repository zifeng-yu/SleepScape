#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/sleepscape ]]; then
    print '请先在项目目录运行：uv sync --locked'
    read '?按回车关闭。'
    exit 1
fi
print 'SleepScape 看板启动后，请打开下方显示的本机地址。'
print '默认地址：http://127.0.0.1:8000；在此窗口按 Ctrl+C 停止。'
exec .venv/bin/sleepscape serve
