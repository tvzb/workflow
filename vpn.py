import os
import re
import time
import json
import base64
import urllib.parse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import yaml

# ==================== 核心参数配置 ====================
SOURCE_FILE = "vpn_sources.txt"
OUTPUT_FILE = "vpn_clash.yaml"
TEMP_CONFIG = "vpn_temp_config.yaml"

MAX_FINAL_NODES = 30        # 最终输出的精品节点数量
SPEED_TEST_CANDIDATES = 60  # 进入第二阶段（真实带宽下载测速）的候选节点数

# 模拟国内环境：使用小米/百度国内探测地址测试【节点 <-> 中国大陆】的真实回程延迟
CN_TEST_URL = "http://connect.rom.miui.com/generate_204"
# 国际连通性校验：确保节点能正常访问海外服务
GLOBAL_TEST_URL = "http://cp.cloudflare.com/generate_204"
# 真实下载测速文件 (Cloudflare 5MB 测速包)
SPEED_TEST_URL = "https://speed.cloudflare.com/__down?bytes=5000000"

DELAY_TIMEOUT = 2500        # 延迟测试超时时间 (毫秒)
SPEED_TIMEOUT = 6           # 带宽下载测试最大耗时 (秒)

PROXY_PORT = 17890          # Mihomo 本地 HTTP 代理端口
API_PORT = 19090            # Mihomo 外部控制 API 端口
# ======================================================


def fetch_sources():
    """读取 vpn_sources.txt 并并发拉取所有订阅内容"""
    if not os.path.exists(SOURCE_FILE):
        print(f"[-] 找不到 {SOURCE_FILE}，请检查文件是否存在。")
        return []

    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    raw_contents = []
    headers = {"User-Agent": "Clash.Meta/v1.18.0"}

    def download(url):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200 and resp.text.strip():
                print(f"[+] 成功拉取源: {url}")
                return resp.text
        except Exception as e:
            print(f"[-] 拉取失败 {url}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        for res in executor.map(download, urls):
            if res:
                raw_contents.append(res)

    return raw_contents


def safe_b64decode(s):
    """安全 Base64 解码"""
    s = s.strip().replace("\n", "").replace("\r", "")
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")


def parse_uri(uri):
    """解析 vmess://, trojan://, ss://, vless:// 链接为 Clash 节点字典"""
    uri = uri.strip()
    try:
        if uri.startswith("vmess://"):
            data = json.loads(safe_b64decode(uri[8:]))
            port = int(data.get("port", 0))
            if not data.get("add") or not port:
                return None
            node = {
                "name": data.get("ps") or "vmess",
                "type": "vmess",
                "server": data.get("add"),
                "port": port,
                "uuid": data.get("id"),
                "alterId": int(data.get("aid", 0)),
                "cipher": data.get("scy", "auto"),
                "tls": True if str(data.get("tls")).lower() == "tls" else False,
                "skip-cert-verify": True,
                "network": data.get("net", "tcp"),
                "udp": True
            }
            if node["network"] == "ws":
                node["ws-opts"] = {
                    "path": data.get("path") or "/",
                    "headers": {"Host": data.get("host") or node["server"]}
                }
            elif node["network"] == "grpc":
                node["grpc-opts"] = {"grpc-service-name": data.get("path") or ""}
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
                "udp": True
            }
            if query.get("type", ["tcp"])[0] == "ws":
                node["network"] = "ws"
                node["ws-opts"] = {
                    "path": query.get("path", ["/"])[0],
                    "headers": {"Host": query.get("host", [parsed.hostname])[0]}
                }
            return node

        elif uri.startswith("vless://"):
            parsed = urllib.parse.urlparse(uri)
            if not parsed.hostname or not parsed.port:
                return None
            name = urllib.parse.unquote(parsed.fragment) or "vless"
            query = urllib.parse.parse_qs(parsed.query)
            security = query.get("security", ["none"])[0]
            net_type = query.get("type", ["tcp"])[0]
            node = {
                "name": name,
                "type": "vless",
                "server": parsed.hostname,
                "port": int(parsed.port),
                "uuid": urllib.parse.unquote(parsed.username or ""),
                "tls": security in ["tls", "reality"],
                "udp": True,
                "skip-cert-verify": True,
                "network": net_type
            }
            if query.get("flow"):
                node["flow"] = query["flow"][0]
            if query.get("sni"):
                node["servername"] = query["sni"][0]
            if security == "reality":
                node["reality-opts"] = {
                    "public-key": query.get("pbk", [""])[0],
                    "short-id": query.get("sid", [""])[0]
                }
                node["client-fingerprint"] = query.get("fp", ["chrome"])[0]
            if net_type == "ws":
                node["ws-opts"] = {
                    "path": query.get("path", ["/"])[0],
                    "headers": {"Host": query.get("host", [parsed.hostname])[0]}
                }
            return node

        elif uri.startswith("ss://"):
            main_part, _, fragment = uri[5:].partition("#")
            name = urllib.parse.unquote(fragment) or "ss"
            if "@" in main_part:
                user_info, host_port = main_part.split("@", 1)
                method_pass = safe_b64decode(user_info)
            else:
                decoded = safe_b64decode(main_part)
                method_pass, host_port = decoded.rsplit("@", 1)

            method, password = method_pass.split(":", 1)
            server, port_str = host_port.split(":", 1)
            port = int(re.split(r"[/\?]", port_str)[0])
            return {
                "name": name,
                "type": "ss",
                "server": server,
                "port": port,
                "cipher": method,
                "password": password,
                "udp": True
            }
    except Exception:
        return None
    return None


