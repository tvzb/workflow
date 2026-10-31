import os
import re
import time
import json
import random
import socket
import base64
import urllib.parse
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import yaml

# ==================== V2.0 重构版核心配置 ====================
SOURCE_FILE = "vpn_sources.txt"
OUTPUT_FILE = "vpn_clash.yaml"
TEMP_CONFIG = "vpn_temp_config.yaml"

MAX_SUBNET_NODES = 2        # 【核心防霸榜】同一个 /24 网段或域名最多只准保留 2 个节点
MAX_INITIAL_POOL = 800      # 进入 Mihomo 初筛的最大节点数
STABILITY_ROUNDS = 3        # 连续稳定性测试轮数（3轮全通才算稳，0丢包）

# 分地区独立保底配额（共输出约 45 个高质量多地区节点）
QUOTA_JP = 12               # 日本专线配额
QUOTA_US = 12               # 美国专线配额
QUOTA_AP = 12               # 亚太(港/新/台/韩)配额
QUOTA_OTHER = 9             # 欧洲/其他高速配额

# 测速与连通性探测地址
CN_TEST_URL = "http://connect.rom.miui.com/generate_204"
GLOBAL_TEST_URL = "https://cp.cloudflare.com/generate_204"
SPEED_TEST_URL = "https://speed.cloudflare.com/__down?bytes=4000000"

DELAY_TIMEOUT = 2200        # 单次握手超时 (ms)
PROXY_PORT = 17890
API_PORT = 19090
# =============================================================


def fetch_sources():
    if not os.path.exists(SOURCE_FILE):
        print(f"[-] 找不到 {SOURCE_FILE}", flush=True)
        return []

    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    print(f"[*] 正在并发拉取 {len(urls)} 个节点源...", flush=True)
    raw_contents = []
    headers = {"User-Agent": "Clash.Meta/v1.18.0"}

    def download(url):
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code == 200 and resp.text.strip():
                print(f"  [+] 成功: {url}", flush=True)
                return resp.text
        except Exception as e:
            print(f"  [-] 失败 {url}: {e}", flush=True)
        return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        for res in executor.map(download, urls):
            if res:
                raw_contents.append(res)
    return raw_contents


def safe_b64decode(s):
    s = s.strip().replace("\n", "").replace("\r", "")
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")


