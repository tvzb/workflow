# -*- coding: utf-8 -*-
"""
配置模块：包含目标频道定义、统一命名规范、拼音别名表、URL清理、种子源裂变规则与运行参数
"""
import re
import urllib.parse
from typing import List, Tuple, Dict

# 1. 央视频道组（CCTV-1 至 CCTV-17，严格排除 CCTV-5+，统一带官方分类名称）
CCTV_CHANNELS = [
    "CCTV-1 综合",
    "CCTV-2 财经",
    "CCTV-3 综艺",
    "CCTV-4 中文国际",
    "CCTV-5 体育",
    "CCTV-6 电影",
    "CCTV-7 国防军事",
    "CCTV-8 电视剧",
    "CCTV-9 纪录",
    "CCTV-10 科教",
    "CCTV-11 戏曲",
    "CCTV-12 社会与法",
    "CCTV-13 新闻",
    "CCTV-14 少儿",
    "CCTV-15 音乐",
    "CCTV-16 奥林匹克",
    "CCTV-17 农业农村",
]

# 2. 卫视频道组（仅省级、直辖市及深圳卫视，纯净名称后无任何后缀）
# 特别指定的优先前置频道（7 个）
SATELLITE_PRIORITY = [
    "江西卫视",
    "东方卫视",
    "北京卫视",
    "江苏卫视",
    "浙江卫视",
    "湖南卫视",
    "辽宁卫视",
]

# 剩余各省/直辖市/深圳卫视（按拼音首字母 A-Z 顺序规范排列）
SATELLITE_REMAINDER = [
    "安徽卫视",    # Anhui (A)
    "重庆卫视",    # Chongqing (C)
    "东南卫视",    # Dongnan (D)
    "甘肃卫视",    # Gansu (G)
    "广东卫视",    # Guangdong (G)
    "广西卫视",    # Guangxi (G)
    "贵州卫视",    # Guizhou (G)
    "海南卫视",    # Hainan (H)
    "河北卫视",    # Hebei (H)
    "河南卫视",    # Henan (H)
    "黑龙江卫视",  # Heilongjiang (H)
    "湖北卫视",    # Hubei (H)
    "吉林卫视",    # Jilin (J)
    "内蒙古卫视",  # Neimenggu (N)
    "宁夏卫视",    # Ningxia (N)
    "青海卫视",    # Qinghai (Q)
    "山东卫视",    # Shandong (S)
    "山西卫视",    # Shanxi (S)
    "陕西卫视",    # Shaanxi (S)
    "深圳卫视",    # Shenzhen (S)
    "四川卫视",    # Sichuan (S)
    "天津卫视",    # Tianjin (T)
    "西藏卫视",    # Xizang (X)
    "新疆卫视",    # Xinjiang (X)
    "云南卫视",    # Yunnan (Y)
]

# 完整卫视频道总顺序表
SATELLITE_CHANNELS = SATELLITE_PRIORITY + SATELLITE_REMAINDER

# 所有目标频道总表
ALL_TARGET_CHANNELS = CCTV_CHANNELS + SATELLITE_CHANNELS


