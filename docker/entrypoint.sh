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

    # ============================
    # 生成频道
    # ============================
    python3 scripts/build_job.py cctv "${SOURCE_DESC}"

    python3 scripts/build_job.py satellite "${SOURCE_DESC}"

    # ============================
    # 合并 TXT
    # ============================
    echo "=== 合并 TXT ==="

    > output/channels_all.txt

    txt_count=0

    for f in output/channels_*.txt
    do
        [ -f "$f" ] || continue
        [ "$f" = "output/channels_all.txt" ] && continue

        txt_count=$((txt_count + 1))

        echo "合并 TXT: $f"

        cat "$f" >> output/channels_all.txt
        echo >> output/channels_all.txt
    done

    if [ "$txt_count" -eq 0 ]; then
        echo "⚠️ 没有任何 TXT 文件可合并" \
            > output/channels_all.txt
    fi

    # ============================
    # 合并 M3U
    # ============================
    echo "=== 合并 M3U ==="

    echo "#EXTM3U" > output/channels_all.m3u

    m3u_count=0

    for f in output/channels_*.m3u
    do
        [ -f "$f" ] || continue
        [ "$f" = "output/channels_all.m3u" ] && continue

        m3u_count=$((m3u_count + 1))

        echo "合并 M3U: $f"

        grep -a -v "^#EXTM3U$" "$f" \
            >> output/channels_all.m3u || true
    done

    echo "TXT文件数量: $txt_count"
    echo "M3U文件数量: $m3u_count"

    # ============================
    # 合并状态文件
    # ============================
    python3 scripts/merge_cache.py

    python3 scripts/merge_state_files.py

    echo "=== 输出目录 ==="
    ls -lh output/

    echo "========================================"
    echo "更新完成: $(date)"
    echo "========================================"
}

# ============================
# 启动 HTTP 服务
# ============================
python3 -m http.server 15123 --directory output &
HTTP_PID=$!

echo "HTTP Server PID: $HTTP_PID"

# ============================
# 启动先执行一次
# ============================
generate

# ============================
# 后台定时更新
# ============================
while true
do
    sleep "${UPDATE_INTERVAL_SECONDS}"
    generate
done &

# ============================
# 保持容器运行
# ============================
wait $HTTP_PID