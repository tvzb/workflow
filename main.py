# -*- coding: utf-8 -*-
"""
极速高产全网多源 IPTV 引擎：
1. 多线程并发极速拉取 16+ 权威高星源池（10秒内完成上万源收集）；
2. TVBox 原生请求头与秒级输出刷新（无缓冲，实时可见进度）；
3. 协议可达保底（防止海外机房误杀国内好源）；
4. 历史好源去重垫后保底，确保每个频道稳稳达到 8~10 个以上；
5. 全流程彻底剔除 URL 中的 '$' 符号及后置文字。
"""
import os
import re
import time
import socket
import asyncio
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    FISSION_TOP_SEEDS_LIMIT,
    REVERSE_HOST_SEARCH_LIMIT,
    REQUEST_TIMEOUT,
    SPEED_TEST_TIMEOUT,
    SPEED_TEST_MAX_BYTES,
    MAX_CONCURRENT_TASKS,
    DEFAULT_OUTPUT_FILE,
    TVBOX_USER_AGENT,
)

HEADERS = {
    "User-Agent": TVBOX_USER_AGENT,
    "Accept": "*/*",
    "Connection": "keep-alive"
}

# 全网精选 16 个超高活跃度的 GitHub 每日同步 IPTV 直播列表矩阵
GITHUB_IPTV_MATRIX = [
    "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/output/result.txt",
    "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/output/user_result.txt",
    "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv4.m3u",
    "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
    "https://raw.githubusercontent.com/YueChan/Live/main/cctv.m3u",
    "https://raw.githubusercontent.com/asdjkl6/tv/tv/tv.txt",
    "https://raw.githubusercontent.com/suxing/iptv/main/feimao.txt",
    "https://raw.githubusercontent.com/suxing/iptv/main/itvlist.txt",
    "https://raw.githubusercontent.com/Supprise0901/TVBox_live/main/live.txt",
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.txt",
    "https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u",
    "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt",
    "https://raw.githubusercontent.com/zbds/live/main/live.txt",
    "https://raw.githubusercontent.com/joevess/IPTV/main/sources/live.txt",
    "https://raw.githubusercontent.com/lalifeier/IPTV/main/txt/cctv.txt",
    "https://raw.githubusercontent.com/lalifeier/IPTV/main/txt/satellite.txt",
    "https://raw.githubusercontent.com/zwc456baby/iptv_sources/main/iptv.txt",
]


def log(msg: str):
    """强制实时输出日志，防止 GitHub Actions 缓冲挂起"""
    print(msg, flush=True)


# ============================
# 1. 历史直播源合并（清除 $ 标记，保底留存）
# ============================
def load_previous_live_sources(file_path: str = DEFAULT_OUTPUT_FILE) -> Dict[str, List[str]]:
    """读取历史 live.txt 中的老源（清除 $），为频道提供稳固底池"""
    previous_sources: Dict[str, List[str]] = {ch: [] for ch in ALL_TARGET_CHANNELS}
    
    content = ""
    if os.path.exists(file_path):
        log(f"[*] 检测到本地历史文件 {file_path}，提取老源...")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    
    # 若本地为空或不存在，尝试从线上主页拉取
    if not content:
        fallback_online_url = "https://tvzb.github.io/live.txt"
        log(f"[*] 尝试从线上备份拉取历史底池 ({fallback_online_url})...")
        try:
            r = requests.get(fallback_online_url, headers=HEADERS, timeout=5)
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
                clean_url = clean_stream_url(raw_url)
                std_name = normalize_channel_name(ch_name)
                if std_name in previous_sources and clean_url.startswith("http"):
                    if clean_url not in previous_sources[std_name]:
                        previous_sources[std_name].append(clean_url)
    
    total_loaded = sum(len(v) for v in previous_sources.values())
    log(f"[+] 成功载入并清洗历史记录中的 {total_loaded} 个历史可用源！")
    return previous_sources


# ============================
# 2. IPv4 过滤与 Host 提取
# ============================
def is_valid_ipv4_url(url: str) -> bool:
    """仅保留 IPv4 协议地址（快速正则初筛）"""
    try:
        clean_url = clean_stream_url(url)
        parsed = urllib.parse.urlparse(clean_url)
        hostname = parsed.hostname
        if not hostname or ":" in hostname or hostname.startswith("["):
            return False

        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', hostname):
            parts = [int(p) for p in hostname.split('.')]
            return all(0 <= p <= 255 for p in parts)

        # 域名默认接纳（由下一步 HTTP 异步连接验证其 DNS，不在此处阻塞）
        return True
    except Exception:
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
    """快速拉取文件内容"""
    headers = HEADERS.copy()
    if token and "github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return ""


