import re
import json
import sys
import time
import random
import requests
import urllib.parse
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from quality_raw import (
    quality_score,
    cache,
    save_all
)

# ============================
# 全局路径
# ============================

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
STATE_DIR = SOURCES_DIR / "state"
OUTPUT_DIR = ROOT / "output"

LIVE_URLS_FILE = SOURCES_DIR / "live_urls.txt"
CHANNEL_LIST_FILE = SOURCES_DIR / "channel_list.txt"
BLACKLIST_FILE = SOURCES_DIR / "blacklist.txt"

UPSTREAM_FAIL_FILE = STATE_DIR / "upstream_fail.json"
UPSTREAM_BLOCKLIST_FILE = STATE_DIR / "upstream_blocklist.json"

# ============================
# 全局变量
# ============================

WHITELIST_ORDER = []   # 保持顺序的列表
WHITELIST_SET = set()  # 用于快速成员判断
BLACKLIST = []

URL_SOURCE = {}              # URL → 上游源
SOURCE_TOTAL = defaultdict(int)
SOURCE_FAIL = defaultdict(int)

UPSTREAM_FAIL = defaultdict(int)
UPSTREAM_BLOCKLIST = {}

FILTERED_LOG = defaultdict(list)

# 最低源数量
MINI_RAW_URLS = 5

# 自动创建必要目录（本地调试用）
SOURCES_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 显示文案 → 内部变量
SORT_MODE_MAP = {
    # 本地源优先
    "本地源 → 高质量":                 "local_high",
    "本地源 → 高质量 → 中质量":         "local_high_mid",
    "本地源 → 高质量 → 中质量 → 低质量": "local_high_mid_low",

    # 高质量优先
    "高质量":                         "high",
    "高质量 → 中质量":                 "high_mid",
    "高质量 → 中质量 → 低质量":         "high_mid_low",

    # 混合模式
    "高质量 → 本地源":                 "high_local",
    "高质量 → 本地源 → 中质量":         "high_local_mid",
    "高质量 → 中质量 → 本地源":         "high_mid_local",

    # 内部值兼容
    "local_high": "local_high",
    "local_high_mid": "local_high_mid",
    "local_high_mid_low": "local_high_mid_low",

    "high": "high",
    "high_mid": "high_mid",
    "high_mid_low": "high_mid_low",

    "high_local": "high_local",
    "high_local_mid": "high_local_mid",
    "high_mid_local": "high_mid_local",
}

# ============================
# 工具函数
# ============================

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except:
            return default
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def is_numeric_channel(name: str) -> bool:
    n = name.strip()
    n = re.sub(r"[台频道]+$", "", n)
    return n.isdigit()

def get_quality_flags():
    """
    根据排序模式返回：
    ENABLE_HIGH, ENABLE_MID, ENABLE_LOW
    """
    if SORT_MODE in (
        "local_high",
        "high_local",
        "high"
    ):
        return True, False, False

    elif SORT_MODE in (
        "local_high_mid",
        "high_local_mid",
        "high_mid_local",
        "high_mid"
    ):
        return True, True, False

    elif SORT_MODE in (
        "local_high_mid_low",
        "high_mid_low"
    ):
        return True, True, True

    return True, False, False

def sort_urls_by_mode(local_urls,use_high,use_mid,use_low):
    """
    根据 SORT_MODE 排序
    """
    if SORT_MODE == "local_high":
        return local_urls + use_high

    elif SORT_MODE == "high_local":
        return use_high + local_urls

    elif SORT_MODE == "local_high_mid":
        return local_urls + use_high + use_mid

    elif SORT_MODE == "high_local_mid":
        return use_high + local_urls + use_mid

    elif SORT_MODE == "high_mid_local":
        return use_high + use_mid + local_urls

    elif SORT_MODE == "local_high_mid_low":
        return local_urls + use_high + use_mid + use_low

    elif SORT_MODE == "high":
        return use_high

    elif SORT_MODE == "high_mid":
        return use_high + use_mid

    elif SORT_MODE == "high_mid_low":
        return use_high + use_mid + use_low

    return local_urls + use_high

