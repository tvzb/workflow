#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高效版：直接从 GitHub Code Search 结果片段中提取直播源
搜索示例：更新 2026-09 CCTV-6,http
只提取 snippet 中的 频道名,http://... 行，不再下载完整文件
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CCTV_FILE = os.path.join(SCRIPT_DIR, "CCTV.txt")
WEISHI_FILE = os.path.join(SCRIPT_DIR, "weishi.txt")

SLEEP_BETWEEN_SEARCH = 7.0          # 基础休眠
MAX_PAGES_PER_QUERY = 4
PER_PAGE = 30
MAX_RETRIES_ON_429 = 6

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github.text-match+json",  # 关键：获取 text_matches 片段
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "IPTV-Collector/2.0"
}

# 只搜索带连字符的央视
CCTV_LOGICAL = [(f"CCTV-{i}", [f"CCTV-{i}"]) for i in range(1, 18)]
CCTV_LOGICAL.append(("CCTV-5+", ["CCTV-5+"]))

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

# 匹配 频道名,http... 的行
LINE_RE = re.compile(
    r'([^\n,，]{2,40})[,，]\s*(https?://[^\s\'"<>]+)',
    re.IGNORECASE
)

EXCLUDE_RE = re.compile(r'::|ipv6|ad\.|ads\.|banner|click|tracking|javascript:', re.I)


def get_recent_dates(days: int):
    today = datetime.utcnow() + timedelta(hours=8)
    dates = []
    for i in range(days):
        d = today - timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
        if i < 4:
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
    params = {"q": query, "per_page": PER_PAGE, "page": page}

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=25)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                wait_seconds = 65
                try:
                    msg = resp.json().get("message", "")
                    m = re.search(r'try again in ([\d.]+)s', msg)
                    if m:
                        wait_seconds = float(m.group(1)) + 3
                except:
                    pass
                print(f"  [429] 第{attempt}次，等待 {wait_seconds:.1f}s 后重试...")
                time.sleep(wait_seconds)
                continue

            print(f"  [Search Error] {resp.status_code}: {resp.text[:150]}")
            return None

        except Exception as e:
            print(f"  [Exception] {e}")
            time.sleep(8)
            continue

    print("  [Failed] 多次429后跳过")
    return None


def extract_from_text_matches(item: dict, preferred: str) -> list[tuple[str, str]]:
    """直接从搜索返回的 text_matches 片段中提取"""
    results = []
    text_matches = item.get("text_matches", [])
    for tm in text_matches:
        fragment = tm.get("fragment", "") or ""
        # 在片段中找所有 频道名,http 行
        for m in LINE_RE.finditer(fragment):
            name = m.group(1).strip()
            url = m.group(2).strip().rstrip(",;")
            if EXCLUDE_RE.search(url):
                continue
            if not url.startswith(("http://", "https://")) or len(url) < 15:
                continue
            # 简单归一
            if preferred.replace("-", "").lower() in name.replace("-", "").replace(" ", "").lower():
                name = preferred
            results.append((name, url))
    return results


def collect_logical_channel(standard_name: str, variants: list[str], dates: list[str], max_count: int):
    collected = set()          # 只存 url
    name_map = {}

    # 核心：搜索时直接带 ",http"，让 GitHub 帮我们过滤
    queries = []
    for d in dates[:6]:
        for v in variants:
            queries.append(f"更新 {d} {v},http")
            queries.append(f"更新{d} {v},http")
    for v in variants:
        queries.append(f"{v},http")

    print(f"\n===== 开始采集: {standard_name}（目标 {max_count} 个） =====")

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
            print(f"    第{page}页 → {len(items)} 个结果")

            for item in items:
                if len(collected) >= max_count:
                    break
                sources = extract_from_text_matches(item, standard_name)
                for name, url in sources:
                    if url in collected:
                        continue
                    collected.add(url)
                    name_map[url] = name
                    if len(collected) >= max_count:
                        print(f"    已达 {max_count} 个，停止")
                        break

            if len(items) < PER_PAGE // 2:
                break

    print(f"  完成 {standard_name}: {len(collected)} 个源")
    return collected, name_map


def write_file(filepath: str, lines: list[str]):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")
    print(f"  → 已写入: {filepath} （{len(lines)} 条）")


def main():
    if not TOKEN:
        print("错误：缺少 GITHUB_TOKEN")
        return

    dates = get_recent_dates(DAYS)
    print(f"优先日期: {dates[:4]} ...")
    print(f"央视上限: {MAX_CCTV} | 卫视上限: {MAX_WEISHI}")

    # 1. 央视
    cctv_results = []
    seen = set()
    for standard_name, variants in CCTV_LOGICAL:
        urls, name_map = collect_logical_channel(standard_name, variants, dates, MAX_CCTV)
        for url in sorted(urls):
            if url in seen:
                continue
            seen.add(url)
            cctv_results.append(f"{name_map.get(url, standard_name)},{url}")

    cctv_results.sort(key=lambda x: x.split(",", 1)[0])
    write_file(CCTV_FILE, cctv_results)
    print(f"\n【央视完成】{len(cctv_results)} 条 → CCTV.txt\n")

    # 2. 卫视
    weishi_results = []
    for ch in WEISHI_CHANNELS:
        urls, name_map = collect_logical_channel(ch, [ch], dates, MAX_WEISHI)
        for url in sorted(urls):
            if url in seen:
                continue
            seen.add(url)
            weishi_results.append(f"{name_map.get(url, ch)},{url}")

    weishi_results.sort(key=lambda x: x.split(",", 1)[0])
    write_file(WEISHI_FILE, weishi_results)
    print(f"\n【卫视完成】{len(weishi_results)} 条 → weishi.txt\n")

    print("========== 全部完成 ==========")
    print(f"CCTV.txt  : {len(cctv_results)}")
    print(f"weishi.txt: {len(weishi_results)}")


if __name__ == "__main__":
    main()