# 3. 频道英文/拼音标识对照字典（用于 URL 规律推导与裂变生成）
CHANNEL_SLUGS: Dict[str, List[str]] = {
    # 央视标识
    "CCTV-1 综合": ["cctv1", "cctv-1"],
    "CCTV-2 财经": ["cctv2", "cctv-2"],
    "CCTV-3 综艺": ["cctv3", "cctv-3"],
    "CCTV-4 中文国际": ["cctv4", "cctv-4"],
    "CCTV-5 体育": ["cctv5", "cctv-5"],
    "CCTV-6 电影": ["cctv6", "cctv-6"],
    "CCTV-7 国防军事": ["cctv7", "cctv-7"],
    "CCTV-8 电视剧": ["cctv8", "cctv-8"],
    "CCTV-9 纪录": ["cctv9", "cctv-9"],
    "CCTV-10 科教": ["cctv10", "cctv-10"],
    "CCTV-11 戏曲": ["cctv11", "cctv-11"],
    "CCTV-12 社会与法": ["cctv12", "cctv-12"],
    "CCTV-13 新闻": ["cctv13", "cctv-13"],
    "CCTV-14 少儿": ["cctv14", "cctv-14"],
    "CCTV-15 音乐": ["cctv15", "cctv-15"],
    "CCTV-16 奥林匹克": ["cctv16", "cctv-16"],
    "CCTV-17 农业农村": ["cctv17", "cctv-17"],
    # 卫视标识（拼音及常见简称）
    "江西卫视": ["jiangxi", "jxws"],
    "东方卫视": ["dongfang", "shanghai", "dfws"],
    "北京卫视": ["beijing", "bjws"],
    "江苏卫视": ["jiangsu", "jsws"],
    "浙江卫视": ["zhejiang", "zjws"],
    "湖南卫视": ["hunan", "hnws"],
    "辽宁卫视": ["liaoning", "lnws"],
    "安徽卫视": ["anhui", "ahws"],
    "重庆卫视": ["chongqing", "cqws"],
    "东南卫视": ["dongnan", "fujian", "dnws"],
    "甘肃卫视": ["gansu", "gsws"],
    "广东卫视": ["guangdong", "gdws"],
    "广西卫视": ["guangxi", "gxws"],
    "贵州卫视": ["guizhou", "gzws"],
    "海南卫视": ["hainan"],
    "河北卫视": ["hebei", "hbws"],
    "河南卫视": ["henan"],
    "黑龙江卫视": ["heilongjiang", "hljws"],
    "湖北卫视": ["hubei"],
    "吉林卫视": ["jilin", "jlws"],
    "内蒙古卫视": ["neimenggu", "nmgws"],
    "宁夏卫视": ["ningxia", "nxws"],
    "青海卫视": ["qinghai", "qhws"],
    "山东卫视": ["shandong", "sdws"],
    "山西卫视": ["shanxi"],
    "陕西卫视": ["shaanxi"],
    "深圳卫视": ["shenzhen", "szws"],
    "四川卫视": ["sichuan", "scws"],
    "天津卫视": ["tianjin", "tjws"],
    "西藏卫视": ["xizang", "xzws"],
    "新疆卫视": ["xinjiang", "xjws"],
    "云南卫视": ["yunnan", "ynws"],
}


def clean_stream_url(raw_url: str) -> str:
    """
    清洗直播源 URL：
    彻底清除 '$' 及其后面的任何文字（如 $订阅源、$电信 等），保留纯净链接
    """
    if not raw_url:
        return ""
    # 去除 $ 及其后的一切文字
    url = raw_url.split("$")[0].strip()
    return url


def normalize_channel_name(raw_name: str) -> str:
    """
    统一名称清洗规则：
    1. 彻底过滤 CCTV-5+ / CCTV5+；
    2. CCTV1 / cctv-1 综合 / CCTV1HD -> CCTV-1 综合；
    3. 江西卫视高清 / 东方卫视(上海) / 湖南HD -> 江西卫视 / 东方卫视 / 湖南卫视（不带任何后缀）。
    """
    if not raw_name:
        return ""
    
    clean_name = raw_name.strip()
    
    # 过滤 CCTV-5+ / CCTV5+
    if re.search(r'(?i)cctv[-_ ]?5\+', clean_name):
        return ""

    # 规范化 CCTV-1 至 CCTV-17
    cctv_match = re.search(r'(?i)cctv[-_ ]?(\d{1,2})', clean_name)
    if cctv_match:
        num = int(cctv_match.group(1))
        if 1 <= num <= 17:
            prefix = f"CCTV-{num} "
            for std_cctv in CCTV_CHANNELS:
                if std_cctv.startswith(prefix):
                    return std_cctv

    # 规范化卫视频道（排除尾部高清、1080P 等字样）
    for sat in SATELLITE_CHANNELS:
        keyword = sat.replace("卫视", "")
        if keyword in clean_name and ("卫视" in clean_name or sat in clean_name or "台" in clean_name):
            return sat
        # 兼容东方卫视标记为上海卫视的情况
        if sat == "东方卫视" and ("上海卫视" in clean_name or "东方台" in clean_name):
            return "东方卫视"

    return ""