def get_sorted_urls(name, raw_urls,enable_high,enable_mid,enable_low):
    """
    根据排序模式返回最终 urls
    """
    local_urls = [u for u in raw_urls if is_local_source(u)]
    remote_urls = [u for u in raw_urls if not is_local_source(u)]

    sorted_remote = detect_and_sort_urls(name, remote_urls)

    def score_of(u):
        return cache.get(normalize_url(u), {}).get("score", 0)

    high_remote = [u for u in sorted_remote if score_of(u) > 90]
    mid_remote  = [u for u in sorted_remote if 60 <= score_of(u) <= 90]
    low_remote  = [u for u in sorted_remote if 10 <= score_of(u) < 60]

    use_high = high_remote if enable_high else []
    use_mid  = mid_remote  if enable_mid  else []
    use_low  = low_remote  if enable_low  else []

    return sort_urls_by_mode(
        local_urls,
        use_high,
        use_mid,
        use_low
    )

# ============================
# 名称规范化
# ============================

def normalize_name(name: str) -> str:
    raw = name.strip().upper()

    m = re.match(r"CCTV[- ]?0?(\d+)", raw)
    if m:
        return f"CCTV{m.group(1)}"

    m2 = re.match(r"(.*?卫视)", raw)
    if m2:
        return m2.group(1)

    cleaned = re.sub(r"(4K|8K|HD|FHD|UHD|超清|高清|标清|频道|综合)$", "", raw)
    cleaned = re.sub(r"\d+[MPKp]+$", "", cleaned)
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", cleaned)

    return cleaned

# ============================
# URL 过滤 / 归一化
# ============================

def is_good_url(u: str) -> bool:
    u = u.strip()
    if not u.startswith("http"):
        return False
    if u.endswith("$"):
        return False
    bad = ["udp/", "rtp/", "://239.", "://224."]
    return not any(k in u for k in bad)

def is_local_source(url: str, from_local_spider=False) -> bool:
    u = url.lower()
    if from_local_spider:
        return (
            u.startswith("rtp://")
            or u.startswith("udp://")
            or "://239." in u
            or "://224." in u
            or "/rtp/" in u
        )
    return False  # 默认情况下不认为是本地源

def score_of(u):
    return cache.get(normalize_url(u), {}).get("score", 0)

def normalize_url(url: str) -> str:
    if not url.startswith("http"):
        return url

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))

    drop_keys = {
        "token", "auth", "ts", "sign", "expires", "expiry",
        "e", "_t", "_ts", "uuid", "session", "sessionid",
        "v", "ver", "random", "r", "t"
    }

    query = {k: v for k, v in query.items() if k.lower() not in drop_keys}
    sorted_query = urlencode(sorted(query.items()))

    path = parsed.path.rstrip("/")
    path = re.sub(r"\.m3u8.*$", ".m3u8", path)
    path = re.sub(r"\.flv.*$", ".flv", path)

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        sorted_query,
        parsed.fragment
    ))

# ============================
# 添加频道源
# ============================

def add_channel(channels, name, url, source_url=None, from_local_spider=False):
    name = normalize_name(name)
    url = normalize_url(url.strip())

    if not name or not url:
        return

    if name not in WHITELIST_SET:
        for key in BLACKLIST:
            if key in name:
                FILTERED_LOG[name].append({
                    "url": url,
                    "keyword": key,
                    "source": source_url
                })
                return

    # 本地源也要加入 channels
    if not is_good_url(url) and not is_local_source(url, from_local_spider):
        return

    if source_url == "local_spider":
        URL_SOURCE[url] = "local_spider"
    elif url not in URL_SOURCE:
        URL_SOURCE[url] = source_url

    if url not in channels[name]:
        channels[name].append(url)

# ============================
# 解析 TXT / M3U / JSON
# ============================

def parse_txt_like(content, channels, source_url=None, from_local_spider=False):
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ",http" in line:
            name, url = line.split(",", 1)
        elif "#" in line and "http" in line:
            name, url = line.split("#", 1)
        else:
            continue
        add_channel(channels, name, url, source_url, from_local_spider)

def parse_m3u(content, channels, source_url=None, from_local_spider=False):
    last_name = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            if "," in line:
                last_name = line.split(",", 1)[1].strip()
        elif line and not line.startswith("#") and last_name:
            add_channel(channels, last_name, line, source_url, from_local_spider)
            last_name = None

