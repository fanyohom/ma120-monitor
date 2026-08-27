# ma120-monitor

基于 **MA120 均线** 的持仓 & 关注池监控脚本，每日通过飞书推送买入/卖出信号。

## 策略

- **MA120：** 最近 120 个交易日收盘价的算术平均值，即 `sum(最近 120 个交易日收盘价) / 120`；不足 120 根日 K 的股票会跳过。
- **偏离率：** `(现价 - MA120) / MA120 × 100%`。
- **买入线：** `MA120 × 0.88`，即低于 MA120 约 12%。
- **卖出线：** `MA120 × 1.12`，即高于 MA120 约 12%。

### 行情口径

- **A 股：** 新浪财经日 K 和实时盘口行情。
- **港股：** 腾讯前复权日 K 和实时盘口行情，股票代码使用 `HK` 加 5 位数字，例如 `HK01801`。
- **现价：** 使用实时盘口最新成交价，用于计算当前偏离率和判断信号。
- **MA120：** 只根据日 K 收盘价序列计算，实时价不会直接替换均线序列中的收盘价。
- **缓存：** 日 K 本地 JSON 缓存在 `.stock_cache/`，同一自然日内复用，跨日自动刷新；实时盘口不走本地缓存。
- **回退：** 实时盘口请求失败时，现价会回退到日 K 最新收盘价，保证脚本不中断。

### 信号判定

- **买入信号：** 当前价跌破当前买入线，并且上一交易日收盘价仍在上一交易日买入线之上或等于买入线。
- **卖出信号：** 当前价突破当前卖出线，并且上一交易日收盘价仍在上一交易日卖出线之下或等于卖出线。
- **区间状态：** 如果股票已经连续处于阈值之外，不会每天重复生成新的买入或卖出信号，而是显示为“低于买入线”或“高于卖出线”。

因此，“今日买入/卖出信号”表示当天首次穿越阈值；“低于买入线/高于卖出线”表示当前所处区间，两者含义不同。

## 文件结构

```
ma120-monitor/
├── stock_dynamic_monitor.py     # 主脚本（持仓 + 关注池，飞书推送）
├── stock_monitor.py             # 旧版（保留备查）
├── stock_portfolio_monitor.py   # 旧版（保留备查）
├── backtest_ma120_strategy.py   # MA120 策略回测工具
├── stocks.xlsx                  # 数据源（sheet: portfolio / watchlist）
└── .stock_cache/                # 日 K 行情缓存（当天有效，一只一个 JSON）
```

## 用法

### 安装 uv

本项目使用 `uv` 管理 Python、虚拟环境和依赖。首次使用先安装 `uv`：

```powershell
# Windows（推荐）
winget install --id=astral-sh.uv -e
```

macOS / Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装后运行 `uv --version` 确认命令可用。项目通过 `pyproject.toml` 声明依赖，并用 `uv.lock` 锁定版本；首次执行 `uv run` 时会自动准备 Python 3.12 和项目环境。

### 首次配置

项目提供了 `.env.example` 模板。先复制一份为 `.env.local`：

```powershell
Copy-Item .env.example .env.local
```

然后编辑 `.env.local`，把占位 token 替换成飞书群机器人 webhook：

```env
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的-webhook-token
```

`.env.local` 已被 `.gitignore` 忽略，不要提交真实 webhook。

### Windows PowerShell

```powershell
cd D:\Project\github\ma120-monitor
$env:PYTHONUTF8='1'
$env:UV_CACHE_DIR='D:\Project\github\ma120-monitor\.uv-cache'

# 持仓 + 关注池（默认推送飞书卡片）
uv run python stock_dynamic_monitor.py

# 只看终端，不发飞书
uv run python stock_dynamic_monitor.py --no-feishu

# 只看持仓
uv run python stock_dynamic_monitor.py --portfolio --no-feishu

# 只看关注池
uv run python stock_dynamic_monitor.py --watchlist --no-feishu

# 运行测试
uv run python -m unittest discover -s tests

# 运行近 5 年历史回测
uv run --extra backtest python backtest_ma120_strategy.py
```

### macOS / Linux

```bash
cd /path/to/ma120-monitor
export PYTHONUTF8=1
export UV_CACHE_DIR="$PWD/.uv-cache"

# 持仓 + 关注池（默认推送飞书卡片）
uv run python stock_dynamic_monitor.py

# 只看终端，不发飞书
uv run python stock_dynamic_monitor.py --no-feishu
```

## 配置

编辑 `stocks.xlsx`，包含两个 sheet：

**sheet `portfolio`** — 持仓清单：

| 股票代码 | 股票名称 | 成本价 | 持仓数量 |
|---|---|---|---|
| 600015 | 华夏银行 | 0 | 0 |
| 601899 | 紫金矿业 | 0 | 0 |

> 成本价/数量留空或 0 表示不计算浮盈，只看信号。
> 股票代码可写成文本或数字，脚本会自动补齐 6 位。

**sheet `watchlist`** — 关注池：

| 股票代码 | 股票名称 |
|---|---|
| 601998 | 中信银行 |
| 600989 | 宝丰能源 |

表头支持中文（`股票代码/股票名称/成本价/持仓数量`）或英文（`code/name/cost/shares`）。

港股代码请使用 `HK` 加 5 位数字，例如信达生物写作 `HK01801`，以保留前导零并自动切换港股行情源。

**依赖：** `uv run` 会根据 `pyproject.toml` 和 `uv.lock` 自动准备固定版本的 Python 与项目依赖，无需手动执行 `pip install`。回测依赖按需通过 `--extra backtest` 安装，旧版脚本依赖按需通过 `--extra legacy` 安装，不影响主监控的首次启动速度。

**飞书推送配置优先级：**

1. 环境变量 `FEISHU_WEBHOOK_URL`
2. 项目根目录 `.env.local` 中的 `FEISHU_WEBHOOK_URL`
3. 都没配置时，脚本会跳过飞书推送并在终端提示

## 定时调度

配在 OpenClaw cron：每周一~五 18:30 (Asia/Shanghai) 自动执行，结果推飞书。

Job id: `656d2346-16c5-4d00-a3ed-e4a9589f3e14`