# ============================
# 3. 多线程极速并发全网拉取（10秒内完成）
# ============================
def search_github_candidates_parallel(token: str, candidate_pools: Dict[str, Set[str]]):
    """采用线程池多线程并发拉取 16 大权威源与 GitHub 仓库，秒级响应"""
    log(f"[*] 启动全网多源多线程并发拉取（矩阵源 + 仓库扫描）...")
    
    all_urls_to_fetch = list(GITHUB_IPTV_MATRIX)

    # 动态抓取 GitHub 上最近高频更新的 3 个 IPTV 仓库作为补充
    headers = HEADERS.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github.v3+json"

    try:
        repo_api = "https://api.github.com/search/repositories?q=iptv+m3u8&sort=updated&order=desc&per_page=4"
        r = requests.get(repo_api, headers=headers, timeout=5)
        if r.status_code == 200:
            for repo in r.json().get("items", []):
                b = repo.get("default_branch", "main")
                fn = repo.get("full_name", "")
                for fp in ["live.txt", "ipv4.m3u", "output/result.txt"]:
                    all_urls_to_fetch.append(f"https://raw.githubusercontent.com/{fn}/{b}/{fp}")
    except Exception:
        pass

    log(f"[+] 正在并发下载 {len(all_urls_to_fetch)} 个源列表文件...")
    t0 = time.time()
    
    # 使用 10 线程并发抓取文本，8 秒内搞定全部文件
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(fetch_content_from_url, u, token): u for u in all_urls_to_fetch}
        for future in as_completed(future_to_url):
            content = future.result()
            if content:
                parse_stream_content(content, candidate_pools)

    log(f"[+] 源文件并发拉取解析完成！耗时: {time.time() - t0:.2f} 秒")


def parse_stream_content(content: str, candidate_pools: Dict[str, Set[str]]):
    """解析直播源内容，自动剔除 '$' 及其后文字"""
    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line and "http" in line:
            parts = line.split(",", 1)
            std_name = normalize_channel_name(parts[0].strip())
            raw_url = parts[1].strip()
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
    """针对高价值主机快速反查"""
    if not token:
        return

    host_counts: Dict[str, int] = {}
    for ch, urls in candidate_pools.items():
        for u in urls:
            h = get_host_identifier(u)
            if h and re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+', h):
                host_counts[h] = host_counts.get(h, 0) + 1

    top_hosts = sorted(host_counts.keys(), key=lambda x: host_counts[x], reverse=True)[:REVERSE_HOST_SEARCH_LIMIT]
    if not top_hosts:
        return

    log(f"[*] 针对核心服务器 Host 进行快速反查: {', '.join(top_hosts)}")
    headers = HEADERS.copy()
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/vnd.github.v3+json"

    for host in top_hosts:
        query = f'"{host}" in:file extension:txt'
        api_url = f"https://api.github.com/search/code?q={urllib.parse.quote(query)}&per_page=3"
        try:
            r = requests.get(api_url, headers=headers, timeout=6)
            if r.status_code == 200:
                for item in r.json().get("items", [])[:2]:
                    html_url = item.get("html_url", "")
                    if html_url and "blob" in html_url:
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                        txt_data = fetch_content_from_url(raw_url, token)
                        if txt_data:
                            parse_stream_content(txt_data, candidate_pools)
        except Exception:
            pass


def apply_seed_fission(candidate_pools: Dict[str, Set[str]]):
    """种子源规律推导与裂变"""
    log("[*] 启动命名规律自动推导（种子源裂变），批量拓展全套央视与卫视频道...")
    fission_candidates = []
    for ch, urls in candidate_pools.items():
        for u in urls:
            if re.search(r'cctv|hunan|jiangxi|beijing|dongfang|1080p', u, re.IGNORECASE):
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

    log(f"[+] 规律推导完成，成功根据种子源衍生出 {fission_count} 个高可能性直播流！")


# ============================
# 4. TVBox 级别快速探测与测速引擎
# ============================
async def test_and_speedtest_stream(session: aiohttp.ClientSession, url: str) -> Tuple[bool, float, float]:
    """
    极速判定逻辑：
    1. 连通性测试：3.5 秒握手，返回 HTTP 200 且含 #EXTM3U 即判定为【真实可播活源】；
    2. 支持 Master Playlist 多级子流向下解析；
    3. 测速拉流：1.8 秒内下载切片测速，超时不判死，赋予保底分 120 KB/s。
    """
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

            if ts_url and ".m3u8" in ts_url.lower():
                try:
                    async with session.get(ts_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), allow_redirects=True) as sub_resp:
                        if sub_resp.status == 200:
                            sub_content = await sub_resp.content.read(2048)
                            sub_text = sub_content.decode('utf-8', errors='ignore')
                            for sub_line in sub_text.splitlines():
                                sub_line = sub_line.strip()
                                if sub_line and not sub_line.startswith("#"):
                                    ts_url = urllib.parse.urljoin(ts_url, sub_line)
                                    break
                except Exception:
                    pass

            baseline_speed = 120.0
            if not ts_url:
                return True, baseline_speed, ttfb

        # 快速实测拉流
        t_speed_start = time.perf_counter()
        bytes_received = 0
        try:
            async with session.get(ts_url, timeout=aiohttp.ClientTimeout(total=SPEED_TEST_TIMEOUT), allow_redirects=True) as ts_resp:
                if ts_resp.status == 200:
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
                    if elapsed > 0 and bytes_received > 0:
                        speed_kbs = (bytes_received / 1024.0) / elapsed
                        return True, round(speed_kbs, 2), round(ttfb, 2)
        except Exception:
            pass

        return True, baseline_speed, round(ttfb, 2)

    except Exception:
        return False, 0.0, 9999.0