def parse_uri(uri):
    """只解析抗封锁能力强的协议：vless, trojan, hysteria2, tuic, 以及带 tls/ws 的 vmess"""
    uri = uri.strip()
    try:
        if uri.startswith("vmess://"):
            data = json.loads(safe_b64decode(uri[8:]))
            port = int(data.get("port", 0))
            net = str(data.get("net", "tcp")).lower()
            tls = str(data.get("tls", "")).lower() == "tls"
            # 淘汰无混淆的裸 TCP vmess
            if net not in ["ws", "grpc", "h2"] and not tls:
                return None
            if not data.get("add") or not port or not data.get("id"):
                return None
            node = {
                "name": data.get("ps") or "vmess",
                "type": "vmess",
                "server": data.get("add"),
                "port": port,
                "uuid": data.get("id"),
                "alterId": int(data.get("aid", 0)),
                "cipher": data.get("scy", "auto"),
                "tls": tls,
                "skip-cert-verify": True,
                "network": net,
                "udp": True
            }
            if tls and data.get("sni"):
                node["servername"] = data.get("sni")
            if net == "ws":
                node["ws-opts"] = {
                    "path": data.get("path") or "/",
                    "headers": {"Host": data.get("host") or node["server"]}
                }
            elif net == "grpc":
                node["grpc-opts"] = {"grpc-service-name": data.get("path") or ""}
            return node

        elif uri.startswith("vless://"):
            parsed = urllib.parse.urlparse(uri)
            if not parsed.hostname or not parsed.port:
                return None
            name = urllib.parse.unquote(parsed.fragment) or "vless"
            query = urllib.parse.parse_qs(parsed.query)
            security = query.get("security", ["none"])[0]
            net_type = query.get("type", ["tcp"])[0]
            # 必须带 TLS、Reality 或 WS 伪装才保留
            if security not in ["tls", "reality"] and net_type not in ["ws", "grpc"]:
                return None
            node = {
                "name": name,
                "type": "vless",
                "server": parsed.hostname,
                "port": int(parsed.port),
                "uuid": urllib.parse.unquote(parsed.username or ""),
                "tls": security in ["tls", "reality"],
                "udp": True,
                "skip-cert-verify": True,
                "network": net_type,
                "client-fingerprint": query.get("fp", ["chrome"])[0]
            }
            if query.get("flow"):
                node["flow"] = query["flow"][0]
            if query.get("sni"):
                node["servername"] = query["sni"][0]
            if security == "reality":
                pbk = query.get("pbk", [""])[0]
                if not pbk:
                    return None
                node["reality-opts"] = {
                    "public-key": pbk,
                    "short-id": query.get("sid", [""])[0]
                }
            if net_type == "ws":
                node["ws-opts"] = {
                    "path": query.get("path", ["/"])[0],
                    "headers": {"Host": query.get("host", [parsed.hostname])[0]}
                }
            elif net_type == "grpc":
                node["grpc-opts"] = {"grpc-service-name": query.get("serviceName", [""])[0]}
            return node

        elif uri.startswith("trojan://"):
            parsed = urllib.parse.urlparse(uri)
            if not parsed.hostname or not parsed.port:
                return None
            name = urllib.parse.unquote(parsed.fragment) or "trojan"
            query = urllib.parse.parse_qs(parsed.query)
            node = {
                "name": name,
                "type": "trojan",
                "server": parsed.hostname,
                "port": int(parsed.port),
                "password": urllib.parse.unquote(parsed.username or ""),
                "sni": query.get("sni", [parsed.hostname])[0],
                "skip-cert-verify": True,
                "udp": True,
                "client-fingerprint": "chrome"
            }
            if query.get("type", ["tcp"])[0] == "ws":
                node["network"] = "ws"
                node["ws-opts"] = {
                    "path": query.get("path", ["/"])[0],
                    "headers": {"Host": query.get("host", [parsed.hostname])[0]}
                }
            return node

        elif uri.startswith(("hysteria2://", "hy2://")):
            parsed = urllib.parse.urlparse(uri)
            if not parsed.hostname or not parsed.port:
                return None
            name = urllib.parse.unquote(parsed.fragment) or "hy2"
            query = urllib.parse.parse_qs(parsed.query)
            node = {
                "name": name,
                "type": "hysteria2",
                "server": parsed.hostname,
                "port": int(parsed.port),
                "password": urllib.parse.unquote(parsed.username or ""),
                "sni": query.get("sni", [parsed.hostname])[0],
                "skip-cert-verify": True,
                "udp": True
            }
            if query.get("obfs"):
                node["obfs"] = query["obfs"][0]
                node["obfs-password"] = query.get("obfs-password", [""])[0]
            return node
    except Exception:
        return None
    return None


def get_subnet_key(server):
    """获取 IP 的 /24 C段特征或主域名特征，用于彻底封杀同一机房批量霸榜"""
    server = str(server).strip().lower()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", server):
        parts = server.split(".")
        return f"ip_{parts[0]}.{parts[1]}.{parts[2]}.0"
    parts = server.split(".")
    if len(parts) >= 2:
        return f"domain_{parts[-2]}.{parts[-1]}"
    return server


def is_gfw_resistant(p):
    """
    核心过滤器：一刀切淘汰在国内必死的裸奔 SS/SSR/明文TCP节点
    只保留真正能过墙的抗封锁协议！
    """
    p_type = str(p.get("type", "")).lower()

    # 1. 直接淘汰全部裸 SS / SSR（截图里 82.38.31.x 全是这类垃圾节点）
    if p_type in ["ss", "ssr"]:
        # 除非带了 v2ray-plugin / obfs 插件，否则一律淘汰
        if not p.get("plugin"):
            return False

    # 2. VLESS / VMess 必须带 TLS、Reality 或 WS/gRPC 传输层伪装
    if p_type in ["vmess", "vless"]:
        net = str(p.get("network", "tcp")).lower()
        tls = bool(p.get("tls")) or bool(p.get("reality-opts"))
        if net == "tcp" and not tls:
            return False

    # 3. 保留 trojan, hysteria2, tuic
    return p_type in ["vmess", "vless", "trojan", "hysteria2", "tuic", "ss"]


def classify_region(name):
    """识别节点所属地区桶"""
    if re.search(r"JP|Japan|日本|东京|大阪|埼玉|名古屋|🇯🇵", name, re.I):
        return "JP"
    if re.search(r"US|United States|America|美国|洛杉矶|圣何塞|西雅图|硅谷|纽约|芝加哥|达拉斯|亚特兰大|🇺🇸", name, re.I):
        return "US"
    if re.search(r"HK|Hong\s*Kong|香港|SG|Singapore|新加坡|狮城|TW|Taiwan|台湾|台北|KR|Korea|韩国|首尔|🇭🇰|🇸🇬|🇹🇼|🇰🇷", name, re.I):
        return "AP"
    return "OTHER"


