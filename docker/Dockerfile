FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-venv \
       python3-pip \
       ffmpeg \
       ca-certificates \
       bash \
       cron \
       tzdata \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境并安装 Python 依赖
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir pillow numpy opencv-python-headless requests

WORKDIR /workspace
COPY . /workspace

# 默认源描述（可被环境变量覆盖）
ENV SOURCE_DESC="高质量 → 本地源"

# 复制入口脚本并赋予执行权限
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

# 原始命令作为参数传递给 entrypoint
CMD bash -c "python3 scripts/build_job.py cctv \"${SOURCE_DESC}\" && \
             python3 scripts/build_job.py satellite \"${SOURCE_DESC}\" && \
             python3 scripts/merge_cache.py && \
             python3 scripts/merge_state_files.py && \
             python3 -m http.server 15123 --directory output"