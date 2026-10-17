# -*- coding: utf-8 -*-
"""
核心执行引擎：支持30天时间窗搜索、活跃仓库自动发现、历史源继承、同IP防雪崩过滤、异步测速优选与live.txt导出
"""
import os
import re
import time
import socket
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, List, Set, Tuple

import aiohttp
import requests

from config import (
    CCTV_CHANNELS,
    SATELLITE_CHANNELS,
    ALL_TARGET_CHANNELS,
    normalize_channel_name,
    MAX_STREAMS_PER_CHANNEL,
    CANDIDATE_POOL_LIMIT,
    MAX_SAME_IP_PER_CHANNEL,
    SEARCH_WINDOW_DAYS,
    REQUEST_TIMEOUT,
    SPEED_TEST_TIMEOUT,
    SPEED_TEST_MAX_BYTES,
    MAX_CONCURRENT_TASKS,
    DEFAULT_OUTPUT_FILE,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


# ============================
# 1. 历史直播源继承与防丢失机制
# ============================
def load_previous_live_sources(file_path: str = DEFAULT_OUTPUT_FILE) -> Dict[str, List[str]]:
    """读取上次保留的 live.txt，确保运行后保留旧源，以防搜索受阻或网络波动导致源全部丢失"""
    previous_sources: Dict[str, List[str]] = {ch: [] for ch in ALL_TARGET_CHANNELS}
    if not os.path.exists(file_path):
        print(f"[*] 未检测到历史文件 {file_path}，将进行全新拉取。")
        return previous_sources

    print(f"[*] 成功载入上次保留的 {file_path}，提取历史有效源作为保底候选...")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ",#genre#" in line:
                    continue
                if "," in line:
                    parts = line.split(",", 1)
                    ch_name = parts[0].strip()
                    url = parts[1].strip()
                    std_name = normalize_channel_name(ch_name)
                    if std_name in previous_sources and url.startswith("http"):
                        if url not in previous_sources[std_name]:
                            previous_sources[std_name].append(url)
    except Exception as e:
        print(f"[!] 读取历史文件异常: {e}")
    
    total_loaded = sum(len(v) for v in previous_sources.values())
    print(f"[+] 历史记录共加载 {total_loaded} 个保底直播源。")
    return previous_sources


# ============================
# 2. IPv4 过滤与 DNS 探测
# ============================
def is_valid_ipv4_url(url: str) -> bool:
    """严格过滤 IPv6 直连与纯 IPv6 域名，仅保留 IPv4 协议地址"""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False

        if ":" in hostname or hostname.startswith("["):
            return False

        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', hostname):
            parts = [int(p) for p in hostname.split('.')]
            return all(0 <= p <= 255 for p in parts)

        addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET)
        if addr_info:
            return True
    except Exception:
        return False
    return False


def get_host_identifier(url: str) -> str:
    """提取 URL 中的 Host/IP，用于同源防雪崩多样性检查"""
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""


def fetch_content_from_url(url: str, token: str = "") -> str:
    """辅助拉取网页或 Raw 文本内容"""
    headers = HEADERS.copy()
    if token and "github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return ""