def extract_and_clean_proxies(raw_contents):
    """提取所有节点并按 (协议, 服务器, 端口) 严格去重"""
    raw_proxies = []
    for content in raw_contents:
        # 1. 尝试 YAML 格式解析
        try:
            data = yaml.safe_load(content)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                raw_proxies.extend(data["proxies"])
                continue
        except Exception:
            pass

        # 2. 尝试 Base64 或纯文本 URI 解析
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
    valid_types = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria2", "tuic"}
    valid_ciphers = {
        "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
        "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
        "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
        "auto", "none", "rc4-md5", "aes-256-cfb", "aes-128-cfb"
    }

    for idx, p in enumerate(raw_proxies):
        if not isinstance(p, dict):
            continue
        p_type = str(p.get("type", "")).lower()
        server = str(p.get("server", "")).strip()
        port = p.get("port")

        if p_type not in valid_types or not server or not port:
            continue
        if server in ["127.0.0.1", "localhost", "0.0.0.0"] or ":" in server:
            continue
        try:
            p["port"] = int(port)
            if not (1 <= p["port"] <= 65535):
                continue
        except ValueError:
            continue

        # 过滤不兼容的加密方式防止 Mihomo 启动崩溃
        if p_type == "ss" and p.get("cipher") not in valid_ciphers:
            continue

        key = f"{p_type}:{server}:{p['port']}"
        if key not in unique:
            raw_name = str(p.get("name", f"Node_{idx}"))
            # 清理之前可能自带的测速标签与特殊字符
            clean_name = re.sub(r"\[.*?\]", "", raw_name)
            clean_name = re.sub(r'[\r\n\t\"\'\\|]', "", clean_name).strip()
            if not clean_name:
                clean_name = "Node"
            p["name"] = f"{clean_name}_{len(unique) + 1}"
            p["skip-cert-verify"] = True
            unique[key] = p

    print(f"[*] 原始提取 {len(raw_proxies)} 个节点，去重清洗后剩余 {len(unique)} 个。")
    return list(unique.values())


