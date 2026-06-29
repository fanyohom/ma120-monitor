"""
持仓监控 - MA120波段策略
监控指定持仓股票的MA120信号
"""

import pandas as pd
import numpy as np
from datetime import datetime
import warnings
import time
import os
import requests
warnings.filterwarnings('ignore')

# 设置代理
os.environ['http_proxy'] = 'http://127.0.0.1:7890'
os.environ['https_proxy'] = 'http://127.0.0.1:7890'
os.environ['all_proxy'] = 'socks5://127.0.0.1:7890'

# ============ MA120 策略参数 ============
MA_LONG = 120
BUY_THRESHOLD = 0.88   # MA120 * 0.88
SELL_THRESHOLD = 1.12  # MA120 * 1.12
DATA_DAYS = 150

# ============ 持仓列表 ============
MY_STOCKS = [
    ("600015", "华夏银行"),
    ("601998", "中信银行"),
    ("601077", "渝农商行"),
    ("601899", "紫金矿业"),
    ("000960", "锡业股份"),
    ("603233", "大参林"),
    ("002714", "牧原股份"),
    ("600900", "长江电力"),
    ("600036", "招商银行"),
    ("002948", "青岛银行"),
    ("601166", "兴业银行"),
]

# ============ 数据获取 ============
def get_stock_hist_data(stock_code: str, days: int = DATA_DAYS) -> pd.DataFrame:
    """通过新浪API获取历史K线数据"""
    try:
        if stock_code.startswith('6'):
            symbol = f'sh{stock_code}'
        else:
            symbol = f'sz{stock_code}'
        
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {
            "symbol": symbol,
            "scale": 240,
            "ma": "no",
            "datalen": days
        }
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['day'])
        df = df.set_index('date').sort_index()
        df['close'] = df['close'].astype(float)
        return df[['close']]
    except Exception as e:
        print(f"  ❌ 获取 {stock_code} 历史数据失败: {e}")
        return pd.DataFrame()


def get_stock_info(code: str) -> dict:
    """获取股票实时行情"""
    try:
        if code.startswith('6'):
            symbol = f'sh{code}'
        else:
            symbol = f'sz{code}'
        url = f'https://qt.gtimg.cn/q={symbol}'
        r = requests.get(url, timeout=10)
        fields = r.text.split('="')[1].split('~')
        return {
            'price': float(fields[3]) if fields[3] else None,
            'name': fields[1],
        }
    except:
        return {'price': None, 'name': code}


def calculate_ma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window).mean()


def check_signal(df: pd.DataFrame) -> dict:
    """检查MA120信号"""
    if len(df) < MA_LONG:
        return None
    
    df = df.copy()
    df['ma120'] = calculate_ma(df['close'], MA_LONG)
    
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    
    price = latest['close']
    ma120 = latest['ma120']
    
    if pd.isna(ma120) or ma120 == 0:
        return None
    
    ma120_pct = (price - ma120) / ma120 * 100
    
    prev_price = prev['close']
    prev_ma120 = prev['ma120'] if not pd.isna(prev['ma120']) else ma120
    prev_ma120_pct = (prev_price - prev_ma120) / prev_ma120 * 100 if prev_ma120 != 0 else 0
    
    result = {
        'price': price,
        'ma120': ma120,
        'ma120_pct': ma120_pct,
        'signal': 'hold',
        'signal_price': None,
    }
    
    if price < ma120 * BUY_THRESHOLD and prev_price >= prev_ma120 * BUY_THRESHOLD:
        result['signal'] = 'buy'
        result['signal_price'] = ma120 * BUY_THRESHOLD
    elif price > ma120 * SELL_THRESHOLD and prev_price <= prev_ma120 * SELL_THRESHOLD:
        result['signal'] = 'sell'
        result['signal_price'] = ma120 * SELL_THRESHOLD
    
    return result


# ============ 主程序 ============
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"📊 持仓监控 - {today}")
    print(f"{'='*60}")
    print(f"策略规则：买入 MA120×0.88 | 卖出 MA120×1.12\n")
    
    results = []
    
    for code, name in MY_STOCKS:
        print(f"🔍 检查 {name} ({code})...")
        
        # 获取实时价格
        info = get_stock_info(code)
        price = info.get('price')
        
        # 获取历史数据
        df = get_stock_hist_data(code)
        
        if df.empty:
            print(f"   ⚠️ 数据获取失败")
            continue
        
        signal = check_signal(df)
        
        if signal is None:
            print(f"   ⚠️ 数据不足")
            continue
        
        current_price = price or signal['price']
        ma120 = signal['ma120']
        ma120_pct = signal['ma120_pct']
        
        if signal['signal'] == 'buy':
            print(f"   🔥 买入信号！股价 {current_price:.2f} < MA120×0.88 ({signal['signal_price']:.2f})，偏离 {ma120_pct:.2f}%")
        elif signal['signal'] == 'sell':
            print(f"   📤 卖出信号！股价 {current_price:.2f} > MA120×1.12 ({signal['signal_price']:.2f})，偏离 {ma120_pct:.2f}%")
        elif ma120_pct >= 0:
            print(f"   📈 持仓区。股价 {current_price:.2f} > MA120 ({ma120:.2f})，偏离 +{ma120_pct:.2f}%")
        else:
            print(f"   📊 持仓区。股价 {current_price:.2f} < MA120 ({ma120:.2f})，偏离 {ma120_pct:.2f}%")
        
        results.append({
            'code': code,
            'name': name,
            'price': current_price,
            'ma120': ma120,
            'ma120_pct': ma120_pct,
            'signal': signal['signal'],
            'signal_price': signal['signal_price'],
        })
    
    # ============ 输出报告 ============
    print(f"\n{'='*60}")
    print(f"📈 持仓监控报告 - {today}")
    print(f"{'='*60}")
    
    buy_signals = [r for r in results if r['signal'] == 'buy']
    sell_signals = [r for r in results if r['signal'] == 'sell']
    hold_signals = [r for r in results if r['signal'] == 'hold']
    
    print(f"\n📊 持仓数量: {len(results)} 支")
    print(f"   MA120 信号: 买入 {len(buy_signals)} | 卖出 {len(sell_signals)} | 持仓 {len(hold_signals)}")
    
    if buy_signals:
        print(f"\n🔥 买入信号:")
        for r in buy_signals:
            print(f"   • {r['name']} ({r['code']})")
            print(f"     现价: {r['price']:.2f} | MA120: {r['ma120']:.2f} | 偏离: {r['ma120_pct']:.2f}%")
            print(f"     买入参考价: {r['signal_price']:.2f} (MA120×0.88)")
    
    if sell_signals:
        print(f"\n📤 卖出信号:")
        for r in sell_signals:
            print(f"   • {r['name']} ({r['code']})")
            print(f"     现价: {r['price']:.2f} | MA120: {r['ma120']:.2f} | 偏离: {r['ma120_pct']:.2f}%")
            print(f"     卖出参考价: {r['signal_price']:.2f} (MA120×1.12)")
    
    if hold_signals:
        print(f"\n📊 持仓区间:")
        for r in hold_signals:
            trend = "↑" if r['ma120_pct'] >= 0 else "↓"
            print(f"   • {r['name']} ({r['code']}): {r['price']:.2f} | MA120: {r['ma120']:.2f} | {trend}{abs(r['ma120_pct']):.2f}%")
    
    print(f"\n{'='*60}")
    
    return results


if __name__ == "__main__":
    main()