# ============================
# 3. 优化采集策略（仓库动态发现 + 30天时间窗代码搜索 + 权威源融合）
# ============================
def search_github_candidates(token: str, candidate_pools: Dict[str, Set[str]]):
    """
    组合采集策略：
    1. 时间窗口扩大至 30 天 (1个月)；
    2. 检索 30 天内活跃推送的 IPTV 优质仓库；
    3. 结合 Code Search 接口搜索最新代码文件；
    4. 融合日更白名单聚合源保底。
    """
    now = datetime.now()
    since_date = (now - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    current_ym = now.strftime("%Y-%m")
    prev_ym = (now - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m")

    print(f"[*] 启动全局检索（搜索范围扩大至过去 {SEARCH_WINDOW_DAYS} 天，从 {since_date} 至今）...")

    headers = HEADERS.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github.v3+json"

    scraped_files: Set[str] = set()

    # 策略 A：直接搜索近 30 天内活跃更新的 IPTV 仓库 (比逐行搜索更海量且不触发429)
    repo_search_api = f"https://api.github.com/search/repositories?q=iptv+m3u8+pushed:>{since_date}&sort=updated&order=desc&per_page=10"
    try:
        r = requests.get(repo_search_api, headers=headers, timeout=12)
        if r.status_code == 200:
            repos = r.json().get("items", [])
            print(f"[+] 发现近 30 天内活跃更新的 {len(repos)} 个 IPTV 仓库，探测其直播列表文件...")
            common_file_paths = [
                "live.txt", "tv.txt", "ipv4.m3u", "IPTV.m3u", "output/user_result.txt", "output/result.txt"
            ]
            for repo in repos:
                default_branch = repo.get("default_branch", "main")
                full_name = repo.get("full_name", "")
                for fp in common_file_paths:
                    raw_url = f"https://raw.githubusercontent.com/{full_name}/{default_branch}/{fp}"
                    scraped_files.add(raw_url)
    except Exception as e:
        print(f"[!] 检索活跃仓库异常: {e}")

    # 策略 B：代码精准搜索（覆盖当月与上月关键字）
    code_queries = [
        f"更新 {current_ym} CCTV-1",
        f"更新 {current_ym} CCTV1",
        f"更新 {current_ym} 卫视",
    ]
    if prev_ym != current_ym:
        code_queries.append(f"更新 {prev_ym} CCTV-1")

    for q in code_queries:
        search_api = f"https://api.github.com/search/code?q={urllib.parse.quote(q)}&sort=indexed&order=desc&per_page=10"
        try:
            r = requests.get(search_api, headers=headers, timeout=12)
            if r.status_code == 200:
                data = r.json()
                for item in data.get("items", [])[:10]:
                    html_url = item.get("html_url", "")
                    if html_url and "blob" in html_url:
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                        scraped_files.add(raw_url)
            elif r.status_code in (403, 429):
                print(f"[!] GitHub Code API 访问达到限额，采用指数等待。")
                time.sleep(2)
        except Exception as e:
            print(f"[!] 检索异常 ({q}): {e}")
        time.sleep(1.5)

    # 策略 C：融合每日高频同步的权威直链源
    fallback_raw_sources = [
        "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/output/result.txt",
        "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv4.m3u",
        "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
    ]
    scraped_files.update(fallback_raw_sources)

    print(f"[+] 汇集共 {len(scraped_files)} 个源地址，解析频道数据并实施 25 个候选上限控制...")
    for file_url in scraped_files:
        content = fetch_content_from_url(file_url, token)
        if not content:
            continue
        parse_stream_content(content, candidate_pools)


def parse_stream_content(content: str, candidate_pools: Dict[str, Set[str]]):
    """解析 TXT 或 M3U 内容，匹配归一化频道名并注入候选池（控制单频道上限为 25 个）"""
    lines = content.splitlines()
    
    # TXT 场景
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line and "http" in line:
            parts = line.split(",", 1)
            std_name = normalize_channel_name(parts[0].strip())
            raw_url = parts[1].strip()
            if std_name and std_name in candidate_pools:
                if len(candidate_pools[std_name]) < CANDIDATE_POOL_LIMIT:
                    if is_valid_ipv4_url(raw_url):
                        candidate_pools[std_name].add(raw_url)

    # M3U 场景
    current_ch = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            match = re.search(r'tvg-name="([^"]+)"', line) or re.search(r',([^,]+)$', line)
            if match:
                current_ch = normalize_channel_name(match.group(1).strip())
        elif line.startswith("http") and current_ch:
            if current_ch in candidate_pools and len(candidate_pools[current_ch]) < CANDIDATE_POOL_LIMIT:
                if is_valid_ipv4_url(line):
                    candidate_pools[current_ch].add(line)
            current_ch = None


# ============================
# 4. 异步连通性测试与切片测速
# ============================
async def test_and_speedtest_stream(session: aiohttp.ClientSession, url: str) -> Tuple[bool, float, float]:
    """连通性校验与媒体切片测速"""
    try:
        t_start = time.perf_counter()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), allow_redirects=True) as resp:
            if resp.status != 200:
                return False, 0.0, 9999.0
            
            ttfb = (time.perf_counter() - t_start) * 1000
            content_sample = await resp.content.read(2048)
            text_sample = content_sample.decode('utf-8', errors='ignore')

            if "#EXTM3U" not in text_sample and "#EXTINF" not in text_sample:
                return False, 0.0, 9999.0

            ts_url = None
            for line in text_sample.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    ts_url = urllib.parse.urljoin(url, line)
                    break

            if not ts_url:
                return True, 100.0, ttfb

        # 切片下载测速
        t_speed_start = time.perf_counter()
        bytes_received = 0
        async with session.get(ts_url, timeout=aiohttp.ClientTimeout(total=SPEED_TEST_TIMEOUT), allow_redirects=True) as ts_resp:
            if ts_resp.status != 200:
                return True, 100.0, ttfb

            while True:
                chunk = await ts_resp.content.read(64 * 1024)
                if not chunk:
                    break
                bytes_received += len(chunk)
                if bytes_received >= SPEED_TEST_MAX_BYTES:
                    break
                if time.perf_counter() - t_speed_start >= SPEED_TEST_TIMEOUT:
                    break

        elapsed = time.perf_counter() - t_speed_start
        if elapsed <= 0:
            elapsed = 0.001
        speed_kbs = (bytes_received / 1024.0) / elapsed
        return True, round(speed_kbs, 2), round(ttfb, 2)

    except Exception:
        return False, 0.0, 9999.0


