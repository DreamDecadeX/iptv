FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-venv \
       python3-pip \
       ffmpeg \
       ca-certificates \
       bash \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir pillow numpy opencv-python-headless requests

WORKDIR /workspace
COPY . /workspace

# 定义环境变量，默认值为 '高质量 → 本地源'
ENV SOURCE_DESC="高质量 → 本地源"

# 在 CMD 中引用该变量
CMD bash -c "python3 scripts/build_job.py cctv \"${SOURCE_DESC}\" && \
             python3 scripts/build_job.py satellite \"${SOURCE_DESC}\" && \
             python3 scripts/merge_cache.py && \
             python3 scripts/merge_state_files.py && \
             python3 -m http.server 15123 --directory output"