def parse_tvbox_json(content, channels, source_url=None, from_local_spider=False):
    try:
        data = json.loads(content)
    except:
        return
    lives = data.get("lives") or []
    for live in lives:
        for ch in live.get("channels", []):
            name = ch.get("name")
            urls = ch.get("urls") or []
            for url in urls:
                add_channel(channels, name, url, source_url, from_local_spider)

def detect_and_parse(content, channels, source_url=None, from_local_spider=False):
    text = content.lstrip()
    if text.startswith("{") and '"lives"' in text:
        parse_tvbox_json(text, channels, source_url, from_local_spider)
    elif "#EXTM3U" in text or "#EXTINF" in text:
        parse_m3u(text, channels, source_url, from_local_spider)
    else:
        parse_txt_like(text, channels, source_url, from_local_spider)

# ============================
# 并发测速 + 上游源统计
# ============================

def detect_and_sort_urls(name, urls, is_entertainment=False):
    urls = list(set(urls))
    good_urls = [u for u in urls if is_good_url(u)]
    total = len(good_urls)

    print(f"\n[{name}] 开始检测，共 {total} 条源\n", flush=True)

    results = {}
    THREADS = 4

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        future_map = {}

        for u in good_urls:

            if is_local_source(u):
                results[u] = 100.0
                print(f"[{name}] 本地源 → 默认 100 分 | {u}", flush=True)

                src = URL_SOURCE.get(u)
                if src:
                    SOURCE_TOTAL[src] += 1
                continue

            src = URL_SOURCE.get(u, "unknown")
            future_map[exe.submit(quality_score, u, src)] = u

        for idx, future in enumerate(as_completed(future_map), start=1):
            url = future_map[future]
            score, cached = future.result()

            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            delay = info.get("delay", 0)
            blur = info.get("blur", 0)
            err = info.get("error", "")

            print(
                f"[{name}] {idx}/{total} "
                f"{'缓存' if cached else '检测'} → "
                f"{w}x{h} | 延迟 {delay}s | 清晰度 {blur:.1f} | 得分 {score:.1f}",
                flush=True
            )

            src = URL_SOURCE.get(url)
            if src:
                SOURCE_TOTAL[src] += 1
                if score <= 0:
                    SOURCE_FAIL[src] += 1

            if err in ("timeout", "connection refused", "network unreachable", "dns error"):
                continue

            if score <= 0:
                continue

            results[url] = score

    if is_entertainment:
        filtered = {}
        for url, score in results.items():
            info = cache.get(url, {})
            w = info.get("width", 0)
            h = info.get("height", 0)
            if w >= 1280 and h >= 720:
                filtered[url] = score
        results = filtered

    print(f"[{name}] 检测完成，可用 {len(results)} / {total}\n", flush=True)

    return sorted(results.keys(), key=lambda u: results[u], reverse=True)

# ============================
# TXT 输出
# ============================

def build_output_txt(channels, mode, whitelist_order):
    lines = []

    # ============================
    # 排序模式 → 启用哪些质量档
    # ============================
    ENABLE_HIGH, ENABLE_MID, ENABLE_LOW = get_quality_flags()

    # ============================
    # 电视台部分
    # ============================
    if mode in ("all", "cctv", "satellite"):
        lines.append("电视频道,#genre#")

        # 遍历白名单顺序列表
        for name in whitelist_order:   # 需要将 whitelist_order 作为参数传入
            # 模式过滤
            if mode == "cctv" and not name.startswith("CCTV"):
                continue
            if mode == "satellite" and name.startswith("CCTV"):
                continue

            # 如果该频道在 channels 中不存在，跳过
            if name not in channels:
                continue
            raw_urls = channels[name]
            # ============================
            # 排序逻辑
            # ============================
            urls = get_sorted_urls(
                name,
                raw_urls,
                ENABLE_HIGH,
                ENABLE_MID,
                ENABLE_LOW
            )

            # 输出
            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    # ============================
    # 媒体频道部分（保持原样）
    # ============================
    if mode in ("all", "entertainment"):
        lines.append("媒体频道,#genre#")
        for name in sorted(channels.keys()):
            if name in WHITELIST_SET:
                continue
            raw_urls = channels[name]

            if len(raw_urls) < MINI_RAW_URLS:
                continue

            if is_numeric_channel(name):
                continue

            urls = detect_and_sort_urls(name, raw_urls, is_entertainment=True)

            for url in urls:
                lines.append(f"{name},{url}")
            lines.append("")

    return "\n".join(lines)