async def benchmark_channel_streams(channel_name: str, urls: Set[str], semaphore: asyncio.Semaphore) -> List[Tuple[float, str]]:
    """并发测速单个频道的候选流，按速度降序排列，并应用同IP防雪崩过滤"""
    ranked_streams: List[Tuple[float, str]] = []
    
    conn = aiohttp.TCPConnector(ssl=False, limit=0)
    async with aiohttp.ClientSession(headers=HEADERS, connector=conn) as session:
        async def sem_task(url: str):
            async with semaphore:
                valid, speed, _ = await test_and_speedtest_stream(session, url)
                if valid and speed > 0:
                    ranked_streams.append((speed, url))

        tasks = [sem_task(u) for u in urls]
        await asyncio.gather(*tasks, return_exceptions=True)

    # 排序：速度由高到低
    ranked_streams.sort(key=lambda x: x[0], reverse=True)

    # 【防雪崩过滤】：同一个 Host/IP 最多保留指定数量（3个），防止单服务器宕机造成频道瘫痪
    filtered_by_ip: List[Tuple[float, str]] = []
    ip_counter: Dict[str, int] = {}
    for speed, url in ranked_streams:
        host = get_host_identifier(url)
        count = ip_counter.get(host, 0)
        if count < MAX_SAME_IP_PER_CHANNEL:
            filtered_by_ip.append((speed, url))
            ip_counter[host] = count + 1
        if len(filtered_by_ip) >= MAX_STREAMS_PER_CHANNEL:
            break

    # 若过滤后未满10个且原本有充裕源，则继续补齐至10个
    if len(filtered_by_ip) < MAX_STREAMS_PER_CHANNEL and len(ranked_streams) > len(filtered_by_ip):
        existing_urls = {u for _, u in filtered_by_ip}
        for speed, url in ranked_streams:
            if url not in existing_urls:
                filtered_by_ip.append((speed, url))
            if len(filtered_by_ip) >= MAX_STREAMS_PER_CHANNEL:
                break

    return filtered_by_ip


# ============================
# 5. 主流程协调与文件导出
# ============================
async def run_pipeline():
    print("=" * 60)
    print("      IPTV 直播源全自动化测速、优选与构建工作流 (优化增强版)")
    print("=" * 60)
    
    github_token = os.environ.get("GITHUB_TOKEN", "")

    # 1. 继承历史源
    previous_sources = load_previous_live_sources(DEFAULT_OUTPUT_FILE)
    candidate_pools: Dict[str, Set[str]] = {ch: set(previous_sources.get(ch, [])) for ch in ALL_TARGET_CHANNELS}

    # 2. 扩大时间窗与动态搜索
    search_github_candidates(github_token, candidate_pools)

    total_candidates = sum(len(v) for v in candidate_pools.values())
    print(f"\n[+] 候选池汇聚完毕，所有频道待测源总数: {total_candidates} 个（单频道上限 {CANDIDATE_POOL_LIMIT} 个）")

    # 3. 执行并发测速与优选
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    final_results: Dict[str, List[str]] = {}

    print("\n[*] 启动连通性探测与 TS 切片并发测速...")
    for idx, channel in enumerate(ALL_TARGET_CHANNELS, 1):
        urls = candidate_pools.get(channel, set())
        if not urls:
            print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 无候选源")
            final_results[channel] = previous_sources.get(channel, [])[:MAX_STREAMS_PER_CHANNEL]
            continue

        ranked = await benchmark_channel_streams(channel, urls, semaphore)
        top_fastest = [url for speed, url in ranked[:MAX_STREAMS_PER_CHANNEL]]

        # 防错保底
        if not top_fastest and previous_sources.get(channel):
            print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 本次测速全失效，启动防清空机制回退旧源")
            top_fastest = previous_sources[channel][:MAX_STREAMS_PER_CHANNEL]
        else:
            best_speed = ranked[0][0] if ranked else 0
            print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 优选出 {len(top_fastest)} 个有效源 (最高速度: {best_speed} KB/s)")

        final_results[channel] = top_fastest

    # 4. 导出为规范的 live.txt 格式
    print(f"\n[*] 正在生成最终输出文件: {DEFAULT_OUTPUT_FILE}...")
    with open(DEFAULT_OUTPUT_FILE, "w", encoding="utf-8") as f:
        # 分组一：央视频道
        f.write("央视频道,#genre#\n")
        for ch in CCTV_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{url}\n")
        
        # 分组二：卫视频道
        f.write("卫视频道,#genre#\n")
        for ch in SATELLITE_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{url}\n")

    total_exported = sum(len(v) for v in final_results.values())
    print("=" * 60)
    print(f"[√] 任务完成！已成功导出 {total_exported} 条高速直播源至 {DEFAULT_OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_pipeline())
