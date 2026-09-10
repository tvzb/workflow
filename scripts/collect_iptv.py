#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Code Search 采集央视 + 省级卫视 IPv4 直播源
- 优先最近 N 天“更新 YYYY-MM-DD 频道名”
- 每个频道去重后最多保留 MAX_PER_CHANNEL 个源后停止
- 支持 TXT 行提取 + M3U 解析
- 输出纯 TXT：频道名,url
"""

import os
import re
import time
import json
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import quote_plus

import requests

# ======================== 配置 ========================
TOKEN = os.getenv("GITHUB_TOKEN", "")
MAX_PER_CHANNEL = int(os.getenv("MAX_PER_CHANNEL", "30"))
DAYS = int(os.getenv("DAYS", "15"))
OUTPUT_DIR = "output"
SLEEP_BETWEEN_SEARCH = 7.5      # 代码搜索限速约10次/分钟，留余量
SLEEP_BETWEEN_FILE = 1.2
MAX_PAGES_PER_QUERY = 5         # 单个查询最多翻几页
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

# 省级卫视（主流）
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

# 匹配直播地址的正则（IPv4优先，排除明显IPv6）
URL_RE = re.compile(
    r'https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?/[^\s\'"<>]+|'
    r'https?://[a-zA-Z0-9][-a-zA-Z0-9.]+\.[a-zA-Z]{2,}(?::\d+)?/[^\s\'"<>]+',
    re.IGNORECASE
)

# 排除明显非直播或IPv6
EXCLUDE_RE = re.compile(r'::|ipv6|ad\.|ads\.|banner|click|tracking', re.I)

# TXT 格式匹配：频道名,http...
TXT_LINE_RE = re.compile(
    r'^[\s]*([^\n,，]{2,40})[,，]\s*(https?://[^\s]+)',
    re.MULTILINE
)

# M3U 简单解析
EXTINF_RE = re.compile(
    r'#EXTINF[^,]*,\s*(.+?)\s*\n\s*(https?://[^\s]+)',
    re.IGNORECASE | re.MULTILINE
)


def get_recent_dates(days: int):
    """生成最近 days 天的日期字符串列表（从新到旧）"""
    today = datetime.utcnow() + timedelta(hours=8)  # 北京时间近似
    dates = []
    for i in range(days):
        d = today - timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
        dates.append(d.strftime("%Y-%m"))  # 也带整月
    # 去重保持顺序
    seen = set()
    result = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


def github_search_code(query: str, page: int = 1):
    """执行一次代码搜索"""
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
    """获取文件原始内容（优先 raw_url，其次 contents API）"""
    # 尝试构造 raw 地址
    repo = item.get("repository", {}).get("full_name", "")
    path = item.get("path", "")
    if repo and path:
        raw_url = f"https://raw.githubusercontent.com/{repo}/master/{path}"
        # 很多仓库默认分支是 main
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

    # 退而求其次用 contents API（base64）
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
    """从文件内容提取 (频道名, url) 列表，优先匹配当前频道"""
    results = []
    if not content:
        return results

    # 1. 先尝试 TXT 格式
    for m in TXT_LINE_RE.finditer(content):
        name = m.group(1).strip()
        url = m.group(2).strip().rstrip(",;")
        if EXCLUDE_RE.search(url):
            continue
        if not URL_RE.search(url):
            continue
        # 简单归一：去掉多余空格、统一大小写感觉
        if channel.replace("-", "").lower() in name.replace("-", "").replace(" ", "").lower() or \
           name.replace("-", "").replace(" ", "").lower() in channel.replace("-", "").lower():
            results.append((name, url))
        else:
            # 也保留其他看起来像直播源的行（后面会按频道过滤）
            results.append((name, url))

    # 2. 再解析 M3U
    for m in EXTINF_RE.finditer(content):
        name = m.group(1).strip()
        url = m.group(2).strip()
        if EXCLUDE_RE.search(url) or not URL_RE.search(url):
            continue
        results.append((name, url))

    # 3. 兜底：直接找所有疑似直播 URL，用当前频道名
    if not results:
        for m in URL_RE.finditer(content):
            url = m.group(0).rstrip(",;")
            if EXCLUDE_RE.search(url):
                continue
            # 简单过滤明显非直播
            if any(x in url.lower() for x in [".jpg", ".png", ".css", ".js", "github.com", "raw.githubusercontent"]):
                continue
            results.append((channel, url))

    return results


def normalize_channel_name(name: str, preferred: str) -> str:
    """尽量把名字归一到标准频道名"""
    name = name.strip()
    # 简单映射
    mapping = {
        "cctv1": "CCTV-1", "cctv-1": "CCTV-1", "cctv1综合": "CCTV-1",
        "cctv5+": "CCTV-5+", "cctv-5+": "CCTV-5+",
    }
    key = name.replace(" ", "").replace("-", "").lower()
    if key in mapping:
        return mapping[key]
    # 如果包含 preferred，直接用 preferred
    if preferred.replace("-", "").lower() in key:
        return preferred
    return name


def collect_for_channel(channel: str, dates: list[str]) -> set[str]:
    """为一个频道采集，返回去重后的 url 集合（最多 MAX_PER_CHANNEL）"""
    collected = set()          # 存 url
    name_map = {}              # url -> 最终频道名

    # 构建查询优先级：带具体日期 > 带月份 > 不带日期
    queries = []
    for d in dates:
        queries.append(f"更新 {d} {channel}")
        queries.append(f"更新{d} {channel}")
    queries.append(f"更新 {channel}")
    queries.append(f"{channel} http")          # 兜底

    print(f"\n===== 开始采集: {channel} =====")

    for q in queries:
        if len(collected) >= MAX_PER_CHANNEL:
            break
        print(f"  查询: {q}")
        for page in range(1, MAX_PAGES_PER_QUERY + 1):
            if len(collected) >= MAX_PER_CHANNEL:
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
                if len(collected) >= MAX_PER_CHANNEL:
                    break
                content = get_raw_content(item)
                time.sleep(SLEEP_BETWEEN_FILE)
                sources = extract_sources(content, channel)
                for name, url in sources:
                    if url in collected:
                        continue
                    # 简单有效性：必须是 http/https 且看起来像路径
                    if not url.startswith(("http://", "https://")):
                        continue
                    if len(url) < 15:
                        continue
                    norm_name = normalize_channel_name(name, channel)
                    collected.add(url)
                    name_map[url] = norm_name
                    if len(collected) >= MAX_PER_CHANNEL:
                        print(f"    已达 {MAX_PER_CHANNEL} 个，停止本频道")
                        break

            # 如果这一页结果很少，基本可以认为后面没有了
            if len(items) < PER_PAGE // 2:
                break

    print(f"  完成 {channel}: 共 {len(collected)} 个源")
    return collected, name_map


def main():
    if not TOKEN:
        print("错误：缺少 GITHUB_TOKEN")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    dates = get_recent_dates(DAYS)
    print(f"优先日期范围（最近 {DAYS} 天）: {dates[:5]} ...")

    all_results = []          # list of "频道名,url"
    seen_urls = set()

    for ch in ALL_CHANNELS:
        urls, name_map = collect_for_channel(ch, dates)
        for url in sorted(urls):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = name_map.get(url, ch)
            all_results.append(f"{name},{url}")

    # 最终排序：先 CCTV，再卫视，同名前按 url
    def sort_key(line):
        name = line.split(",", 1)[0]
        if name.upper().startswith("CCTV"):
            return (0, name, line)
        return (1, name, line)

    all_results.sort(key=sort_key)

    # 写文件
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_file = os.path.join(OUTPUT_DIR, f"cctv_weishi_ipv4_{ts}.txt")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(all_results))
        if all_results:
            f.write("\n")

    print(f"\n========== 全部完成 ==========")
    print(f"总计有效源: {len(all_results)}")
    print(f"输出文件: {out_file}")
    # 同时写一个 latest 方便下载
    latest = os.path.join(OUTPUT_DIR, "cctv_weishi_ipv4_latest.txt")
    with open(latest, "w", encoding="utf-8") as f:
        f.write("\n".join(all_results))
        if all_results:
            f.write("\n")
    print(f"同时生成: {latest}")


if __name__ == "__main__":
    main()
