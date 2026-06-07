import math
import time
import threading
import subprocess
import json
import tempfile
from pathlib import Path
from PIL import Image
import numpy as np
import cv2

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "sources/state"
CACHE_FILE = STATE_DIR / "cache.json"

# ============================
# 全局缓存 + 原始观测
# ============================

cache_lock = threading.Lock()
cache = {}
RAW_RESULTS = {}
EXPIRE_SECONDS = 12 * 3600          # 正常缓存12小时
FAILED_EXPIRE_SECONDS = 24 * 3600   # 失败源缓存24小时

# ============================
# JSON 工具
# ============================

def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# 加载缓存
cache = load_json(CACHE_FILE)

# ============================
# 静默运行子进程
# ============================

def run_silent(cmd, timeout=5):
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout
    )

# ============================
# ffprobe：获取视频流信息（分辨率、码率、帧率）
# ============================

def get_video_info(url, timeout=5):
    """
    返回: (success, width, height, bitrate, fps)
    fps 为平均帧率，如 25.0，若无法获取则返回 None
    """
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate,avg_frame_rate",
            url
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width = stream.get("width", 0)
        height = stream.get("height", 0)
        bitrate = int(stream.get("bit_rate", 0))

        # 解析帧率，如 "30000/1001" 或 "25/1"
        fps = None
        if "avg_frame_rate" in stream:
            fr = stream["avg_frame_rate"]
            if "/" in fr:
                num, den = fr.split("/")
                if den != "0":
                    fps = float(num) / float(den)
            else:
                fps = float(fr) if fr else None

        return True, width, height, bitrate, fps
    except Exception:
        return False, 0, 0, 0, None

# ============================
# 首帧延迟（真实测量）
# ============================

def measure_first_frame_delay(url, timeout=5):
    """
    返回首帧延迟（秒）
    """
    start = time.time()
    try:
        # 解码第一帧到 null 设备
        cmd = ["ffmpeg", "-v", "quiet", "-i", url, "-vframes", "1", "-f", "null", "-"]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        delay = time.time() - start
        return min(delay, timeout)
    except subprocess.TimeoutExpired:
        return float(timeout)   # 超时视为延迟=timeout
    except Exception:
        return 999.0            # 异常视为极大延迟

# ============================
# 截图清晰度（Laplacian 方差）
# ============================

def snapshot_blur_score(url, timeout=5):
    """
    返回清晰度分数（数值越大越清晰）
    """
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        cmd = ["ffmpeg", "-v", "quiet", "-y", "-i", url, "-vframes", "1", tmp]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)

        img = Image.open(tmp).convert("L")
        arr = np.array(img)
        Path(tmp).unlink(missing_ok=True)
        return cv2.Laplacian(arr, cv2.CV_64F).var()
    except Exception:
        return 0.0

# ============================
# 静态画面检测（连续帧差）
# ============================

def is_static_stream(url, timeout=8, frames=6, interval=1.0, threshold=20):
    """
    通过连续采样多帧判断是否为静态画面
    :param url: 流地址
    :param timeout: 总超时
    :param frames: 采样帧数（至少3）
    :param interval: 每帧间隔秒数（至少0.5）
    :param threshold: 相邻帧平均像素差的阈值（越大越宽松）
    :return: True 表示静态画面
    """
    try:
        tmp_files = []
        for i in range(frames):
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
            tmp_files.append(tmp)

        # 抓取 frames 帧，每帧间隔 interval 秒
        for i, tmp in enumerate(tmp_files):
            cmd = ["ffmpeg", "-v", "quiet", "-y", "-i", url, "-vframes", "1", tmp]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout / frames)
            if i < frames - 1:
                time.sleep(interval)

        imgs = []
        for tmp in tmp_files:
            img = Image.open(tmp).convert("L")
            imgs.append(np.array(img))
            Path(tmp).unlink(missing_ok=True)

        if len(imgs) < 2:
            return False

        diffs = []
        for i in range(len(imgs) - 1):
            diff = cv2.absdiff(imgs[i], imgs[i+1])
            mean_diff = np.mean(diff)
            diffs.append(mean_diff)

        avg_diff = np.mean(diffs)
        std_diff = np.std(diffs)

        # 平均差异小于阈值 且 差异变化很小 => 静态
        if avg_diff < threshold and std_diff < (threshold * 0.5):
            return True
        return False
    except Exception:
        return False   # 出错时保守认为不是静态

# ============================
# 正态分布观感映射：raw_score → 0~100
# ============================

