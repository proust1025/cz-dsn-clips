#!/bin/bash
# 抖音切片自动抓取 —— 入口脚本
#   ./run.sh login   首次使用：扫码登录抖音
#   ./run.sh run     立即跑一次抓取（定时任务也是调这个），跑完自动同步到飞书多维表格
#   ./run.sh check   检查登录态是否还有效
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/proust/.workbuddy/binaries/python/envs/douyin/bin/python"
cd "$DIR"

case "${1:-run}" in
  login)
    exec "$PY" login.py
    ;;
  run)
    if [ $# -gt 0 ]; then shift; fi
    "$PY" harvest.py "$@"
    echo "[$(date +%H:%M:%S)] 正在将本次新下载的视频同步到飞书多维表格…"
    "$PY" sync_feishu.py || echo "⚠️ 飞书同步失败（详见日志 data/logs/），下次运行会自动重试"
    echo "[$(date +%H:%M:%S)] 正在用语音识别提取视频脚本（仅处理新视频）…"
    "$PY" extract_scripts.py || echo "⚠️ 脚本提取失败（详见日志），下次运行会自动重试"
    "$PY" sync_scripts.py || echo "⚠️ 脚本同步到飞书失败（详见日志），下次运行会自动重试"
    ;;
  check)
    exec "$PY" -c "
from playwright.sync_api import sync_playwright
from core import launch_browser, load_config, setup_logger, is_logged_in
log = setup_logger('check')
cfg = load_config()
cfg['headless'] = True
with sync_playwright() as p:
    ctx = launch_browser(p, cfg, log)
    ok = is_logged_in(ctx.cookies())
    print('登录态有效' if ok else '登录态已失效，请运行 ./run.sh login 重新扫码')
    ctx.close()
"
    ;;
  *)
    echo "用法: ./run.sh [login|run|check]"
    exit 1
    ;;
esac
