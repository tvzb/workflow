# -*- coding: utf-8 -*-
"""
配置模块：包含目标频道定义、统一命名规范、排序规则与优化运行参数
"""
import re

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


# 运行控制参数
MAX_STREAMS_PER_CHANNEL = 10      # 最终保留的最快直播源数
CANDIDATE_POOL_LIMIT = 25         # 单频道候选上限（优化为 25 个黄金平衡）
MAX_SAME_IP_PER_CHANNEL = 3       # 单频道相同 IP 节点最多保留 3 个（防雪崩）
SEARCH_WINDOW_DAYS = 30           # 检索时间窗口扩至 30 天（覆盖全月）
REQUEST_TIMEOUT = 3.5             # 连通性探测超时（秒）
SPEED_TEST_TIMEOUT = 2            # 切片下载测速超时（秒）
SPEED_TEST_MAX_BYTES = 1024 * 1024 * 2  # 测速单流最大抓取量（2MB）
MAX_CONCURRENT_TASKS = 25         # 协程并发测试数
DEFAULT_OUTPUT_FILE = "live.txt"  # 唯一输出文件格式