def extract_and_filter_proxies(raw_contents):
    raw_proxies = []
    for content in raw_contents:
        try:
            data = yaml.safe_load(content)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                raw_proxies.extend(data["proxies"])
                continue
        except Exception:
            pass

        text = content.strip()
        if "://" not in text:
            try:
                text = safe_b64decode(text)
            except Exception:
                pass

        for line in text.splitlines():
            node = parse_uri(line)
            if node:
                raw_proxies.append(node)

    unique = {}
    subnet_counter = defaultdict(int)

    # 打乱顺序，防止单一订阅源占满配额
    random.shuffle(raw_proxies)

    for idx, p in enumerate(raw_proxies):
        if not isinstance(p, dict):
            continue
        if not is_gfw_resistant(p):
            continue

        p_type = str(p.get("type", "")).lower()
        server = str(p.get("server", "")).strip()
        port = p.get("port")

        if not server or not port or server in ["127.0.0.1", "localhost", "0.0.0.0"] or ":" in server or " " in server:
            continue
        try:
            p["port"] = int(port)
            if not (1 <= p["port"] <= 65535):
                continue
        except ValueError:
            continue

        if p_type in ["vmess", "vless"] and not p.get("uuid"):
            continue

        # 精确去重
        exact_key = f"{p_type}:{server}:{p['port']}"
        if exact_key in unique:
            continue

        # C段子网限流：防止类似 82.38.31.x 的同一批垃圾节点霸占名额
        subnet_key = get_subnet_key(server)
        if subnet_counter[subnet_key] >= MAX_SUBNET_NODES:
            continue
        subnet_counter[subnet_key] += 1

        raw_name = str(p.get("name", f"Node_{idx}"))
        clean_name = re.sub(r"\[.*?\]", "", raw_name)
        clean_name = re.sub(r'[\r\n\t\"\'\\|,]', "", clean_name).strip()
        if not clean_name:
            clean_name = "Node"

        p["name"] = f"{clean_name}_{len(unique) + 1}"
        p["skip-cert-verify"] = True
        p["udp"] = True
        unique[exact_key] = p

    all_clean = list(unique.values())
    print(f"[*] 原始抓取 {len(raw_proxies)} 个 -> 淘汰裸SS与同网段霸榜后，剩余 {len(all_clean)} 个抗封锁独立节点。", flush=True)

    # 按地区均衡抽样进入测试池
    if len(all_clean) > MAX_INITIAL_POOL:
        buckets = {"JP": [], "US": [], "AP": [], "OTHER": []}
        for p in all_clean:
            buckets[classify_region(p["name"])].append(p)
        selected = (
            buckets["JP"][:220] +
            buckets["US"][:220] +
            buckets["AP"][:220] +
            buckets["OTHER"][:140]
        )
        if len(selected) < MAX_INITIAL_POOL:
            rest = [p for p in all_clean if p not in selected]
            selected.extend(rest[:MAX_INITIAL_POOL - len(selected)])
        all_clean = selected
        print(f"[*] 已按【日/美/亚太/欧美】均衡分配 {len(all_clean)} 个节点进入三轮抗抖动实测。", flush=True)

    return all_clean


def wait_for_mihomo_ready(timeout=12):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{API_PORT}/version", timeout=1)
            if r.status_code == 200:
                print(f"[+] Mihomo 内核已启动: {r.json().get('version')}", flush=True)
                return True
        except Exception:
            time.sleep(0.5)
    return False


