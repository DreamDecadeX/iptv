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
EXPIRE_SECONDS = 12 * 3600          # 成功源12小时
FAILED_EXPIRE_SECONDS = 24 * 3600   # 失败源24小时

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
# ffprobe：分辨率 + 码率
# ============================

def probe_stream(url, timeout=5):
    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate",
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
        streams = data.get("streams", [])

        if not streams:
            return False, 0, 0, None

        stream = streams[0]

        width = stream.get("width", 0)
        height = stream.get("height", 0)

        fps = None
        fr = stream.get("avg_frame_rate")

        if fr:
            try:
                if "/" in fr:
                    num, den = fr.split("/")
                    if float(den) != 0:
                        fps = float(num) / float(den)
                else:
                    fps = float(fr)
            except:
                pass

        return True, width, height, fps

    except:
        return False, 0, 0, None

# ============================
# ffmpeg：首帧延迟
# ============================

def measure_first_frame_delay(url, timeout=5):
    start = time.time()

    try:
        cmd = [
            "ffmpeg",
            "-v", "quiet",
            "-i", url,
            "-vframes", "1",
            "-f", "null",
            "-"
        ]

        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout
        )

        return min(time.time() - start, timeout)

    except subprocess.TimeoutExpired:
        return float(timeout)

    except:
        return 999.0

# ============================
# ffmpeg：截图 + 清晰度（Laplacian）
# ============================

def snapshot_blur_score(url, timeout=5):
    tmp = None

    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False
        ).name

        cmd = [
            "ffmpeg",
            "-v", "quiet",
            "-y",
            "-i", url,
            "-vframes", "1",
            tmp
        ]

        run_silent(cmd, timeout=timeout)

        img = Image.open(tmp).convert("L")
        arr = np.array(img)

        return cv2.Laplacian(
            arr,
            cv2.CV_64F
        ).var()

    except:
        return 0

    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)

# ============================
# ffmpeg：检测静态画面（帧差）
# ============================

def is_static_stream(url, timeout=8, frames=5, interval=1.0, threshold=20):
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
        # 临时文件列表
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

        # 读取所有帧为灰度图
        imgs = []
        for tmp in tmp_files:
            img = Image.open(tmp).convert("L")
            imgs.append(np.array(img))
            # 删除临时文件
            Path(tmp).unlink(missing_ok=True)

        if len(imgs) < 2:
            return False

        # 计算相邻帧之间的像素差绝对值均值
        diffs = []
        for i in range(len(imgs) - 1):
            diff = cv2.absdiff(imgs[i], imgs[i+1])
            mean_diff = np.mean(diff)
            diffs.append(mean_diff)

        # 取平均差值和差异性（标准差）
        avg_diff = np.mean(diffs)
        std_diff = np.std(diffs)

        # 判断：平均差异小于阈值 并且 差异变化很小（稳定静态）
        if avg_diff < threshold and std_diff < (threshold * 0.5):
            return True
        else:
            return False

    except Exception as e:
        # 出错时保守认为不是静态（避免误杀）
        return False

# ============================
# 质量检测（核心）
# ============================

def quality_score(url, source="unknown"):
    now = time.time()

    with cache_lock:
        if url in cache:
            info = cache[url]

            ts = info.get("ts", 0)
            score = info.get("score", 0)

            if score <= 0:
                if now - ts < FAILED_EXPIRE_SECONDS:
                    return score, True
            else:
                if now - ts < EXPIRE_SECONDS:
                    return score, True

    ok, w, h, fps = probe_stream(url)

    delay = measure_first_frame_delay(url)
    blur = snapshot_blur_score(url)

    failed = (
        (not ok)
        or (w <= 0)
        or (h <= 0)
    )

    # IPTV静态检测误判严重，默认关闭
    # if not failed:
    #     try:
    #         if is_static_stream(url):
    #             failed = True
    #     except:
    #         pass

    if failed:
        raw_score = 0
        final_score = 0

    else:

        pixels = w * h

        # 分辨率评分
        if pixels >= 3840 * 2160:
            resolution_score = 50

        elif pixels >= 1920 * 1080:
            resolution_score = 40

        elif pixels >= 1280 * 720:
            resolution_score = 25

        elif pixels >= 720 * 576:
            resolution_score = 15

        else:
            resolution_score = 0

        # 清晰度评分
        blur_score = min(
            math.sqrt(max(blur, 0)),
            25
        )

        # FPS评分
        if fps is None:
            fps_score = 10
        else:
            fps_score = min(fps, 60) / 60 * 20

        # 延迟惩罚
        delay_penalty = min(delay, 4.0) * 1.5

        raw_score = (
            resolution_score
            + blur_score
            + fps_score
            - delay_penalty
        )

        final_score = max(
            0,
            min(raw_score, 100)
        )

    with cache_lock:
        cache[url] = {
            "width": w,
            "height": h,
            "fps": fps,
            "delay": delay,
            "blur": blur,
            "raw_score": raw_score,
            "score": final_score,
            "ts": now,
            "source": source
        }

    RAW_RESULTS[url] = {
        "ok": not failed,
        "raw_score": raw_score,
        "score": final_score,
        "width": w,
        "height": h,
        "fps": fps,
        "delay": delay,
        "blur": blur
    }

    return final_score, False

# ============================
# 保存（cache + raw_results）
# ============================

def cleanup_cache():
    now = time.time()
    new_cache = {}

    for url, info in cache.items():

        ts = info.get("ts", 0)
        score = info.get("score", 0)

        # 失败源缓存24小时
        if score <= 0:
            if now - ts < FAILED_EXPIRE_SECONDS:
                new_cache[url] = info

        # 成功源缓存12小时
        else:
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
