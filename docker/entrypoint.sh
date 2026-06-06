#!/bin/bash
set -e

# 确保时区为中国时区（如果环境变量 TZ 未设置，默认已通过 ENV 设置）
export TZ=${TZ:-Asia/Shanghai}
echo "Container timezone: $(date)"

# 如果用户提供了 CRON_SCHEDULE 环境变量，则设置定时任务
if [ -n "$CRON_SCHEDULE" ]; then
    echo "Setting up cron schedule: $CRON_SCHEDULE"
    # 写入 crontab：在指定时间执行 pkill -f "python3 -m http.server"
    # 这会导致 HTTP 服务器终止，进而容器主进程退出，Docker 自动重启容器
    (crontab -l 2>/dev/null || echo "") | { cat; echo "$CRON_SCHEDULE root pkill -f 'python3 -m http.server' > /proc/1/fd/1 2>&1"; } | crontab -
    # 启动 cron 服务（后台运行）
    service cron start
    echo "Cron daemon started."
fi

# 执行传递给 CMD 的命令（即原始的一大串构建 + 启动服务器）
exec "$@"