# ============================
# M3U 输出
# ============================

LOGO_BASES = [
    "https://www.xn--rgv465a.top/tvlogo/",
    "https://live.fanmingming.cn/tv/",
    "https://gitee.com/cquptxiong/live/raw/main/tv/"
]

def build_output_m3u(channels, mode, whitelist_order):
    lines = []
    lines.append("#EXTM3U")

    def get_logo(name):
        if name.startswith("CCTV"):
            num = name.replace("CCTV", "")
            filename_main = f"CCTV-{num}.png"
            filename_alt  = f"CCTV{num}.png"
        else:
            filename_main = f"{name}.png"
            filename_alt  = f"{name}.png"

        filename_main = urllib.parse.quote(filename_main)
        filename_alt  = urllib.parse.quote(filename_alt)

        for base in LOGO_BASES:
            url = base + filename_main
            try:
                if requests.head(url, timeout=1.5).status_code == 200:
                    return url
            except:
                pass

        for base in LOGO_BASES:
            url = base + filename_alt
            try:
                if requests.head(url, timeout=1.5).status_code == 200:
                    return url
            except:
                pass

        return None

    def get_group(name):
        # 白名单中的频道归为“电视频道”，否则归为“媒体频道”
        if name in WHITELIST_SET:   # 全局集合
            return "电视频道"
        return "媒体频道"

    # ============================
    # 排序模式 → 启用哪些质量档
    # ============================
    ENABLE_HIGH, ENABLE_MID, ENABLE_LOW = get_quality_flags()

    # ============================
    # 电视台部分
    # ============================
    if mode in ("all", "cctv", "satellite"):
        # 遍历白名单顺序列表
        for name in whitelist_order:   # 需要将 whitelist_order 作为参数传入
            # 模式过滤
            if mode == "cctv" and not name.startswith("CCTV"):
                continue
            if mode == "satellite" and name.startswith("CCTV"):
                continue

            # 如果该频道在 channels 中不存在，跳过
            if name not in channels:
                continue

            raw_urls = channels[name]
            # ============================
            # 排序逻辑
            # ============================
            urls = get_sorted_urls(
                name,
                raw_urls,
                ENABLE_HIGH,
                ENABLE_MID,
                ENABLE_LOW
            )

            # ============================
            # 找到最佳远程源（第一个 score>0 的远程源）
            # ============================
            best_remote = next(
                (u for u in urls if not is_local_source(u) and score_of(u) > 0),
                None
            )

            # ============================
            # 输出 M3U
            # ============================
            tvg_id = name
            logo = get_logo(name)
            group = get_group(name)

            # 根据 SET_GROUP 决定是否输出 group-title
            if SET_GROUP == "true":
                group_field = f'group-title="{group}" '
            else:
                group_field = ""

            for idx, url in enumerate(urls, start=1):
                norm_url = normalize_url(url)
                info = cache.get(norm_url, {})

                score = info.get("score", 0)
                w = info.get("width", 0)
                h = info.get("height", 0)
                delay = info.get("delay", 0)
                blur = info.get("blur", 0)

                res = f"{w}x{h}" if w and h else "N/A"

                best_flag = "yes" if url == best_remote else "no"
                local_flag = "yes" if is_local_source(url) else "no"

                extinf = (
                    f'#EXTINF:-1 '
                    f'tvg-id="{tvg_id}" '
                    f'tvg-name="{name}" '
                    f'tvg-logo="{logo}" '
                    f'{group_field}'
                    f'score="{score:.1f}" '
                    f'resolution="{res}" '
                    f'delay="{delay}" '
                    f'blur="{blur:.2f}" '
                    f'best="{best_flag}" '
                    f'rank="{idx}" '
                    f'local="{local_flag}",{name}'
                )

                lines.append(extinf)
                lines.append(url)

    return "\n".join(lines)

# ============================
# 上游源失败统计
# ============================