def run_benchmark(proxies):
    """
    启动 Mihomo 内核执行两阶段评测：
    阶段 1：并发测试国内回程延迟 (CN RTT) 与国际连通性
    阶段 2：对优选候选节点进行真实文件下载测速 (MB/s)
    """
    if not proxies:
        return []

    all_names = [p["name"] for p in proxies]
    test_config = {
        "port": PROXY_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
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

    proc = subprocess.Popen(
        ["./mihomo", "-d", ".", "-f", TEMP_CONFIG],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(3)  # 等待内核完全启动

    # ---------------- 阶段 1：国内回程延迟 + 国际连通性双测 ----------------
    print(f"[*] 阶段 1：正在测试 {len(proxies)} 个节点的【国内回程延迟】与【国际连通性】...")

    def check_latency(proxy):
        name = proxy["name"]
        enc_name = urllib.parse.quote(name, safe="")
        cn_api = f"http://127.0.0.1:{API_PORT}/proxies/{enc_name}/delay?timeout={DELAY_TIMEOUT}&url={urllib.parse.quote(CN_TEST_URL)}"
        global_api = f"http://127.0.0.1:{API_PORT}/proxies/{enc_name}/delay?timeout={DELAY_TIMEOUT}&url={urllib.parse.quote(GLOBAL_TEST_URL)}"
        try:
            # 1. 测国际连通性
            r_gl = requests.get(global_api, timeout=3.5)
            if r_gl.status_code != 200:
                return None
            gl_delay = r_gl.json().get("delay", 0)
            if gl_delay <= 0:
                return None

            # 2. 测中国大陆回程延迟（核心指标：模拟国内访问该节点的物理回路）
            r_cn = requests.get(cn_api, timeout=3.5)
            if r_cn.status_code == 200:
                cn_delay = r_cn.json().get("delay", 0)
                if 0 < cn_delay < DELAY_TIMEOUT:
                    # 综合延迟：70% 权重给国内回程，30% 权重给国际出站
                    weighted_delay = int(cn_delay * 0.7 + gl_delay * 0.3)
                    return (weighted_delay, cn_delay, proxy)
        except Exception:
            pass
        return None

    stage1_passed = []
    with ThreadPoolExecutor(max_workers=35) as executor:
        futures = [executor.submit(check_latency, p) for p in proxies]
        for future in as_completed(futures):
            res = future.result()
            if res:
                stage1_passed.append(res)

    # 按综合延迟从低到高排序，取前 SPEED_TEST_CANDIDATES 名进入带宽实测
    stage1_passed.sort(key=lambda x: x[0])
    candidates = stage1_passed[:SPEED_TEST_CANDIDATES]
    print(f"[+] 阶段 1 完成：{len(stage1_passed)} 个节点双端连通，选出延迟最低的 {len(candidates)} 个进入带宽实测。")

    # ---------------- 阶段 2：真实下载吞吐量测速 ----------------
    print(f"[*] 阶段 2：开始逐个实测真实下载带宽 (限时 {SPEED_TIMEOUT}s/节点)...")
    scored_nodes = []
    proxies_dict = {
        "http": f"http://127.0.0.1:{PROXY_PORT}",
        "https": f"http://127.0.0.1:{PROXY_PORT}"
    }

    for weighted_delay, cn_delay, proxy in candidates:
        name = proxy["name"]
        try:
            # 切换 Mihomo 当前代理节点
            requests.put(
                f"http://127.0.0.1:{API_PORT}/proxies/SPEED_TEST_SELECTOR",
                json={"name": name},
                timeout=2
            )

            start_t = time.time()
            downloaded = 0
            with requests.get(SPEED_TEST_URL, proxies=proxies_dict, stream=True, timeout=4) as r:
                if r.status_code == 200:
                    for chunk in r.iter_content(chunk_size=65536):
                        downloaded += len(chunk)
                        if time.time() - start_t >= 3.0:  # 每个节点最多测 3 秒即止，节省 Action 时间
                            break
            elapsed = max(time.time() - start_t, 0.1)
            speed_mbps = round((downloaded / (1024 * 1024)) / elapsed, 2)
        except Exception:
            speed_mbps = 0.0

        # 即使下载测速大文件被限流，只要双端延迟极佳也保留保底分
        # 综合排序分：速度越高越好，延迟越低越好
        # 计算公式：每 1 MB/s 记 100 分，减去 综合延迟(ms) * 0.25
        score = (speed_mbps * 100) - (weighted_delay * 0.25)

        # 将实测速度和国内回程延迟直接写入节点名，方便在 OpenClash 面板直观查看
        base_name = re.sub(r"_\d+$", "", name)
        proxy["name"] = f"[{speed_mbps:.1f}M|{cn_delay}ms] {base_name}"
        scored_nodes.append((score, speed_mbps, cn_delay, proxy))
        print(f"    -> {proxy['name']} (得分: {score:.1f})")

    proc.terminate()
    proc.wait()
    if os.path.exists(TEMP_CONFIG):
        os.remove(TEMP_CONFIG)

    # 按综合得分从高到低排序（速度越快、延迟越小排在最前面）
    scored_nodes.sort(key=lambda x: (-x[0], x[2]))

    # 确保最终节点名称唯一
    final_proxies = []
    used_names = set()
    for idx, (_, speed, delay, p) in enumerate(scored_nodes[:MAX_FINAL_NODES], 1):
        final_name = f"{idx:02d}_{p['name']}"
        while final_name in used_names:
            final_name += "_"
        used_names.add(final_name)
        p["name"] = final_name
        final_proxies.append(p)

    print(f"[+] 阶段 2 完成！已按【速度优先 + 低延时】排序选出 Top {len(final_proxies)} 节点。")
    return final_proxies


def generate_clash_yaml(proxies):
    """生成可直接导入 OpenClash 或作为 proxy-provider 的配置文件"""
    if not proxies:
        print("[-] 本次未筛出有效节点，保留原有配置不覆盖。")
        return

    all_names = [p["name"] for p in proxies]

    # 地区智能分组（适配你的日本/美国专线分流需求）
    jp_names = [n for n in all_names if re.search(r"JP|Japan|日本|东京|大阪|埼玉|🇯🇵", n, re.I)]
    us_names = [n for n in all_names if re.search(r"US|United States|America|美国|洛杉矶|圣何塞|西雅图|硅谷|纽约|芝加哥|🇺🇸", n, re.I)]
    hk_sg_names = [n for n in all_names if re.search(r"HK|Hong Kong|香港|SG|Singapore|新加坡|狮城|TW|台湾|🇭🇰|🇸🇬|🇹🇼", n, re.I)]

    if not jp_names:
        jp_names = all_names[:10]
    if not us_names:
        us_names = all_names[:10]
    if not hk_sg_names:
        hk_sg_names = all_names[:10]

    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "🚀 节点选择",
                "type": "select",
                "proxies": ["⚡ 自动最快", "🇯🇵 日本专线", "🇺🇸 美国专线", "🇭🇰 亚太高速"] + all_names
            },
            {
                "name": "⚡ 自动最快",
                "type": "url-test",
                "proxies": all_names,
                "url": "http://www.gstatic.com/generate_204",
                "interval": 180,
                "tolerance": 30
            },
            {
                "name": "🇯🇵 日本专线",
                "type": "url-test",
                "proxies": jp_names,
                "url": "http://www.gstatic.com/generate_204",
                "interval": 180,
                "tolerance": 30
            },
            {
                "name": "🇺🇸 美国专线",
                "type": "url-test",
                "proxies": us_names,
                "url": "http://www.gstatic.com/generate_204",
                "interval": 180,
                "tolerance": 30
            },
            {
                "name": "🇭🇰 亚太高速",
                "type": "url-test",
                "proxies": hk_sg_names,
                "url": "http://www.gstatic.com/generate_204",
                "interval": 180,
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
    print(f"[+] 成功生成配置文件: {OUTPUT_FILE}")


if __name__ == "__main__":
    raw_list = fetch_sources()
    clean_list = extract_and_clean_proxies(raw_list)
    best_nodes = run_benchmark(clean_list)
    generate_clash_yaml(best_nodes)
