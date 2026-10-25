import akshare as ak
import pandas as pd
import datetime

def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== 正在拉取 A 股全市场数据 ({today}) ===")
    
    try:
        # 抓取全 A 股当前行情快照
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            raise ValueError("获取到的行情数据为空")
    except Exception as e:
        err_msg = f"拉取行情失败: {e}\n时间: {today}"
        print(err_msg)
        # 即使失败也写入文件，避免 Action 报文件不存在
        with open("latest_trin.txt", "w", encoding="utf-8") as f:
            f.write(err_msg)
        return

    # 过滤停牌或无成交额数据
    valid_df = df[df['成交额'] > 0]

    # 分离上涨与下跌股票 (过滤平盘)
    up_stocks = valid_df[valid_df['涨跌幅'] > 0]
    down_stocks = valid_df[valid_df['涨跌幅'] < 0]

    up_count = len(up_stocks)
    down_count = len(down_stocks)

    up_amount = up_stocks['成交额'].sum()
    down_amount = down_stocks['成交额'].sum()

    ratio_a = up_count / down_count if down_count > 0 else 1.0
    ratio_b = up_amount / down_amount if down_amount > 0 else 1.0
    trin = ratio_a / ratio_b if ratio_b > 0 else 1.0

    if trin < 0.6:
        status = "【极端超买 / 情绪狂热】资金高度集中在上涨股，警惕短线冲顶"
    elif trin > 1.8:
        status = "【极端超卖 / 恐慌砸盘】资金集中在下跌股抛售，关注短线恐慌盘宣泄后的反弹"
    else:
        status = "【多空均衡】资金与涨跌家数结构分布处于中性健康区间"

    output = f"""
=========================================================
            A股全市场阿姆氏指标 (TRIN) 监控报告
=========================================================
统计时间: {today}
---------------------------------------------------------
【市场宽广度 (A)】
- 上涨家数: {up_count} 只
- 下跌家数: {down_count} 只
- 宽广度 (A = 涨/跌家数): {ratio_a:.3f}

【资金力量比 (B)】
- 上涨成交总额: {up_amount / 1e8:.2f} 亿元
- 下跌成交总额: {down_amount / 1e8:.2f} 亿元
- 资金比 (B = 涨/跌成交额): {ratio_b:.3f}

---------------------------------------------------------
【核心指标】
阿姆氏指标 TRIN (A / B): {trin:.3f}
市场状态评估: {status}
=========================================================
"""
    print(output)

    with open("latest_trin.txt", "w", encoding="utf-8") as f:
        f.write(output)

if __name__ == "__main__":
    main()
