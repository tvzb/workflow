#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Code Search 采集央视 + 省级卫视 IPv4 直播源
- 央视每个频道最多 MAX_CCTV 个源（默认30）
- 卫视每个频道最多 MAX_WEISHI 个源（默认15）
- 优先最近 N 天“更新 YYYY-MM-DD 频道名”
- 支持 TXT 行提取 + M3U 解析
- 输出纯 TXT：频道名,url（央视+卫视合并在一个文件）
- 结果文件放在与脚本同一目录
"""

import os
import re
import time
from datetime import datetime, timedelta

import requests

# ======================== 配置 ========================
TOKEN = os.getenv("GITHUB_TOKEN", "")
MAX_CCTV = int(os.getenv("MAX_CCTV", "30"))
MAX_WEISHI = int(os.getenv("MAX_WEISHI", "15"))
DAYS = int(os.getenv("DAYS", "15"))

# 结果文件放在脚本同一目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "cctv_weishi_ipv4.txt")

SLEEP_BETWEEN_SEARCH = 7.5
SLEEP_BETWEEN_FILE = 1.2
MAX_PAGES_PER_QUERY = 5
PER_PAGE = 30

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github.text-match+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "IPTV-Collector/1.0"
}

# 央视
CCTV_CHANNELS = []
for i in range(1, 18):
    CCTV_CHANNELS.append(f"CCTV-{i}")
    CCTV_CHANNELS.append(f"CCTV{i}")
CCTV_CHANNELS.extend(["CCTV-5+", "CCTV5+"])

# 省级卫视
WEISHI_CHANNELS = [
    "北京卫视", "天津卫视", "河北卫视", "山西卫视", "内蒙古卫视",
    "辽宁卫视", "吉林卫视", "黑龙江卫视",
    "东方卫视", "江苏卫视", "浙江卫视", "安徽卫视", "东南卫视",
    "江西卫视", "山东卫视", "河南卫视", "湖北卫视", "湖南卫视",
    "广东卫视", "广西卫视", "海南卫视",
    "重庆卫视", "四川卫视", "贵州卫视", "云南卫视",
    "西藏卫视", "陕西卫视", "甘肃卫视", "青海卫视", "宁夏卫视", "新疆卫视",
    "深圳卫视"
]

ALL_CHANNELS = CCTV_CHANNELS + WEISHI_CHANNELS

URL_RE = re.compile(
    r'https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?/[^\s\'"<>]+|'
    r'https?://[a-zA-Z0-9][-a-zA-Z0-9.]+\.[a-zA-Z]{2,}(?::\d+)?/[^\s\'"<>]+',
    re.IGNORECASE
)

EXCLUDE_RE = re.compile(r'::|ipv6|ad\.|ads\.|banner|click|tracking', re.I)

TXT_LINE_RE = re.compile(
    r'^[\s]*([^\n,，]{2,40})[,，]\s*(https?://[^\s]+)',
    re.MULTILINE
)

EXTINF_RE = re.compile(
    r'#EXTINF[^,]*,\s*(.+?)\s*\n\s*(https?://[^\s]+)',
    re.IGNORECASE | re.MULTILINE
)


def get_recent_dates(days: int):
    today = datetime.utcnow() + timedelta(hours=8)
    dates = []
    for i in range(days):
        d = today - timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
        dates.append(d.strftime("%Y-%m"))
    seen = set()
    result = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


def github_search_code(query: str, page: int = 1):
    url = "https://api.github.com/search/code"
    params = {
        "q": query,
        "per_page": PER_PAGE,
        "page": page
    }
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code == 403:
            print(f"  [RateLimit] 403，等待 60 秒...")
            time.sleep(60)
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [Search Error] {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()
    except Exception as e:
        print(f"  [Search Exception] {e}")
        return None


def get_raw_content(item: dict) -> str:
    repo = item.get("repository", {}).get("full_name", "")
    path = item.get("path", "")
    if repo and path:
        for branch in ["master", "main"]:
            try:
                r = requests.get(
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{path}",
                    headers={"User-Agent": HEADERS["User-Agent"]},
                    timeout=15
                )
                if r.status_code == 200 and len(r.text) > 50:
                    return r.text
            except:
                pass

    contents_url = item.get("url")
    if contents_url:
        try:
            r = requests.get(contents_url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("encoding") == "base64" and data.get("content"):
                    import base64
                    return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        except:
            pass
    return ""


def extract_sources(content: str, channel: str) -> list[tuple[str, str]]:
    results = []
    if not content:
        return results

    for m in TXT_LINE_RE.finditer(content):
        name = m.group(1).strip()
        url = m.group(2).strip().rstrip(",;")
        if EXCLUDE_RE.search(url) or not URL_RE.search(url):
            continue
        results.append((name, url))

    for m in EXTINF_RE.finditer(content):
        name = m.group(1).strip()
        url = m.group(2).strip()
        if EXCLUDE_RE.search(url) or not URL_RE.search(url):
            continue
        results.append((name, url))

    if not results:
        for m in URL_RE.finditer(content):
            url = m.group(0).rstrip(",;")
            if EXCLUDE_RE.search(url):
                continue
            if any(x in url.lower() for x in [".jpg", ".png", ".css", ".js", "github.com", "raw.githubusercontent"]):
                continue
            results.append((channel, url))

    return results


def normalize_channel_name(name: str, preferred: str) -> str:
    name = name.strip()
    mapping = {
        "cctv1": "CCTV-1", "cctv-1": "CCTV-1", "cctv1综合": "CCTV-1",
        "cctv5+": "CCTV-5+", "cctv-5+": "CCTV-5+",
    }
    key = name.replace(" ", "").replace("-", "").lower()
    if key in mapping:
        return mapping[key]
    if preferred.replace("-", "").lower() in key:
        return preferred
    return name


def collect_for_channel(channel: str, dates: list[str], max_count: int):
    collected = set()
    name_map = {}

    queries = []
    for d in dates:
        queries.append(f"更新 {d} {channel}")
        queries.append(f"更新{d} {channel}")
    queries.append(f"更新 {channel}")
    queries.append(f"{channel} http")

    print(f"\n===== 开始采集: {channel}（目标 {max_count} 个） =====")

    for q in queries:
        if len(collected) >= max_count:
            break
        print(f"  查询: {q}")
        for page in range(1, MAX_PAGES_PER_QUERY + 1):
            if len(collected) >= max_count:
                break
            data = github_search_code(q, page)
            time.sleep(SLEEP_BETWEEN_SEARCH)
            if not data or "items" not in data:
                break
            items = data["items"]
            if not items:
                break
            print(f"    第{page}页 → {len(items)} 个文件")

            for item in items:
                if len(collected) >= max_count:
                    break
                content = get_raw_content(item)
                time.sleep(SLEEP_BETWEEN_FILE)
                sources = extract_sources(content, channel)
                for name, url in sources:
                    if url in collected:
                        continue
                    if not url.startswith(("http://", "https://")) or len(url) < 15:
                        continue
                    norm_name = normalize_channel_name(name, channel)
                    collected.add(url)
                    name_map[url] = norm_name
                    if len(collected) >= max_count:
                        print(f"    已达 {max_count} 个，停止本频道")
                        break

            if len(items) < PER_PAGE // 2:
                break

    print(f"  完成 {channel}: 共 {len(collected)} 个源")
    return collected, name_map


def main():
    if not TOKEN:
        print("错误：缺少 GITHUB_TOKEN")
        return

    dates = get_recent_dates(DAYS)
    print(f"优先日期范围（最近 {DAYS} 天）: {dates[:5]} ...")
    print(f"央视上限: {MAX_CCTV} | 卫视上限: {MAX_WEISHI}")

    all_results = []
    seen_urls = set()

    # 先采央视
    for ch in CCTV_CHANNELS:
        urls, name_map = collect_for_channel(ch, dates, MAX_CCTV)
        for url in sorted(urls):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = name_map.get(url, ch)
            all_results.append(f"{name},{url}")

    # 再采卫视
    for ch in WEISHI_CHANNELS:
        urls, name_map = collect_for_channel(ch, dates, MAX_WEISHI)
        for url in sorted(urls):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = name_map.get(url, ch)
            all_results.append(f"{name},{url}")

    def sort_key(line):
        name = line.split(",", 1)[0]
        if name.upper().startswith("CCTV"):
            return (0, name, line)
        return (1, name, line)

    all_results.sort(key=sort_key)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_results))
        if all_results:
            f.write("\n")

    print(f"\n========== 全部完成 ==========")
    print(f"总计有效源: {len(all_results)}")
    print(f"输出文件: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
