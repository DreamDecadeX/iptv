#!/bin/bash
set -e

export TZ=${TZ:-Asia/Shanghai}

# 默认6小时
UPDATE_INTERVAL_HOURS=${UPDATE_INTERVAL_HOURS:-6}

# 转换成秒
UPDATE_INTERVAL_SECONDS=$((UPDATE_INTERVAL_HOURS * 3600))

echo "========================================"
echo "Container started: $(date)"
echo "Update interval: ${UPDATE_INTERVAL_HOURS} hours"
echo "========================================"

cd /workspace

generate() {
    echo "========================================"
    echo "开始更新 IPTV 数据: $(date)"
    echo "========================================"

    python3 scripts/build_job.py cctv "${SOURCE_DESC}"

    python3 scripts/build_job.py satellite "${SOURCE_DESC}"

    python3 scripts/merge_cache.py

    python3 scripts/merge_state_files.py

    echo "========================================"
    echo "更新完成: $(date)"
    echo "========================================"
}

# 启动时先执行一次
generate

# 启动 HTTP 服务
python3 -m http.server 15123 --directory output &
HTTP_PID=$!

echo "HTTP Server PID: $HTTP_PID"

# 后台定时更新
while true
do
    sleep "${UPDATE_INTERVAL_SECONDS}"
    generate
done &

# 等待 HTTP 服务退出
wait $HTTP_PID