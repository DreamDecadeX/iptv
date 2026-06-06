#!/bin/bash
set -e

export TZ=${TZ:-Asia/Shanghai}

echo "Container timezone: $(date)"

# 设置定时任务
if [ -n "$CRON_SCHEDULE" ]; then
    echo "Setting up cron schedule: $CRON_SCHEDULE"

    cat <<EOF >/etc/cron.d/restart-job
$CRON_SCHEDULE root pkill -f "python3 -m http.server"
EOF

    chmod 0644 /etc/cron.d/restart-job

    service cron start

    echo "Cron daemon started."
fi

echo "开始生成 IPTV 数据..."

python3 scripts/build_job.py cctv "${SOURCE_DESC}"

python3 scripts/build_job.py satellite "${SOURCE_DESC}"

python3 scripts/merge_cache.py

python3 scripts/merge_state_files.py

echo "启动 HTTP 服务..."

exec python3 -m http.server 15123 --directory output