def map_to_0_100(raw_score):
    """
    使用 tanh 映射，0 分对应 50 分
    """
    if raw_score <= -100:
        return 0.0
    x = raw_score / 25.0          # 调整敏感度，使 raw_score 在 -25~25 区间显著变化
    y = math.tanh(x)
    return (y + 1) * 50

# ============================
# 质量检测
# ============================

def quality_score(url, source="unknown"):
    """
    返回 (score, from_cache)  score: 0~100, 0 表示完全不可用
    """
    now = time.time()

    # 1. 缓存命中
    with cache_lock:
        if url in cache:
            ts = cache[url].get("ts", 0)
            score = cache[url].get("score", 0)
            # 失败源（score<=0）保留更长时间，正常源保留标准时间
            if score <= 0:
                if now - ts < FAILED_EXPIRE_SECONDS:
                    return score, True
            else:
                if now - ts < EXPIRE_SECONDS:
                    return score, True

    # 2. 获取视频信息
    ok, w, h, bitrate, fps = get_video_info(url)
    failed = (not ok) or (w == 0) or (h == 0) or (fps is None or fps <= 0)

    # 3. 静态画面检测（直接抛弃）
    if not failed:
        try:
            if is_static_stream(url, frames=3, interval=0.5):
                print(f"[static] 静态画面 → {url}")
                failed = True
        except Exception:
            pass

    # 4. 首帧延迟测量
    delay = measure_first_frame_delay(url) if not failed else 999.0

    # 5. 清晰度得分（如果未失败）
    if not failed:
        blur = snapshot_blur_score(url)
    else:
        blur = 0.0

    # 6. 评分计算
    if failed:
        raw_score = -100.0
    else:
        # ----- 分辨率得分：使用平方根，降低线性权重 -----
        resolution_score = math.sqrt(w * h) / 100.0      # 1080p -> 1440/100=14.4分
        # ----- 清晰度得分：将 Laplacian 方差映射到 0~15 分 -----
        # 典型方差范围：模糊(<100) 正常(100~500) 清晰(>500)
        blur_score = min(blur / 100.0, 15.0)            # 最高15分
        # ----- 码率得分：基于 bits per pixel (bpp) -----
        # bpp = bitrate (bps) / (fps * width * height)
        bpp = bitrate / (fps * w * h)
        # 优秀 bpp 范围 0.1~0.5，超过 0.5 不再额外加分
        bpp_score = min(bpp / 0.2, 1.0) * 10.0          # 最高10分
        # ----- 延迟惩罚：首帧延迟每多1秒扣20分，最多扣60分 -----
        delay_penalty = min(delay, 3.0) * 20.0          # 最大60分

        raw_score = (resolution_score + blur_score + bpp_score) - delay_penalty

        # 增加一个保底下限，避免极低分（但保留负分用于映射）
        raw_score = max(raw_score, -50.0)

    # 7. 映射到 0~100
    final_score = map_to_0_100(raw_score)

    # 8. 写入缓存
    with cache_lock:
        cache[url] = {
            "width": w,
            "height": h,
            "bitrate": bitrate,
            "fps": fps,
            "delay": delay,
            "blur": blur,
            "bpp": bpp if not failed else 0,
            "raw_score": raw_score,
            "score": final_score,
            "ts": now,
            "source": source
        }

    # 9. 上报原始观测
    RAW_RESULTS[url] = {
        "ok": not failed,
        "raw_score": raw_score,
        "score": final_score,
        "width": w,
        "height": h,
        "bitrate": bitrate,
        "fps": fps,
        "delay": delay,
        "blur": blur,
        "bpp": bpp if not failed else 0
    }

    return final_score, False

# ============================
# 缓存清理与保存
# ============================

def cleanup_cache():
    now = time.time()
    new_cache = {}

    for url, info in cache.items():
        ts = info.get("ts", 0)
        score = info.get("score", 0)

        if score <= 0:
            # 失败源：保留 24 小时
            if now - ts < FAILED_EXPIRE_SECONDS:
                new_cache[url] = info
        else:
            # 正常源：保留标准时长
            if now - ts < EXPIRE_SECONDS:
                new_cache[url] = info

    return new_cache

def save_all(job_name=None):
    global cache

    # 自动清理过期缓存
    cache = cleanup_cache()

    # 保存 cache.json
    save_json(CACHE_FILE, cache)

    # 保存 raw_results
    if job_name:
        raw_file = STATE_DIR / f"raw_results_{job_name}.json"
        save_json(raw_file, RAW_RESULTS)
