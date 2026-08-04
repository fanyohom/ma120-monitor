# ma120-monitor

基于 **MA120 均线** 的持仓 & 关注池监控脚本，每日通过飞书推送买入/卖出信号。

## 策略

- **买入线：** 股价 < MA120 × 0.88
- **卖出线：** 股价 > MA120 × 1.12

数据源：新浪财经日 K（HTTP）。本地 JSON 缓存在 `.stock_cache/`，避免重复请求。

## 文件结构

```
ma120-monitor/
├── stock_dynamic_monitor.py     # 主脚本（持仓 + 关注池，飞书推送）
├── stock_monitor.py             # 旧版（保留备查）
├── stock_portfolio_monitor.py   # 旧版（保留备查）
├── backtest_ma120_strategy.py   # MA120 策略回测工具
├── stocks.xlsx                  # 数据源（sheet: portfolio / watchlist）
└── .stock_cache/                # 日 K 行情缓存（一只一个 JSON）
```

## 用法

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
uv run --with pandas --with openpyxl --with requests python stock_dynamic_monitor.py

# 只看终端，不发飞书
uv run --with pandas --with openpyxl --with requests python stock_dynamic_monitor.py --no-feishu

# 只看持仓
uv run --with pandas --with openpyxl --with requests python stock_dynamic_monitor.py --portfolio --no-feishu

# 只看关注池
uv run --with pandas --with openpyxl --with requests python stock_dynamic_monitor.py --watchlist --no-feishu
```

### macOS / Linux

```bash
cd /path/to/ma120-monitor
export PYTHONUTF8=1
export UV_CACHE_DIR="$PWD/.uv-cache"

# 持仓 + 关注池（默认推送飞书卡片）
uv run --with pandas --with openpyxl --with requests python stock_dynamic_monitor.py

# 只看终端，不发飞书
uv run --with pandas --with openpyxl --with requests python stock_dynamic_monitor.py --no-feishu
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

**依赖：** 脚本通过 `uv run --with ...` 自动准备 `pandas/openpyxl/requests`。

**飞书推送配置优先级：**

1. 环境变量 `FEISHU_WEBHOOK_URL`
2. 项目根目录 `.env.local` 中的 `FEISHU_WEBHOOK_URL`
3. 都没配置时，脚本会跳过飞书推送并在终端提示

## 定时调度

配在 OpenClaw cron：每周一~五 18:30 (Asia/Shanghai) 自动执行，结果推飞书。

Job id: `656d2346-16c5-4d00-a3ed-e4a9589f3e14`