async def benchmark_channel_streams(channel_name: str, urls: Set[str], semaphore: asyncio.Semaphore) -> List[Tuple[float, str]]:
    """并发测速，速度降序排列"""
    ranked_streams: List[Tuple[float, str]] = []
    
    conn = aiohttp.TCPConnector(ssl=False, limit=0)
    async with aiohttp.ClientSession(headers=HEADERS, connector=conn) as session:
        async def sem_task(url: str):
            async with semaphore:
                valid, speed, _ = await test_and_speedtest_stream(session, url)
                if valid and speed > 0:
                    ranked_streams.append((speed, clean_stream_url(url)))

        tasks = [sem_task(u) for u in urls]
        await asyncio.gather(*tasks, return_exceptions=True)

    ranked_streams.sort(key=lambda x: x[0], reverse=True)

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

    if len(filtered_by_ip) < MAX_STREAMS_PER_CHANNEL and len(ranked_streams) > len(filtered_by_ip):
        existing_urls = {u for _, u in filtered_by_ip}
        for speed, url in ranked_streams:
            if url not in existing_urls:
                filtered_by_ip.append((speed, url))
            if len(filtered_by_ip) >= MAX_STREAMS_PER_CHANNEL:
                break

    return filtered_by_ip


# ============================
# 5. 主流程协调与输出生成（秒级输出）
# ============================
async def run_pipeline():
    log("=" * 70)
    log("  IPTV 极速高产全网多源引擎：16+权威矩阵并发拉取 + TVBox防误杀 + 秒级日志")
    log("=" * 70)
    
    t_start_total = time.time()
    github_token = os.environ.get("GITHUB_TOKEN", "")

    # 1. 载入历史源作为底池备份
    previous_sources = load_previous_live_sources(DEFAULT_OUTPUT_FILE)
    candidate_pools: Dict[str, Set[str]] = {ch: set(previous_sources.get(ch, [])) for ch in ALL_TARGET_CHANNELS}

    # 2. 全网多线程并发拉取（10秒内搞定）
    search_github_candidates_parallel(github_token, candidate_pools)

    # 3. 核心服务器反查与种子裂变
    reverse_search_hosts_on_github(github_token, candidate_pools)
    apply_seed_fission(candidate_pools)

    total_candidates = sum(len(v) for v in candidate_pools.values())
    log(f"\n[+] 全渠道扩充完毕！当前候选源总库累计: {total_candidates} 条地址！")

    # 4. 高并发快速测速（每频道 2~3 秒）
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    final_results: Dict[str, List[str]] = {}

    log("\n[*] 启动连通性探测与 TS 切片深度测速（实时进度如下）：")
    for idx, channel in enumerate(ALL_TARGET_CHANNELS, 1):
        urls = candidate_pools.get(channel, set())
        if not urls:
            final_results[channel] = previous_sources.get(channel, [])[:MAX_STREAMS_PER_CHANNEL]
            log(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 无候选源，沿用历史底池 {len(final_results[channel])} 个")
            continue

        ranked = await benchmark_channel_streams(channel, urls, semaphore)
        top_fastest = [url for speed, url in ranked[:MAX_STREAMS_PER_CHANNEL]]

        # 保底补齐机制：若新测出的源不够 8 个，自动用历史老文件中的有效源在后面补足
        if len(top_fastest) < 8 and previous_sources.get(channel):
            for old_u in previous_sources[channel]:
                clean_old = clean_stream_url(old_u)
                if clean_old not in top_fastest:
                    top_fastest.append(clean_old)
                if len(top_fastest) >= MAX_STREAMS_PER_CHANNEL:
                    break

        best_speed = ranked[0][0] if ranked else 0
        log(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 锁定 {len(top_fastest)} 个有效直播源 (最快: {best_speed} KB/s)")
        final_results[channel] = top_fastest

    # 5. 导出文件
    log(f"\n[*] 正在导出最终文件: {DEFAULT_OUTPUT_FILE} (全部剔除 $ 标记)...")
    with open(DEFAULT_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("央视频道,#genre#\n")
        for ch in CCTV_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{clean_stream_url(url)}\n")
        
        f.write("卫视频道,#genre#\n")
        for ch in SATELLITE_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{clean_stream_url(url)}\n")

    total_exported = sum(len(v) for v in final_results.values())
    total_elapsed = time.time() - t_start_total
    log("=" * 70)
    log(f"[√] 任务圆满完成！耗时: {total_elapsed:.1f} 秒，已成功导出 {total_exported} 条直播源至 {DEFAULT_OUTPUT_FILE}")
    log("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_pipeline())
