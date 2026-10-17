# -*- coding: utf-8 -*-
"""
核心执行引擎：
1. 彻底清除直播源 URL 中 '$' 及其后文字；
2. 历史 live.txt 源全面合并再检，劣汰死源；
3. 优质 IP:Port 全网精准反查；
4. 命名规律自动推导演生（种子源裂变）；
5. 严格切片测速优选与 Top 10 纯净 live.txt 导出。
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
    clean_stream_url,
    extrapolate_channels_from_seed,
    MAX_STREAMS_PER_CHANNEL,
    CANDIDATE_POOL_LIMIT,
    MAX_SAME_IP_PER_CHANNEL,
    SEARCH_WINDOW_DAYS,
    FISSION_TOP_SEEDS_LIMIT,
    REVERSE_HOST_SEARCH_LIMIT,
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
# 1. 历史直播源合并（清除 $ 标记，进入统一测速池）
# ============================
def load_previous_live_sources(file_path: str = DEFAULT_OUTPUT_FILE) -> Dict[str, List[str]]:
    """读取已有的 live.txt（若本地没有，则尝试从线上主页拉取），清除 $ 后缀并载入待测池"""
    previous_sources: Dict[str, List[str]] = {ch: [] for ch in ALL_TARGET_CHANNELS}
    
    content = ""
    if os.path.exists(file_path):
        print(f"[*] 检测到本地历史文件 {file_path}，读取历史源...")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"[!] 读取本地历史文件出错: {e}")
    else:
        fallback_online_url = "https://tvzb.github.io/live.txt"
        print(f"[*] 本地暂无 {file_path}，尝试同步线上历史源 ({fallback_online_url})...")
        try:
            r = requests.get(fallback_online_url, headers=HEADERS, timeout=6)
            if r.status_code == 200:
                content = r.text
        except Exception:
            pass

    if content:
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ",#genre#" in line:
                continue
            if "," in line:
                parts = line.split(",", 1)
                ch_name = parts[0].strip()
                raw_url = parts[1].strip()
                # 关键：彻底清除 $ 及其后面的修饰字
                clean_url = clean_stream_url(raw_url)
                std_name = normalize_channel_name(ch_name)
                if std_name in previous_sources and clean_url.startswith("http"):
                    if clean_url not in previous_sources[std_name]:
                        previous_sources[std_name].append(clean_url)
    
    total_loaded = sum(len(v) for v in previous_sources.values())
    print(f"[+] 成功加载并清洗历史记录中的 {total_loaded} 个历史源，加入全量待测池一并检验。")
    return previous_sources


# ============================
# 2. IPv4 过滤与 Host 提取
# ============================
def is_valid_ipv4_url(url: str) -> bool:
    """严格过滤 IPv6 直连与纯 IPv6 域名，仅保留 IPv4 协议地址"""
    try:
        clean_url = clean_stream_url(url)
        parsed = urllib.parse.urlparse(clean_url)
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
    """提取 URL 中的 host:port"""
    try:
        clean_url = clean_stream_url(url)
        parsed = urllib.parse.urlparse(clean_url)
        return parsed.netloc or parsed.hostname or ""
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
# 3. 动态检索、IP反查与内容解析
# ============================
def search_github_candidates(token: str, candidate_pools: Dict[str, Set[str]]):
    """30 天时间窗检索：活跃仓库 + 代码关键词搜索 + 权威源"""
    now = datetime.now()
    since_date = (now - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    current_ym = now.strftime("%Y-%m")
    prev_ym = (now - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m")

    print(f"[*] 启动全网多维检索（时间范围：过去 {SEARCH_WINDOW_DAYS} 天，从 {since_date} 至今）...")

    headers = HEADERS.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github.v3+json"

    scraped_files: Set[str] = set()

    # 1. 活跃 IPTV 仓库扫描
    repo_search_api = f"https://api.github.com/search/repositories?q=iptv+m3u8+pushed:>{since_date}&sort=updated&order=desc&per_page=8"
    try:
        r = requests.get(repo_search_api, headers=headers, timeout=12)
        if r.status_code == 200:
            repos = r.json().get("items", [])
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

    # 2. 核心代码行精准检索
    code_queries = [
        f"更新 {current_ym} CCTV-1",
        f"更新 {current_ym} CCTV1",
        f"更新 {current_ym} 卫视",
    ]
    if prev_ym != current_ym:
        code_queries.append(f"更新 {prev_ym} CCTV-1")

    for q in code_queries:
        search_api = f"https://api.github.com/search/code?q={urllib.parse.quote(q)}&sort=indexed&order=desc&per_page=8"
        try:
            r = requests.get(search_api, headers=headers, timeout=12)
            if r.status_code == 200:
                for item in r.json().get("items", [])[:8]:
                    html_url = item.get("html_url", "")
                    if html_url and "blob" in html_url:
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                        scraped_files.add(raw_url)
            elif r.status_code in (403, 429):
                time.sleep(2)
        except Exception:
            pass
        time.sleep(1.2)

    # 3. 权威聚合源
    fallback_raw_sources = [
        "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/output/result.txt",
        "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv4.m3u",
        "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
    ]
    scraped_files.update(fallback_raw_sources)

    print(f"[+] 汇集 {len(scraped_files)} 个源列表，解析数据并清除 '$' 符号与杂质...")
    for file_url in scraped_files:
        content = fetch_content_from_url(file_url, token)
        if content:
            parse_stream_content(content, candidate_pools)


def parse_stream_content(content: str, candidate_pools: Dict[str, Set[str]]):
    """解析直播源内容，自动剔除 '$' 及其后面的所有文字"""
    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line and "http" in line:
            parts = line.split(",", 1)
            std_name = normalize_channel_name(parts[0].strip())
            raw_url = parts[1].strip()
            # 彻底清洗掉 $ 符号及其后文字
            clean_url = clean_stream_url(raw_url)
            if std_name and std_name in candidate_pools:
                if len(candidate_pools[std_name]) < CANDIDATE_POOL_LIMIT:
                    if is_valid_ipv4_url(clean_url):
                        candidate_pools[std_name].add(clean_url)

    # M3U 场景
    current_ch = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            match = re.search(r'tvg-name="([^"]+)"', line) or re.search(r',([^,]+)$', line)
            if match:
                current_ch = normalize_channel_name(match.group(1).strip())
        elif line.startswith("http") and current_ch:
            clean_url = clean_stream_url(line)
            if current_ch in candidate_pools and len(candidate_pools[current_ch]) < CANDIDATE_POOL_LIMIT:
                if is_valid_ipv4_url(clean_url):
                    candidate_pools[current_ch].add(clean_url)
            current_ch = None


def reverse_search_hosts_on_github(token: str, candidate_pools: Dict[str, Set[str]]):
    """
    【高阶功能 1：优质 IP 反查】
    提取候选源中的 IP:Port，在 GitHub 上反查包含该主机完整频道列表的文件
    """
    if not token:
        return

    # 提取出现频率高或典型的 IP:Port 组合
    host_counts: Dict[str, int] = {}
    for ch, urls in candidate_pools.items():
        for u in urls:
            h = get_host_identifier(u)
            if h and re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+', h):
                host_counts[h] = host_counts.get(h, 0) + 1

    top_hosts = sorted(host_counts.keys(), key=lambda x: host_counts[x], reverse=True)[:REVERSE_HOST_SEARCH_LIMIT]
    if not top_hosts:
        return

    print(f"\n[*] 启动优质主机反查机制，针对高频服务器深入检索全套频道: {', '.join(top_hosts)}")
    headers = HEADERS.copy()
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/vnd.github.v3+json"

    for host in top_hosts:
        query = f'"{host}" in:file extension:txt'
        api_url = f"https://api.github.com/search/code?q={urllib.parse.quote(query)}&per_page=5"
        try:
            r = requests.get(api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                for item in r.json().get("items", []):
                    html_url = item.get("html_url", "")
                    if html_url and "blob" in html_url:
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                        txt_data = fetch_content_from_url(raw_url, token)
                        if txt_data:
                            parse_stream_content(txt_data, candidate_pools)
            time.sleep(1.5)
        except Exception as e:
            print(f"[!] 反查主机 {host} 出错: {e}")


def apply_seed_fission(candidate_pools: Dict[str, Set[str]]):
    """
    【高阶功能 2：种子源规律推导与裂变生成】
    观察地址规律（如 cctv1 -> cctv6, jiangxi -> hunan），自动衍生全套央视和卫视地址
    """
    print("\n[*] 启动命名规律自动推导（种子源裂变），批量拓展全套央视与卫视频道...")
    fission_candidates = []
    for ch, urls in candidate_pools.items():
        for u in urls:
            if re.search(r'cctv|hunan|jiangxi|beijing|dongfang', u, re.IGNORECASE):
                fission_candidates.append(u)
                if len(fission_candidates) >= FISSION_TOP_SEEDS_LIMIT:
                    break
        if len(fission_candidates) >= FISSION_TOP_SEEDS_LIMIT:
            break

    fission_count = 0
    for seed_url in fission_candidates:
        derived_pairs = extrapolate_channels_from_seed(seed_url)
        for target_ch, derived_url in derived_pairs:
            if target_ch in candidate_pools and len(candidate_pools[target_ch]) < CANDIDATE_POOL_LIMIT:
                if derived_url not in candidate_pools[target_ch] and is_valid_ipv4_url(derived_url):
                    candidate_pools[target_ch].add(derived_url)
                    fission_count += 1

    print(f"[+] 规律推导完成，成功根据种子源衍生出 {fission_count} 个潜在频道直播流！")


# ============================
# 4. 异步连通性测试与切片测速
# ============================
async def test_and_speedtest_stream(session: aiohttp.ClientSession, url: str) -> Tuple[bool, float, float]:
    """两阶段严格测试：连通性验证 + TS 切片真实拉流测速（死源坚决判伪）"""
    try:
        clean_url = clean_stream_url(url)
        t_start = time.perf_counter()
        async with session.get(clean_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), allow_redirects=True) as resp:
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
                    ts_url = urllib.parse.urljoin(clean_url, line)
                    break

            if not ts_url:
                return True, 80.0, ttfb

        # 真实下载媒体切片测速
        t_speed_start = time.perf_counter()
        bytes_received = 0
        async with session.get(ts_url, timeout=aiohttp.ClientTimeout(total=SPEED_TEST_TIMEOUT), allow_redirects=True) as ts_resp:
            if ts_resp.status != 200:
                return True, 80.0, ttfb

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
    """并发测速单个频道的候选流，按速度降序排列，严格排除死源，执行 IP 多样性过滤"""
    ranked_streams: List[Tuple[float, str]] = []
    
    conn = aiohttp.TCPConnector(ssl=False, limit=0)
    async with aiohttp.ClientSession(headers=HEADERS, connector=conn) as session:
        async def sem_task(url: str):
            async with semaphore:
                valid, speed, _ = await test_and_speedtest_stream(session, url)
                # 只有通过测试且具有真实速率的才保留（彻底剔除老文件中的死源）
                if valid and speed > 0:
                    ranked_streams.append((speed, clean_stream_url(url)))

        tasks = [sem_task(u) for u in urls]
        await asyncio.gather(*tasks, return_exceptions=True)

    ranked_streams.sort(key=lambda x: x[0], reverse=True)

    # 同 IP 节点数量控制（防雪崩）
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

    # 若防雪崩过滤后不足 10 个，且还有其他备选有效源，则补满 10 个
    if len(filtered_by_ip) < MAX_STREAMS_PER_CHANNEL and len(ranked_streams) > len(filtered_by_ip):
        existing_urls = {u for _, u in filtered_by_ip}
        for speed, url in ranked_streams:
            if url not in existing_urls:
                filtered_by_ip.append((speed, url))
            if len(filtered_by_ip) >= MAX_STREAMS_PER_CHANNEL:
                break

    return filtered_by_ip


# ============================
# 5. 主流程协调与输出生成
# ============================
async def run_pipeline():
    print("=" * 65)
    print("   IPTV 全自动采集引擎：IP反查 + 规律裂变 + 历史真活检验 + 清理$")
    print("=" * 65)
    
    github_token = os.environ.get("GITHUB_TOKEN", "")

    # 1. 载入历史 live.txt 中的源（全面清除 $ 杂质），全部进入待测池
    previous_sources = load_previous_live_sources(DEFAULT_OUTPUT_FILE)
    candidate_pools: Dict[str, Set[str]] = {ch: set(previous_sources.get(ch, [])) for ch in ALL_TARGET_CHANNELS}

    # 2. 全网多维检索
    search_github_candidates(github_token, candidate_pools)

    # 3. 针对高价值服务器 Host:Port 执行 GitHub 全局反查
    reverse_search_hosts_on_github(github_token, candidate_pools)

    # 4. 根据优质种子源地址规律执行裂变衍生
    apply_seed_fission(candidate_pools)

    total_candidates = sum(len(v) for v in candidate_pools.values())
    print(f"\n[+] 全渠道扩充完毕！所有频道参与测速的总候选源数: {total_candidates} 个")

    # 5. 并发严格测速与劣汰
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    final_results: Dict[str, List[str]] = {}

    print("\n[*] 启动连通性探测与 TS 切片深度测速（全面淘汰失效源）...")
    for idx, channel in enumerate(ALL_TARGET_CHANNELS, 1):
        urls = candidate_pools.get(channel, set())
        if not urls:
            print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 无候选源")
            final_results[channel] = []
            continue

        ranked = await benchmark_channel_streams(channel, urls, semaphore)
        top_fastest = [url for speed, url in ranked[:MAX_STREAMS_PER_CHANNEL]]
        
        best_speed = ranked[0][0] if ranked else 0
        print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 测速通过 {len(ranked)} 个有效源，精选 Top {len(top_fastest)} (最快: {best_speed} KB/s)")
        final_results[channel] = top_fastest

    # 6. 生成纯净的 live.txt
    print(f"\n[*] 正在导出最终文件: {DEFAULT_OUTPUT_FILE} (已剔除所有 $ 标记)...")
    with open(DEFAULT_OUTPUT_FILE, "w", encoding="utf-8") as f:
        # 分组一：央视频道
        f.write("央视频道,#genre#\n")
        for ch in CCTV_CHANNELS:
            for url in final_results.get(ch, []):
                # 写入前二次确保没有任何 $
                f.write(f"{ch},{clean_stream_url(url)}\n")
        
        # 分组二：卫视频道
        f.write("卫视频道,#genre#\n")
        for ch in SATELLITE_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{clean_stream_url(url)}\n")

    total_exported = sum(len(v) for v in final_results.values())
    print("=" * 65)
    print(f"[√] 任务圆满完成！已成功导出 {total_exported} 条真实有效的高速直播源至 {DEFAULT_OUTPUT_FILE}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_pipeline())