def extrapolate_channels_from_seed(seed_url: str) -> List[Tuple[str, str]]:
    """
    核心种子裂变算法：根据优质有效源的命名规律，自动衍生推导同服务器其他所有央视及卫视地址
    输入例如：http://173.208.212.130:8181/1080p/cctv1.m3u8
    输出例如：[(CCTV-2 财经, .../cctv2.m3u8), ..., (江西卫视, .../jiangxi.m3u8)]
    """
    results: List[Tuple[str, str]] = []
    clean_seed = clean_stream_url(seed_url)
    if not clean_seed:
        return results

    # 模式 1：检测 URL 中是否包含 CCTV 模式（如 cctv1, cctv-1, cctv01, cctv1_1080p）
    cctv_pattern = re.search(r'(cctv[-_]?0?(\d{1,2}))([a-zA-Z0-9_\-]*)(\.m3u8|\.flv|\.ts)', clean_seed, re.IGNORECASE)
    if cctv_pattern:
        prefix = clean_seed[:cctv_pattern.start(1)]
        matched_token = cctv_pattern.group(1)
        suffix = clean_seed[cctv_pattern.end(3):]
        extra_tag = cctv_pattern.group(3)
        has_hyphen = "-" in matched_token
        has_zero = "0" in matched_token and len(cctv_pattern.group(2)) == 1

        # 衍生 CCTV-1 至 CCTV-17
        for std_ch in CCTV_CHANNELS:
            num = int(std_ch.split()[0].replace("CCTV-", ""))
            num_str = f"0{num}" if has_zero and num < 10 else str(num)
            token = f"cctv-{num_str}" if has_hyphen else f"cctv{num_str}"
            if matched_token.isupper():
                token = token.upper()
            extrapolated_url = f"{prefix}{token}{extra_tag}{suffix}"
            results.append((std_ch, extrapolated_url))

        # 同步衍生各省卫视（用拼音替换 cctvX）
        for std_sat in SATELLITE_CHANNELS:
            slug = CHANNEL_SLUGS.get(std_sat, [None])[0]
            if slug:
                extrapolated_url = f"{prefix}{slug}{suffix}"
                results.append((std_sat, extrapolated_url))
        return results

    # 模式 2：检测 URL 中是否包含已知卫视拼音（如 hunan.m3u8）
    for std_ch, slugs in CHANNEL_SLUGS.items():
        for slug in slugs:
            pattern = re.search(rf'({re.escape(slug)})([a-zA-Z0-9_\-]*)(\.m3u8|\.flv|\.ts)', clean_seed, re.IGNORECASE)
            if pattern:
                prefix = clean_seed[:pattern.start(1)]
                suffix = clean_seed[pattern.end(2):]
                # 衍生所有其他卫视
                for other_ch, other_slugs in CHANNEL_SLUGS.items():
                    target_slug = other_slugs[0]
                    extrapolated_url = f"{prefix}{target_slug}{suffix}"
                    results.append((other_ch, extrapolated_url))
                # 衍生央视频道
                for std_cctv in CCTV_CHANNELS:
                    cctv_num = std_cctv.split()[0].replace("CCTV-", "")
                    extrapolated_url = f"{prefix}cctv{cctv_num}{suffix}"
                    results.append((std_cctv, extrapolated_url))
                return results

    return results


# 运行控制参数
MAX_STREAMS_PER_CHANNEL = 10      # 最终保留的最快直播源数
CANDIDATE_POOL_LIMIT = 40         # 单频道候选池上限（支持裂变高充沛容量）
MAX_SAME_IP_PER_CHANNEL = 3       # 单频道相同 IP 节点最多保留 3 个（防雪崩）
SEARCH_WINDOW_DAYS = 30           # 检索时间窗口（30 天）
FISSION_TOP_SEEDS_LIMIT = 5       # 裂变触发种子源数量上限（选速度最快的前5台服务器）
REVERSE_HOST_SEARCH_LIMIT = 4     # 反查 GitHub 的优质 IP:Port 数量上限
REQUEST_TIMEOUT = 3.5             # 连通性探测超时（秒）
SPEED_TEST_TIMEOUT = 2            # 切片下载测速超时（秒）
SPEED_TEST_MAX_BYTES = 1024 * 1024 * 2  # 测速单流最大抓取量（2MB）
MAX_CONCURRENT_TASKS = 30         # 协程并发测试数
DEFAULT_OUTPUT_FILE = "live.txt"  # 唯一输出文件格式