def run_benchmark(proxies):
    if not proxies:
        return []

    all_names = [p["name"] for p in proxies]
    test_config = {
        "mixed-port": PROXY_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "error",
        "external-controller": f"127.0.0.1:{API_PORT}",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "SPEED_TEST_SELECTOR",
                "type": "select",
                "proxies": all_names
            }
        ],
        "rules": [
            "MATCH,SPEED_TEST_SELECTOR"
        ]
    }

    with open(TEMP_CONFIG, "w", encoding="utf-8") as f:
        yaml.safe_dump(test_config, f, allow_unicode=True)

    proc = subprocess.Popen(["./mihomo", "-d", ".", "-f", TEMP_CONFIG])
    if not wait_for_mihomo_ready():
        print("[-] Mihomo 启动失败！", flush=True)
        proc.terminate()
        return []

    # ---------------- 阶段 1：三轮连续抗抖动 + 0丢包稳定性筛选 ----------------
    print(f"[*] 阶段 1：执行 {STABILITY_ROUNDS} 轮连续稳定性压力测试（任何一轮超时即淘汰）...", flush=True)

    def test_single_round(proxy):
        name = proxy["name"]
        enc_name = urllib.parse.quote(name, safe="")
        cn_api = f"http://127.0.0.1:{API_PORT}/proxies/{enc_name}/delay?timeout={DELAY_TIMEOUT}&url={urllib.parse.quote(CN_TEST_URL)}"
        gl_api = f"http://127.0.0.1:{API_PORT}/proxies/{enc_name}/delay?timeout={DELAY_TIMEOUT}&url={urllib.parse.quote(GLOBAL_TEST_URL)}"
        try:
            r_gl = requests.get(gl_api, timeout=2.5)
            if r_gl.status_code != 200:
                return None
            gl_d = r_gl.json().get("delay", 0)

            r_cn = requests.get(cn_api, timeout=2.5)
            if r_cn.status_code != 200:
                return None
            cn_d = r_cn.json().get("delay", 0)

            if 0 < gl_d < DELAY_TIMEOUT and 0 < cn_d < DELAY_TIMEOUT:
                return (cn_d, gl_d)
        except Exception:
            pass
        return None

    # 记录每个节点每一轮的延迟表现
    survivors = list(proxies)
    delay_history = defaultdict(list)

    for r_idx in range(1, STABILITY_ROUNDS + 1):
        next_survivors = []
        with ThreadPoolExecutor(max_workers=60) as executor:
            future_map = {executor.submit(test_single_round, p): p for p in survivors}
            for future in as_completed(future_map):
                p = future_map[future]
                res = future.result()
                if res:
                    delay_history[p["name"]].append(res)
                    next_survivors.append(p)
        survivors = next_survivors
        print(f"  -> 第 {r_idx}/{STABILITY_ROUNDS} 轮校验完成，0丢包存活节点数: {len(survivors)}", flush=True)
        if r_idx < STABILITY_ROUNDS:
            time.sleep(1)

    # 计算平均延迟与抖动（Jitter），并按地区分桶选拔候选人进入带宽测速
    region_candidates = {"JP": [], "US": [], "AP": [], "OTHER": []}
    for p in survivors:
        records = delay_history[p["name"]]
        avg_cn = int(sum(x[0] for x in records) / len(records))
        avg_gl = int(sum(x[1] for x in records) / len(records))
        jitter = max(x[0] for x in records) - min(x[0] for x in records)

        # 协议加分：VLESS-Reality / Hysteria2 / WS+TLS 在国内穿透率远高于普通节点
        proto_bonus = 0
        if p.get("reality-opts") or p.get("type") == "hysteria2":
            proto_bonus = -80  # 等效降低 80ms 排序权重
        elif p.get("network") in ["ws", "grpc"] and p.get("tls"):
            proto_bonus = -50

        rank_metric = avg_cn * 0.6 + avg_gl * 0.2 + jitter * 0.5 + proto_bonus
        reg = classify_region(p["name"])
        region_candidates[reg].append((rank_metric, avg_cn, jitter, p))

    # 每个地区挑出最优的前 N 名进入真实下载测速
    speed_test_list = []
    quotas_stage2 = {"JP": 16, "US": 16, "AP": 16, "OTHER": 12}
    for reg, items in region_candidates.items():
        items.sort(key=lambda x: x[0])
        selected = items[:quotas_stage2[reg]]
        speed_test_list.extend(selected)
        print(f"  [+] 地区桶 [{reg}] 0丢包存活 {len(items)} 个 -> 选出 {len(selected)} 个测带宽", flush=True)

    # ---------------- 阶段 2：真实下载带宽测试 + 最终地区配额录取 ----------------
    print(f"[*] 阶段 2：对 {len(speed_test_list)} 个高稳定节点进行真实下载测速...", flush=True)
    final_buckets = {"JP": [], "US": [], "AP": [], "OTHER": []}

    for idx, (metric, avg_cn, jitter, proxy) in enumerate(speed_test_list, 1):
        name = proxy["name"]
        p_type = proxy["type"].upper()
        reg = classify_region(name)
        speed_mbps = 0.0
        try:
            requests.put(
                f"http://127.0.0.1:{API_PORT}/proxies/SPEED_TEST_SELECTOR",
                json={"name": name},
                timeout=2
            )
            cmd = [
                "curl", "-x", f"http://127.0.0.1:{PROXY_PORT}",
                "-s", "-o", "/dev/null",
                "-w", "%{speed_download}",
                "--connect-timeout", "2",
                "--max-time", "2.5",
                SPEED_TEST_URL
            ]
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=3.5).decode().strip()
            speed_mbps = round(float(out) / (1024 * 1024), 2)
        except Exception:
            speed_mbps = 0.0

        # 速度为 0 说明无法跑大流量数据，直接淘汰！
        if speed_mbps < 0.15:
            print(f"  [{idx:02d}/{len(speed_test_list):02d}] 淘汰(断流/无吞吐) {name}", flush=True)
            continue

        # 综合分：带宽权重 + 低延迟 + 低抖动
        score = (speed_mbps * 100) - (avg_cn * 0.2) - (jitter * 0.3)
        base_name = re.sub(r"_\d+$", "", name)
        proxy["name"] = f"[{speed_mbps:.1f}M|{avg_cn}ms] {p_type}_{base_name}"
        final_buckets[reg].append((score, speed_mbps, avg_cn, proxy))
        print(f"  [{idx:02d}/{len(speed_test_list):02d}] 通过: {proxy['name']} (抖动:{jitter}ms)", flush=True)

    proc.terminate()
    proc.wait()
    if os.path.exists(TEMP_CONFIG):
        os.remove(TEMP_CONFIG)

    # 按配额从各地区桶提取最优节点，再全局按得分排序
    final_Targets = []
    target_quotas = {"JP": QUOTA_JP, "US": QUOTA_US, "AP": QUOTA_AP, "OTHER": QUOTA_OTHER}
    for reg, quota in target_quotas.items():
        bucket = final_buckets[reg]
        bucket.sort(key=lambda x: (-x[0], x[2]))
        final_Targets.extend(bucket[:quota])

    final_Targets.sort(key=lambda x: (-x[0], x[2]))

    final_proxies = []
    used_names = set()
    for idx, (_, speed, delay, p) in enumerate(final_Targets, 1):
        final_name = f"{idx:02d}_{p['name']}"
        while final_name in used_names:
            final_name += "_"
        used_names.add(final_name)
        p["name"] = final_name
        final_proxies.append(p)

    print(f"[+] 全部评测完成！最终精选出 {len(final_proxies)} 个跨机房抗封锁节点。", flush=True)
    return final_proxies


