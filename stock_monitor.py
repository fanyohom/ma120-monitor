"""
MA120 高股息白马波段策略 - 每日监控
每天检查持仓股票是否符合买卖信号
"""

import pandas as pd
import numpy as np
import baostock as bs
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# 登录 baostock（全局只需一次）
bs.login()

# ============ 策略参数 ============
MA_LONG = 120
MA_SHORT = 60
BUY_THRESHOLD = 0.88   # MA120 * 0.88
SELL_THRESHOLD = 1.12  # MA120 * 1.12

# ============ 股票池 ============
STOCKS = [
    ("601899", "紫金矿业"),
    ("600015", "华夏银行"),
    ("601998", "中信银行"),
    ("601077", "渝农商行"),
    ("000960", "锡业股份"),
    ("600989", "宝丰能源"),
]

# ============ 数据获取 ============
def get_stock_data(stock_code: str, days: int = 200) -> pd.DataFrame:
    """获取近N日日线数据（后复权）"""
    try:
        end_date = (datetime.now() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        
        # baostock 需要 sh/sz 前缀
        bs_code = f"sh.{stock_code}" if stock_code.startswith(("6",)) else f"sz.{stock_code}"
        
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"  # 2=后复权
        )
        
        if rs.error_code != '0':
            print(f"  ❌ 获取 {stock_code} 失败: {rs.error_msg}")
            return pd.DataFrame()
        
        data = []
        while rs.error_code == '0' and rs.next():
            data.append(rs.get_row_data())
        
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data, columns=['date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        return df
    except Exception as e:
        print(f"  ❌ 获取 {stock_code} 数据失败: {e}")
        return pd.DataFrame()


def calculate_ma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window).mean()


def check_signal(df: pd.DataFrame) -> dict:
    """检查当前是否符合买卖信号"""
    if len(df) < MA_LONG:
        return None
    
    df = df.copy()
    df['ma120'] = calculate_ma(df['close'], MA_LONG)
    df['ma60'] = calculate_ma(df['close'], MA_SHORT)
    
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    
    price = latest['close']
    ma120 = latest['ma120']
    ma60 = latest['ma60']
    
    if pd.isna(ma120):
        return None
    
    # 计算位置
    ma120_pct = (price - ma120) / ma120 * 100  # 相对MA120偏离百分比
    
    result = {
        'price': price,
        'ma120': ma120,
        'ma60': ma60,
        'ma120_pct': ma120_pct,
        'buy_signal': False,
        'sell_signal': False,
        'hold_signal': False,
        'in_buy_zone': False,
        'in_sell_zone': False,
        'message': ''
    }
    
    # 买入信号：价格 < MA120 * 0.88，且前一天不在买入区间
    prev_ma120 = prev['ma120'] if not pd.isna(prev['ma120']) else ma120
    prev_price = prev['close']
    prev_ma120_pct = (prev_price - prev_ma120) / prev_ma120 * 100 if prev_ma120 != 0 else 0
    
    if price < ma120 * BUY_THRESHOLD and prev_price >= prev_ma120 * BUY_THRESHOLD:
        result['buy_signal'] = True
        result['message'] = f"🔥 买入信号！股价 {price:.2f} < MA120×0.88 ({ma120*BUY_THRESHOLD:.2f})，偏离 MA120 {ma120_pct:.2f}%"
    elif price < ma120 * BUY_THRESHOLD:
        result['in_buy_zone'] = True
        result['message'] = f"📍 仍在买入区间。股价 {price:.2f} < MA120×0.88 ({ma120*BUY_THRESHOLD:.2f})，偏离 MA120 {ma120_pct:.2f}%"
    elif price > ma120 * SELL_THRESHOLD and prev_price <= prev_ma120 * SELL_THRESHOLD:
        result['sell_signal'] = True
        result['message'] = f"📤 卖出信号！股价 {price:.2f} > MA120×1.12 ({ma120*SELL_THRESHOLD:.2f})，偏离 MA120 {ma120_pct:.2f}%"
    elif price > ma120 * SELL_THRESHOLD:
        result['in_sell_zone'] = True
        result['message'] = f"📍 仍在卖出区间。股价 {price:.2f} > MA120×1.12 ({ma120*SELL_THRESHOLD:.2f})，偏离 MA120 +{ma120_pct:.2f}%"
    elif price >= ma120:
        result['hold_signal'] = True
        result['message'] = f"📈 持仓区间。股价 {price:.2f} > MA120 ({ma120:.2f})，偏离 MA120 +{ma120_pct:.2f}%"
    else:
        result['hold_signal'] = True
        result['message'] = f"📊 持仓区间。股价 {price:.2f} < MA120 ({ma120:.2f})，偏离 MA120 {ma120_pct:.2f}%"
    
    return result


# ============ 主程序 ============
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n📊 MA120 策略每日监控 - {today}\n")
    
    signals = []
    
    for code, name in STOCKS:
        print(f"  🔍 检查 {name} ({code})...")
        df = get_stock_data(code)
        
        if df.empty:
            print(f"     ❌ 数据获取失败")
            continue
        
        result = check_signal(df)
        
        if result is None:
            print(f"     ⚠️ 数据不足，跳过")
            continue
        
        print(f"     {result['message']}")
        
        signals.append({
            'code': code,
            'name': name,
            **result
        })
    
    # ============ 输出报告 ============
    print(f"\n{'='*60}")
    print(f"📈 监控汇总 - {today}")
    print(f"{'='*60}")
    
    buy_signals = [s for s in signals if s['buy_signal']]
    sell_signals = [s for s in signals if s['sell_signal']]
    in_buy_zone = [s for s in signals if s.get('in_buy_zone')]
    in_sell_zone = [s for s in signals if s.get('in_sell_zone')]
    
    if buy_signals:
        print(f"\n🔥 买入信号 ({len(buy_signals)} 支):")
        for s in buy_signals:
            print(f"   • {s['name']} ({s['code']}): {s['message']}")
    
    if sell_signals:
        print(f"\n📤 卖出信号 ({len(sell_signals)} 支):")
        for s in sell_signals:
            print(f"   • {s['name']} ({s['code']}): {s['message']}")
    
    if in_buy_zone:
        print(f"\n📍 仍在买入区间 ({len(in_buy_zone)} 支):")
        for s in in_buy_zone:
            print(f"   • {s['name']} ({s['code']}): {s['message']}")
    
    if in_sell_zone:
        print(f"\n📍 仍在卖出区间 ({len(in_sell_zone)} 支):")
        for s in in_sell_zone:
            print(f"   • {s['name']} ({s['code']}): {s['message']}")
    
    if not buy_signals and not sell_signals and not in_buy_zone and not in_sell_zone:
        print(f"\n⏸️ 今日无买入/卖出信号")
    
    # 打印持仓区间股票
    hold_signals = [s for s in signals if s['hold_signal'] and not s['buy_signal'] and not s['sell_signal']]
    if hold_signals:
        print(f"\n📊 持仓区间 ({len(hold_signals)} 支):")
        for s in hold_signals:
            print(f"   • {s['name']} ({s['code']}): {s['message']}")
    
    print(f"\n{'='*60}")
    
    return signals


if __name__ == "__main__":
    main()
