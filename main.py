# -*- coding: utf-8 -*-
"""
核心执行引擎（高产全网多源版）：
1. 融合 18+ GitHub 权威日更高星源池 + 活跃仓库嗅探 + 代码精准检索；
2. TVBox 原生内核（okhttp/3.12.1）请求头与 5 秒握手防海外机房误杀；
3. 支持 Master Playlist 多级子流递归解析；
4. 协议可达即保底为活源，跨海测速辅助降序排列；
5. 历史有效源深度融合补充，确保每个频道稳稳达到 8~10 个以上高质量源；
6. 彻底剔除所有 URL 中的 '$' 符号及后置文字。
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
    TVBOX_USER_AGENT,
)

HEADERS = {
    "User-Agent": TVBOX_USER_AGENT,
    "Accept": "*/*",
    "Connection": "keep-alive"
}

# 全网精选 18 个超高活跃度的 GitHub 每日同步 IPTV 直播列表矩阵
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
    "https://raw.githubusercontent.com/BurningC4/Chinese-IPTV/master/TV-IPV4.m3u",
    "https://raw.githubusercontent.com/zwc456baby/iptv_sources/main/iptv.txt",
]


# ============================
# 1. 历史直播源合并（清除 $ 标记，保底留存）
# ============================
def load_previous_live_sources(file_path: str = DEFAULT_OUTPUT_FILE) -> Dict[str, List[str]]:
    """读取历史 live.txt 中的老源（清除 $），为频道提供稳固底池"""
    previous_sources: Dict[str, List[str]] = {ch: [] for ch in ALL_TARGET_CHANNELS}
    
    content = ""
    if os.path.exists(file_path):
        print(f"[*] 检测到本地历史文件 {file_path}，提取老源...")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
    
    if not content:
        fallback_online_url = "https://tvzb.github.io/live.txt"
        print(f"[*] 尝试从线上备份拉取历史底池 ({fallback_online_url})...")
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
                clean_url = clean_stream_url(raw_url)
                std_name = normalize_channel_name(ch_name)
                if std_name in previous_sources and clean_url.startswith("http"):
                    if clean_url not in previous_sources[std_name]:
                        previous_sources[std_name].append(clean_url)
    
    total_loaded = sum(len(v) for v in previous_sources.values())
    print(f"[+] 成功载入并清洗历史记录中的 {total_loaded} 个历史可用源！")
    return previous_sources


# ============================
# 2. IPv4 过滤与 Host 提取
# ============================
def is_valid_ipv4_url(url: str) -> bool:
    """仅保留 IPv4 协议地址"""
    try:
        clean_url = clean_stream_url(url)
        parsed = urllib.parse.urlparse(clean_url)
        hostname = parsed.hostname
        if not hostname or ":" in hostname or hostname.startswith("["):
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
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return ""


# ============================
# 3. 海量全网采集与解析（含权威矩阵 + 活跃仓库 + 代码搜索）
# ============================
def search_github_candidates(token: str, candidate_pools: Dict[str, Set[str]]):
    """多渠道并发拉网式采集：18+权威矩阵 + 近30天活跃仓库 + 关键字搜索"""
    now = datetime.now()
    since_date = (now - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    current_ym = now.strftime("%Y-%m")

    print(f"[*] 启动全网拉网式采集（时间窗口：近 {SEARCH_WINDOW_DAYS} 天，从 {since_date} 至今）...")

    headers = HEADERS.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github.v3+json"

    scraped_files: Set[str] = set()

    # 1. 注入全网 18 大权威聚合矩阵（保证海量活源基础）
    scraped_files.update(GITHUB_IPTV_MATRIX)

    # 2. 嗅探近 30 天内活跃推送的 IPTV 与 TVBox 仓库
    repo_queries = [
        f"iptv+m3u8+pushed:>{since_date}",
        f"tvbox+live+pushed:>{since_date}",
    ]
    for rq in repo_queries:
        api_url = f"https://api.github.com/search/repositories?q={rq}&sort=updated&order=desc&per_page=10"
        try:
            r = requests.get(api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                for repo in r.json().get("items", []):
                    b = repo.get("default_branch", "main")
                    fn = repo.get("full_name", "")
                    for fp in ["live.txt", "tv.txt", "ipv4.m3u", "IPTV.m3u", "output/result.txt"]:
                        scraped_files.add(f"https://raw.githubusercontent.com/{fn}/{b}/{fp}")
        except Exception:
            pass

    # 3. 代码精准关键字检索
    code_queries = [
        f"CCTV-1 {current_ym} extension:txt",
        f"CCTV1 {current_ym} extension:m3u",
        "央视频道,#genre# extension:txt",
        "卫视频道,#genre# extension:txt",
    ]
    for q in code_queries:
        api_url = f"https://api.github.com/search/code?q={urllib.parse.quote(q)}&sort=indexed&order=desc&per_page=8"
        try:
            r = requests.get(api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                for item in r.json().get("items", [])[:6]:
                    html_url = item.get("html_url", "")
                    if html_url and "blob" in html_url:
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                        scraped_files.add(raw_url)
            elif r.status_code in (403, 429):
                time.sleep(2)
        except Exception:
            pass
        time.sleep(1.0)

    print(f"[+] 汇集共 {len(scraped_files)} 个源列表文件，开始并发拉取与解析...")
    for file_url in scraped_files:
        content = fetch_content_from_url(file_url, token)
        if content:
            parse_stream_content(content, candidate_pools)


def parse_stream_content(content: str, candidate_pools: Dict[str, Set[str]]):
    """解析 TXT 或 M3U 内容，彻底剥离 '$' 及其后字符"""
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
    """【高阶功能 1：优质 IP:Port 全局反查】"""
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

    print(f"\n[*] 启动高价值主机反查，检索核心服务器所有频道: {', '.join(top_hosts)}")
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
            time.sleep(1.2)
        except Exception:
            pass


def apply_seed_fission(candidate_pools: Dict[str, Set[str]]):
    """【高阶功能 2：种子源规律推导裂变】"""
    print("\n[*] 启动命名规律自动推导（种子源裂变），批量衍生全套央视与卫视频道...")
    fission_candidates = []
    for ch, urls in candidate_pools.items():
        for u in urls:
            if re.search(r'cctv|hunan|jiangxi|beijing|dongfang|1080p|live', u, re.IGNORECASE):
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

    print(f"[+] 规律推导完成，成功根据种子源衍生出 {fission_count} 个高可能性直播流！")


# ============================
# 4. TVBox 原生级智能连通与测速引擎（防海外误杀）
# ============================
async def test_and_speedtest_stream(session: aiohttp.ClientSession, url: str) -> Tuple[bool, float, float]:
    """
    智能判定逻辑（与 TVBox 一致）：
    1. 连通性测试：放宽握手至 5 秒。只要返回 HTTP 200 且含 #EXTM3U，即判定为【真实可播活源】；
    2. 支持 Master Playlist 多级子流向下解析；
    3. 测速拉流：若拉流顺畅则按测得网速排序；若跨海延迟高但能播放，赋予基准分 120 KB/s，绝不直接丢弃！
    """
    try:
        clean_url = clean_stream_url(url)
        t_start = time.perf_counter()
        
        # 阶段 1：获取 M3U8 播放列表
        async with session.get(clean_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), allow_redirects=True) as resp:
            if resp.status != 200:
                return False, 0.0, 9999.0
            
            ttfb = (time.perf_counter() - t_start) * 1000
            content_sample = await resp.content.read(4096)
            text_sample = content_sample.decode('utf-8', errors='ignore')

            if "#EXTM3U" not in text_sample and "#EXTINF" not in text_sample:
                return False, 0.0, 9999.0

            # 提取真实切片或二级子播放列表
            ts_url = None
            lines = text_sample.splitlines()
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#"):
                    ts_url = urllib.parse.urljoin(clean_url, line)
                    break

            # 若为 Master Playlist，向下请求一级子 m3u8
            if ts_url and ".m3u8" in ts_url.lower():
                try:
                    async with session.get(ts_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), allow_redirects=True) as sub_resp:
                        if sub_resp.status == 200:
                            sub_content = await sub_resp.content.read(4096)
                            sub_text = sub_content.decode('utf-8', errors='ignore')
                            for sub_line in sub_text.splitlines():
                                sub_line = sub_line.strip()
                                if sub_line and not sub_line.startswith("#"):
                                    ts_url = urllib.parse.urljoin(ts_url, sub_line)
                                    break
                except Exception:
                    pass

            # 握手成功已可播放，设置保底速度
            baseline_speed = 120.0

            if not ts_url:
                return True, baseline_speed, ttfb

        # 阶段 2：尝试拉取媒体分片实测带宽
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

        # 即使跨海切片测速超时，只要前面已证实是有效 m3u8，TVBox 在国内就能正常播放，保留该源！
        return True, baseline_speed, round(ttfb, 2)

    except Exception:
        return False, 0.0, 9999.0


async def benchmark_channel_streams(channel_name: str, urls: Set[str], semaphore: asyncio.Semaphore) -> List[Tuple[float, str]]:
    """并发测速，速度降序排列，执行 IP 多样性过滤"""
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

    # 同 IP 节点数量控制
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

    # 补齐名额
    if len(filtered_by_ip) < MAX_STREAMS_PER_CHANNEL and len(ranked_streams) > len(filtered_by_ip):
        existing_urls = {u for _, u in filtered_by_ip}
        for speed, url in ranked_streams:
            if url not in existing_urls:
                filtered_by_ip.append((speed, url))
            if len(filtered_by_ip) >= MAX_STREAMS_PER_CHANNEL:
                break

    return filtered_by_ip


# ============================
# 5. 主流程协调与输出生成（含历史底池保底补齐）
# ============================
async def run_pipeline():
    print("=" * 70)
    print("   IPTV 全网大采集引擎：18+权威矩阵 + TVBox防误杀 + 裂变反查 + 历史深度融合")
    print("=" * 70)
    
    github_token = os.environ.get("GITHUB_TOKEN", "")

    # 1. 载入历史源作为底池备份
    previous_sources = load_previous_live_sources(DEFAULT_OUTPUT_FILE)
    candidate_pools: Dict[str, Set[str]] = {ch: set(previous_sources.get(ch, [])) for ch in ALL_TARGET_CHANNELS}

    # 2. 全网 18 大矩阵与 GitHub 深度采集
    search_github_candidates(github_token, candidate_pools)

    # 3. 针对高价值服务器 Host:Port 执行 GitHub 全局反查
    reverse_search_hosts_on_github(github_token, candidate_pools)

    # 4. 根据优质种子源地址规律执行裂变衍生
    apply_seed_fission(candidate_pools)

    total_candidates = sum(len(v) for v in candidate_pools.values())
    print(f"\n[+] 全渠道扩充完毕！当前候选源总库累计: {total_candidates} 条地址！")

    # 5. 并发严格测速与优选
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    final_results: Dict[str, List[str]] = {}

    print("\n[*] 启动连通性探测与 TS 切片深度测速（TVBox 原生校验模式）...")
    for idx, channel in enumerate(ALL_TARGET_CHANNELS, 1):
        urls = candidate_pools.get(channel, set())
        if not urls:
            final_results[channel] = previous_sources.get(channel, [])[:MAX_STREAMS_PER_CHANNEL]
            print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 无候选源，沿用历史底池 {len(final_results[channel])} 个")
            continue

        ranked = await benchmark_channel_streams(channel, urls, semaphore)
        top_fastest = [url for speed, url in ranked[:MAX_STREAMS_PER_CHANNEL]]

        # 【核心保底机制】：若测出的数量不足 8 个，自动将历史老文件中去重后的已知源补齐到后面
        if len(top_fastest) < 8 and previous_sources.get(channel):
            for old_u in previous_sources[channel]:
                clean_old = clean_stream_url(old_u)
                if clean_old not in top_fastest:
                    top_fastest.append(clean_old)
                if len(top_fastest) >= MAX_STREAMS_PER_CHANNEL:
                    break

        best_speed = ranked[0][0] if ranked else 0
        print(f"[{idx:02d}/{len(ALL_TARGET_CHANNELS):02d}] {channel}: 精选保底锁定 {len(top_fastest)} 个直播源 (最快速度: {best_speed} KB/s)")
        final_results[channel] = top_fastest

    # 6. 生成纯净的 live.txt
    print(f"\n[*] 正在导出最终文件: {DEFAULT_OUTPUT_FILE} (全部清除 $ 标记)...")
    with open(DEFAULT_OUTPUT_FILE, "w", encoding="utf-8") as f:
        # 分组一：央视频道
        f.write("央视频道,#genre#\n")
        for ch in CCTV_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{clean_stream_url(url)}\n")
        
        # 分组二：卫视频道
        f.write("卫视频道,#genre#\n")
        for ch in SATELLITE_CHANNELS:
            for url in final_results.get(ch, []):
                f.write(f"{ch},{clean_stream_url(url)}\n")

    total_exported = sum(len(v) for v in final_results.values())
    print("=" * 70)
    print(f"[√] 任务圆满完成！已成功导出 {total_exported} 条高品质直播源至 {DEFAULT_OUTPUT_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_pipeline())
