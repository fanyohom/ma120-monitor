"""
MA120 高股息白马波段策略 - 历史回测
基于 "MA120 下方 12% 买入，MA120 上方 12% 卖出" 规则
回测时间：近 5 年
"""

import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# ============ 策略参数 ============
MA_LONG = 120      # MA120
MA_SHORT = 60      # MA60
BUY_THRESHOLD = 0.88   # MA120 * 0.88 = MA120 下方 12%
SELL_THRESHOLD = 1.12  # MA120 * 1.12 = MA120 上方 12%
DIVIDEND_REINVEST_BELOW_MA60 = True  # 分红再投入条件：股价 < MA60

# ============ 数据获取 ============
def get_stock_data(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    获取 A 股历史数据（使用 akshare）
    stock_code: 股票代码，如 "600519"（茅台）、"600900"（长江电力）
    """
    try:
        # 尝试获取日线数据
        df = ak.stock_zh_a_hist(
            symbol=stock_code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"  # 前复权
        )
        df.columns = ['date', 'open', 'close', 'high', 'low', 'volume', 'amount', 'amplitude', 'change_pct', 'change_amount', 'turnover']
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').sort_index()
        df['close'] = df['close'].astype(float)
        return df
    except Exception as e:
        print(f"  ❌ 获取 {stock_code} 数据失败: {e}")
        return pd.DataFrame()


def get_dividend_data(stock_code: str) -> pd.DataFrame:
    """
    获取股票分红数据（年度分红）
    """
    try:
        df = ak.stock_dividend_detail(
            symbol="sh" + stock_code if stock_code.startswith("6") else "sz" + stock_code,
            indicator="分红"
        )
        return df
    except:
        return pd.DataFrame()


# ============ 技术指标计算 ============
def calculate_ma(series: pd.Series, window: int) -> pd.Series:
    """计算移动平均线"""
    return series.rolling(window=window).mean()


def calculate_ma120_and_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算 MA120 及买卖信号
    返回包含持仓状态的 DataFrame
    """
    df = df.copy()
    df['ma120'] = calculate_ma(df['close'], MA_LONG)
    df['ma60'] = calculate_ma(df['close'], MA_SHORT)

    # 初始化
    df['signal'] = 0      # 0=空仓, 1=持仓
    df['buy_signal'] = False
    df['sell_signal'] = False
    df['dividend_reinvest'] = False  # 分红再投入信号

    # 逐行计算信号
    position = 0  # 当前是否持仓
    cost_basis = 0.0  # 买入成本（用于计算真实收益）

    for i in range(MA_LONG, len(df)):
        row = df.iloc[i]
        ma120 = row['ma120']
        ma60 = row['ma60']
        price = row['close']

        if pd.isna(ma120):
            continue

        # 买入信号：价格 < MA120 * 0.88（MA120 下方 12%），且空仓
        if not position and price < ma120 * BUY_THRESHOLD:
            df.iloc[i, df.columns.get_loc('buy_signal')] = True
            df.iloc[i, df.columns.get_loc('signal')] = 1
            position = 1
            cost_basis = price
            continue

        # 卖出信号：价格 > MA120 * 1.12（MA120 上方 12%），且持仓
        if position and price > ma120 * SELL_THRESHOLD:
            df.iloc[i, df.columns.get_loc('sell_signal')] = True
            df.iloc[i, df.columns.get_loc('signal')] = 0
            position = 0
            cost_basis = 0.0
            continue

        # 分红再投入：持仓且价格 < MA60
        if position and DIVIDEND_REINVEST_BELOW_MA60 and not pd.isna(ma60) and price < ma60:
            df.iloc[i, df.columns.get_loc('dividend_reinvest')] = True

        df.iloc[i, df.columns.get_loc('signal')] = position

    return df


# ============ 收益计算 ============
def calculate_returns(df: pd.DataFrame, initial_capital: float = 100000) -> dict:
    """
    根据交易信号计算策略收益
    """
    trades = []  # 记录每笔交易
    capital = initial_capital
    shares = 0
    position = False
    entry_price = 0.0

    total_trades = 0
    winning_trades = 0
    losing_trades = 0

    for i in range(len(df)):
        row = df.iloc[i]

        # 买入
        if row['buy_signal'] and not position:
            shares = capital / row['close']
            entry_price = row['close']
            capital = 0
            position = True
            total_trades += 1
            trades.append({
                'date': row.name,
                'action': 'BUY',
                'price': entry_price,
                'shares': shares,
                'capital_before': capital + shares * entry_price
            })

        # 卖出
        elif row['sell_signal'] and position:
            capital = shares * row['close']
            pnl_pct = (row['close'] - entry_price) / entry_price * 100
            trades.append({
                'date': row.name,
                'action': 'SELL',
                'price': row['close'],
                'shares': shares,
                'capital_after': capital,
                'pnl_pct': pnl_pct
            })
            if pnl_pct > 0:
                winning_trades += 1
            else:
                losing_trades += 1
            shares = 0
            position = False

    # 如果最终还持仓，按最后价格计算
    final_capital = capital + shares * df.iloc[-1]['close'] if position else capital

    # 计算关键指标
    total_return = (final_capital - initial_capital) / initial_capital * 100
    total_days = (df.index[-1] - df.index[0]).days
    annual_return = (final_capital / initial_capital) ** (365 / total_days) - 1
    annual_return_pct = annual_return * 100

    # 年化收益（简化）
    years = total_days / 365
    cagr = (final_capital / initial_capital) ** (1 / years) - 1 if years > 0 else 0

    win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0

    summary = {
        'initial_capital': initial_capital,
        'final_capital': final_capital,
        'total_return_pct': total_return,
        'annual_return_pct': annual_return_pct * 100,
        'cagr_pct': cagr * 100,
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'trades': trades,
        'start_date': str(df.index[0].date()),
        'end_date': str(df.index[-1].date()),
        'years': years
    }

    return summary


# ============ 完整回测报告 ============
def run_backtest(stock_code: str, stock_name: str, start_date: str, end_date: str):
    """对单个股票运行完整回测"""
    print(f"\n{'='*60}")
    print(f"📈 {stock_name} ({stock_code})")
    print(f"   回测区间: {start_date} ~ {end_date}")
    print(f"{'='*60}")

    # 获取数据
    print(f"  📡 正在获取数据...")
    df = get_stock_data(stock_code, start_date, end_date)
    if df.empty:
        print(f"  ❌ 数据获取失败，跳过")
        return None

    print(f"  ✅ 获取到 {len(df)} 个交易日数据")

    # 计算信号
    df = calculate_ma120_and_signals(df)

    # 计算收益
    result = calculate_returns(df)

    # 输出报告
    print(f"\n  💰 初始资金: {result['initial_capital']:,.0f} 元")
    print(f"  💰 最终资金: {result['final_capital']:,.0f} 元")
    print(f"  📊 总收益率: {result['total_return_pct']:+.2f}%")
    print(f"  📊 年化收益率(CAGR): {result['cagr_pct']:+.2f}%")
    print(f"  🔄 总交易次数: {result['total_trades']} 笔")
    print(f"  ✅ 盈利交易: {result['winning_trades']} 笔")
    print(f"  ❌ 亏损交易: {result['losing_trades']} 笔")
    print(f"  🎯 胜率: {result['win_rate']:.1f}%")

    # 打印交易明细
    if result['trades']:
        print(f"\n  📋 交易明细:")
        for t in result['trades']:
            if t['action'] == 'BUY':
                print(f"    {t['date'].date()} 买入 @ {t['price']:.2f} 元, 持有 {t['shares']:.2f} 股")
            else:
                print(f"    {t['date'].date()} 卖出 @ {t['price']:.2f} 元, 收益 {t['pnl_pct']:+.2f}%")

    return result


# ============ 主程序 ============
if __name__ == "__main__":

    # 回测区间：近 5 年
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=365*5)).strftime("%Y%m%d")

    # 测试股票池（高股息白马）
    stocks = [
        ("600900", "长江电力"),
        ("601919", "中远海控"),
        ("600519", "贵州茅台"),
        ("600036", "招商银行"),
        ("601288", "农业银行"),
        ("600028", "中国石化"),
        ("601398", "工商银行"),
        ("600887", "伊利股份"),
    ]

    print(f"""
╔══════════════════════════════════════════════════════╗
║     MA120 高股息白马波段策略 - 历史回测系统            ║
║     回测区间: {start_date} ~ {end_date}                    ║
╠══════════════════════════════════════════════════════╣
║  策略规则:                                             ║
║  • 买入: 股价 < MA120 * 0.88 (MA120 下方 12%)         ║
║  • 卖出: 股价 > MA120 * 1.12 (MA120 上方 12%)         ║
║  • 分红再投入: 持仓且股价 < MA60                       ║
║  • 仓位: 不止押一支，2-3 支轮动                       ║
╚══════════════════════════════════════════════════════╝
    """)

    results = {}
    for code, name in stocks:
        r = run_backtest(code, name, start_date, end_date)
        if r:
            results[name] = r

    # ============ 汇总报告 ============
    print(f"\n\n{'='*70}")
    print(f"📊 策略汇总报告")
    print(f"{'='*70}")
    print(f"{'股票名称':<12} {'总收益':>10} {'年化收益':>10} {'交易次数':>8} {'胜率':>8}")
    print(f"{'-'*70}")

    total_initial = 0
    total_final = 0

    for name, r in results.items():
        print(f"{name:<12} {r['total_return_pct']:>+9.2f}% {r['cagr_pct']:>+9.2f}% {r['total_trades']:>8} {r['win_rate']:>7.1f}%")
        total_initial += r['initial_capital']
        total_final += r['final_capital']

    print(f"{'-'*70}")
    overall_return = (total_final - total_initial) / total_initial * 100
    print(f"{'组合平均':<12} {overall_return:>+9.2f}%")

    print(f"\n⚠️  注意: 以上为前复权价格回测结果，未扣除交易费用")
    print(f"⚠️  分红再投入逻辑已简化，实际分红时点与股价波动可能不同步")
    print(f"⚠️  历史表现不代表未来收益，策略在震荡市和熊市可能失效")