def update_upstream_fail(live_sources):
    global UPSTREAM_FAIL, UPSTREAM_BLOCKLIST

    today = time.strftime("%Y-%m-%d")
    updated = []

    for src, label in live_sources:
        total = SOURCE_TOTAL.get(src, 0)
        failed = SOURCE_FAIL.get(src, 0)

        if total == 0:
            failed = 1
            total = 1

        if failed == total:
            UPSTREAM_FAIL[src] += 1
            print(f"[source] {src} 全部失败（连续 {UPSTREAM_FAIL[src]} 次）")

            if UPSTREAM_FAIL[src] >= 10:
                remove_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
                UPSTREAM_BLOCKLIST[src] = {
                    "fail_time": today,
                    "remove_time": remove_date
                }
                print(f"[source] {src} 连续 10 次失败 → 已永久删除")
                continue

            updated.append((src, label))
        else:
            UPSTREAM_FAIL[src] = 0
            updated.append((src, label))

    with LIVE_URLS_FILE.open("w", encoding="utf-8") as f:
        for src, label in updated:
            if label:
                f.write(f"{src}${label}\n")
            else:
                f.write(f"{src}\n")

    save_json(UPSTREAM_BLOCKLIST_FILE, UPSTREAM_BLOCKLIST)
    save_json(UPSTREAM_FAIL_FILE, UPSTREAM_FAIL)

# ============================
# 主流程
# ============================

def load_live_urls():
    items = []
    with LIVE_URLS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "$" in line:
                url, name = line.split("$", 1)
            else:
                url, name = line, ""
            items.append((url.strip(), name.strip()))
    return items

def load_channel_whitelist():
    order = []
    s = set()
    if CHANNEL_LIST_FILE.exists():
        with CHANNEL_LIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name:
                    norm = normalize_name(name)
                    order.append(norm)
                    s.add(norm)
    return order, s

def load_blacklist():
    bl = []
    if BLACKLIST_FILE.exists():
        with BLACKLIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                key = line.strip()
                if key:
                    bl.append(key)
    return bl

def fetch_text(url, timeout=8, retries=3):
    print(f"[fetch] {url}")
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            return r.text
        except:
            if attempt == retries:
                print(f"  >>> Skip {url}")
                return ""

def main(mode):
    # 强制创建 output 目录，避免 GitHub Actions 报错
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    global WHITELIST_SET, WHITELIST_ORDER, BLACKLIST
    WHITELIST_ORDER, WHITELIST_SET = load_channel_whitelist()
    BLACKLIST = load_blacklist()

    # 加载上游源
    live_sources = load_live_urls()
    channels = defaultdict(list)

    # 解析所有上游源
    for src, label in live_sources:
        content = fetch_text(src)
        detect_and_parse(content, channels, source_url=src)

    # 解析本地 spider 源
    local_file = SOURCES_DIR / "local_spider.m3u"
    if local_file.exists():
        print("[local spider] 加载本地 spider 源")
        content = local_file.read_text(encoding="utf-8")
        detect_and_parse(content, channels, source_url="local_spider", from_local_spider=True)

    # ============================
    # 输出 TXT / M3U（永远生成文件）
    # ============================
    txt = build_output_txt(channels, mode, WHITELIST_ORDER)
    m3u = build_output_m3u(channels, mode, WHITELIST_ORDER)

    txt_path = OUTPUT_DIR / f"channels_{mode}.txt"
    m3u_path = OUTPUT_DIR / f"channels_{mode}.m3u"

    # 即使为空也写入，避免 mv output/* 报错
    txt_path.write_text(txt or "", encoding="utf-8")
    m3u_path.write_text(m3u or "", encoding="utf-8")

    # 保存测速缓存
    save_all(mode)

    # 保存黑名单过滤日志
    filtered_file = OUTPUT_DIR / f"filtered_{mode}.json"
    filtered_file.write_text(
        json.dumps(FILTERED_LOG, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # ============================
    # 上游源失败统计（核心逻辑）
    # ============================
    update_upstream_fail(live_sources)

    print("\n[done] 构建完成\n")

if __name__ == "__main__":
    global SORT_MODE,SET_GROUP
    # 类型
    mode = sys.argv[1] if len(sys.argv) > 1 else "cctv"
    # 排序
    raw_sort_mode = sys.argv[2] if len(sys.argv) > 2 else "本地源 → 高质量"
    SORT_MODE = SORT_MODE_MAP.get(raw_sort_mode, "local_high")
    # 分类
    SET_GROUP = sys.argv[3] if len(sys.argv) > 3 else "true"
    main(mode)