def generate_clash_yaml(proxies):
    if not proxies:
        print("[-] 未筛出有效节点，放弃更新。", flush=True)
        return

    all_names = [p["name"] for p in proxies]
    jp_names = [n for n in all_names if classify_region(n) == "JP"]
    us_names = [n for n in all_names if classify_region(n) == "US"]
    ap_names = [n for n in all_names if classify_region(n) == "AP"]

    if not jp_names:
        jp_names = all_names
    if not us_names:
        us_names = all_names
    if not ap_names:
        ap_names = all_names

    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "unified-delay": True,      # 开启统一延迟计算，去除握手偏差
        "tcp-concurrent": True,     # 开启 TCP 并发连接，大幅提升国内连接成功率
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "🚀 节点选择",
                "type": "select",
                "proxies": ["⚡ 自动最快", "🛡️ 故障转移(最稳)", "🇯🇵 日本专线", "🇺🇸 美国专线", "🇭🇰 亚太高速"] + all_names
            },
            {
                "name": "⚡ 自动最快",
                "type": "url-test",
                "proxies": all_names,
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 120,
                "tolerance": 30,
                "lazy": False
            },
            {
                "name": "🛡️ 故障转移(最稳)",
                "type": "fallback",
                "proxies": all_names,
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 120,
                "lazy": False
            },
            {
                "name": "🇯🇵 日本专线",
                "type": "url-test",
                "proxies": jp_names,
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 120,
                "tolerance": 30
            },
            {
                "name": "🇺🇸 美国专线",
                "type": "url-test",
                "proxies": us_names,
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 120,
                "tolerance": 30
            },
            {
                "name": "🇭🇰 亚太高速",
                "type": "url-test",
                "proxies": ap_names,
                "url": "https://cp.cloudflare.com/generate_204",
                "interval": 120,
                "tolerance": 30
            }
        ],
        "rules": [
            "GEOIP,CN,DIRECT",
            "MATCH,🚀 节点选择"
        ]
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    print(f"[+] 已生成抗封锁优化版配置: {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    raw_list = fetch_sources()
    clean_list = extract_and_filter_proxies(raw_list)
    best_nodes = run_benchmark(clean_list)
    generate_clash_yaml(best_nodes)
