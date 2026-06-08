# stock_data_collector / stock_data_ingestion

A 股与港股通日频结构化数据获取工具。它是量化系统的数据底座，不是交易 Agent：负责从 Tushare Pro、AKShare（东方财富 + 腾讯）、BaoStock 等数据源获取数据，保存 raw payload，标准化字段，做多源交叉校验，记录冲突，计算质量分，并把可追溯的数据写入 SQLite / Parquet，供后续 Agent、因子系统和回测系统查询。

当前代码版本：`0.3.0`。AKShare adapter：`0.4.0`。

测试基准：完整依赖环境 `python -m pytest -q` 为 **105 passed**。在缺少 `pyarrow` / `SQLAlchemy` 的轻量环境里，Parquet 与部分 SQLite/QueryService 测试会被 skip，可能看到类似 `99 passed, 6 skipped`。

---

## 0. 给运维 Agent 的 TL;DR

你是调度 Agent，目标是每天盘后自动拉取“当天 / 前一交易日”的数据，并在凭证、Cookie、落盘、质量冲突或供应商异常时提醒用户。

记住 6 件事：

1. **工作目录**：`tools/stock_data_collector/`。
2. **环境**：先激活项目所用的 Python 虚拟环境（需包含核心依赖与所需 provider SDK），不要擅自新建 venv：
   ```bash
   source <你的虚拟环境>/bin/activate
   ```
3. **CLI 入口**：
   ```bash
   python -m stock_data_ingestion.cli <group> <command>
   ```
   不确定参数时使用 `--help`，例如：
   ```bash
   python -m stock_data_ingestion.cli fetch historical-bars --help
   ```
4. **全局 `--config-dir` 必须放在子命令前面**，推荐所有生产/测试命令都显式传入：
   ```bash
   python -m stock_data_ingestion.cli --config-dir config fetch historical-bars --help
   ```
5. **跑数据前先检查 Cookie，跑完后查健康摘要**：
   ```bash
   python -m stock_data_ingestion.cli --config-dir config verify eastmoney-cookie
   python -m stock_data_ingestion.cli --config-dir config query meta-summary --ticker 600519.SH
   ```
6. **每条 `fetch` 都输出一个 `StockDataResponse` JSON**。Agent 判断成败至少检查：
   - `status`
   - `errors[].error_code`
   - `persistence.saved`
   - `provider_results[]`
   - `quality_report.conflicts[].severity`

最小每日序列如下，适合滚动更新。多数行情类命令不传日期时，会按配置自动回看窗口并幂等去重。

```bash
CFG="config"
U="600519.SH 000001.SZ"   # 当日股票池示例；生产中应替换为真实股票池

python -m stock_data_ingestion.cli --config-dir "$CFG" verify eastmoney-cookie || echo "Eastmoney Cookie may need refresh"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch trading-status  --tickers $U
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch historical-bars --tickers $U --frequency 1d --adjust none --cross-validate
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch adj-factor      --tickers $U --cross-validate
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch valuation       --tickers $U
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch money-flow     --tickers $U

python -m stock_data_ingestion.cli --config-dir "$CFG" query meta-summary --ticker 600519.SH
```

关键纪律：**Tushare 是 canonical provider；AKShare / BaoStock 只做验证、补充、fallback 或 provider-specific 保存，不能因为辅源不一致就无痕覆盖 Tushare。** 出现 `high` / `critical` 冲突时，Agent 应提醒用户复核，不要自行改数据。给下游使用时优先走 `trading-ready` 查询。

---

## 1. 程序定位与边界

本工具只做四件事：**获取 → 校验 → 存储 → 查询**。

明确不做：

- 不做 LLM Agent 决策；
- 不生成投资建议；
- 不交易、不下单、不接券商接口；
- 不做新闻舆情采集；
- 不把原始大字段塞进 SQLite；
- 不把不同供应商口径的数据强行合并为唯一真值；
- 不自动用多数投票覆盖 canonical provider。

第一阶段范围是“日频盘后因子系统”：

- 股票池：全 A 股正常上市股票，包括主板、创业板、科创板、北交所；不含 ETF / 可转债；默认不维护退市股；外加港股通股票本身的港股日线。
- 数据类型：基础信息、交易日历、交易状态、A 股日线、港股通港股日线、复权因子、估值、财报、财务指标、资金流、分红送转、指数成分。
- 窗口：行情、估值、复权、资金流默认回看 `market_data_lookback_days = 400` 自然日；财务默认覆盖 `financial_lookback_quarters = 8` 个季度。
- 复权：标准层只存未复权 OHLCV + `adj_factor`；`qfq` / `hfq` 在查询层按需动态计算。
- 默认不是分钟线、tick、Level-2 或实时盘中系统。

---

## 2. 安装与环境

进入项目目录并激活项目所用的 Python 虚拟环境（需 Python >=3.11，包含核心依赖、`pytest` 及所需 provider SDK：`tushare` / `akshare` / `baostock` / `jqdatasdk`）。**不要新建 venv**；若发现缺依赖，先与维护者确认再安装：

```bash
cd tools/stock_data_collector
source <你的虚拟环境>/bin/activate
```

如需在其它环境从源码安装开发依赖：

```bash
pip install -e .[dev,providers]
```

只运行不依赖真实外部 API 的单元测试时：

```bash
pip install -e .[dev]
python -m pytest -q
```

凭证写入 `.env`，不要提交真实 `.env`：

```bash
cp .env.example .env
```

`.env` 会被 CLI 自动加载。CLI 当前没有 `--env-file` 参数；如需指定 env 文件，使用环境变量：

```bash
export STOCK_DATA_ENV_FILE=/path/to/.env
```

自动发现顺序大致为：

1. `STOCK_DATA_ENV_FILE`；
2. `--config-dir DIR` 对应的 `DIR/.env` 与 `DIR/../.env`；
3. 当前目录及父目录中的 `.env` / `config/.env`；
4. 包根目录附近的 `.env` / `config/.env`。

默认不覆盖已有非空环境变量。需要覆盖时：

```bash
export STOCK_DATA_ENV_OVERRIDE=true
```

需要禁用自动加载时：

```bash
export STOCK_DATA_DISABLE_ENV_AUTOLOAD=true
```

关键环境变量：

| 变量 | 用途 | 必需性 |
|---|---|---|
| `TUSHARE_TOKEN` | Tushare Pro token | 主流程必需 |
| `EASTMONEY_COOKIE` | 东方财富 Cookie，AKShare 资金流 fallback 用 | 资金流走东财时需要 |
| `EASTMONEY_USER_AGENT` | 与 Cookie 对应的 User-Agent | 可选，建议复制同一浏览器请求头 |
| `EASTMONEY_ACCEPT_LANGUAGE` | Eastmoney 请求语言 | 可选 |
| `JQDATA_USERNAME` / `JQDATA_PASSWORD` | JoinQuant/JQData 账号 | 默认不用，仅启用 JQData 时需要 |
| `STOCK_DATA_CONFIG_DIR` | 配置目录 | 可选 |
| `STOCK_DATA_SQLITE_PATH` | SQLite 路径覆盖 | 仅未显式传 `--config-dir` 时生效 |
| `STOCK_DATA_PROVIDERS` | 启用 provider 硬白名单 | 可选，常用 `tushare,akshare,baostock` |
| `STOCK_DATA_CANONICAL_PROVIDER` | canonical provider | 推荐 `tushare` |
| `STOCK_DATA_DISABLED_PROVIDERS` | 禁用 provider | 推荐 `joinquant` |
| `STOCK_DATA_PROVIDERS_<REQUEST_TYPE>` | 按请求类型覆盖 provider | 可选，例如 `STOCK_DATA_PROVIDERS_MONEY_FLOW=tushare,akshare` |

---

## 3. 配置文件

默认配置目录是 `config/`。也可以通过 `--config-dir DIR` 或 `STOCK_DATA_CONFIG_DIR` 指定。

注意：显式传入 `--config-dir` 后，该目录下的 `storage.yaml` 优先于 `.env` 中的 `STOCK_DATA_SQLITE_PATH`。这可以避免 smoke test 误写默认生产库。

### 3.1 `config/data_sources.yaml`

典型配置：

```yaml
canonical_provider: tushare
active_providers:
  - tushare
  - akshare
  - baostock
market_data_lookback_days: 400
financial_lookback_quarters: 8
default_daily_update_time: "20:30"
allow_majority_override_canonical: false
minimum_quality_for_trading_use: 0.80
```

主要 request type provider override：

```yaml
security_master: [tushare, baostock, akshare]
trade_calendar: [tushare, baostock]
trading_status: [tushare, baostock, akshare]
historical_bars: [tushare, akshare, baostock]
adj_factor: [tushare, baostock]
valuation_metric: [tushare, baostock, akshare]
financial_statement: [tushare, baostock]
financial_indicator: [tushare, baostock]
money_flow: [tushare, akshare]
corporate_action: [tushare, baostock]
industry_concept: [akshare, baostock]
index_data: [tushare, baostock]
```

Provider 别名会自动规范化，例如：

```text
tushare / ts / thushare
akshare / ak / tencent
baostock / bao_stock / bs
joinquant / jqdata / jq
```

### 3.2 `config/storage.yaml`

默认存储：

```yaml
sqlite_path: data/stock_data.db
enable_wal: true
raw_object_root: data/raw_objects
parquet_root: data/parquet
compress_raw_payload: true
raw_format: jsonl.gz
timezone: Asia/Shanghai
log_path: logs/stock_data_ingestion.log
```

### 3.3 `config/data_quality.yaml`

用于配置：

- 字段容忍阈值；
- provider 可靠性初始分；
- 质量评分权重；
- 冲突严重度规则；
- 关键字段列表；
- 可由 supplement provider 补充的字段白名单。

默认 trading-ready 阈值来自 `minimum_quality_for_trading_use = 0.80`。

---

## 4. 数据源策略

| 数据源 | 默认角色 | 用途 | 认证 | 主要边界 |
|---|---|---|---|---|
| Tushare Pro | canonical | A 股主记录、交易日历、日线、港股通港股日线、复权因子、估值、财务、资金流、公司行动 | `TUSHARE_TOKEN` | 受 token、积分、接口权限影响 |
| AKShare（东方财富 + 腾讯） | validator + supplement | A 股行情交叉验证、腾讯 fallback、资金流、行业/概念、部分实时快照 | 通常无；东财资金流建议 Cookie | 东财接口可能触发浏览器校验；无独立复权因子表 |
| BaoStock | validator + supplement | SH/SZ A 股基础资料、交易日历、日线、估值、复权、季频财务、行业、指数成分、分红 | SDK `bs.login()` | 不作为港股源；无个股 money-flow |
| JoinQuant/JQData | optional future provider | 未来研究验证 | 账号密码 | 默认关闭，Agent 不应默认启用 |

铁律：

- `allow_majority_override_canonical` 默认且应保持 `false`。
- 辅源永不无痕覆盖 Tushare。
- 主源缺字段时，只能按补充字段白名单补缺。
- 行业、概念、资金流等天然口径差异大的数据，优先 provider-specific 保存，不强行合成唯一真值。
- 港股通港股日线只走 Tushare HK 接口；AKShare / BaoStock 不作为当前港股日线源。

---

## 5. Eastmoney Cookie 运维

AKShare 通常不需要登录，但东方财富部分接口会触发浏览器验证。个股资金流尤其常见。当前策略：

1. `AKShareAdapter.fetch_money_flow()` 先尝试 AKShare 原生 `stock_individual_fund_flow`；
2. 请求 Eastmoney 时注入 `EASTMONEY_COOKIE`、UA、Referer 等浏览器请求头；
3. 如果原生路径失败且存在 Cookie，则 fallback 到 direct Eastmoney `fflow/daykline`；
4. Cookie 过期时，资金流可能失败，并提示类似 “Eastmoney browser cookie may be expired”。

### 5.1 更新 Cookie

1. 浏览器打开：
   ```text
   https://data.eastmoney.com/zjlx/detail.html
   ```
2. 如果出现验证/验证码，手动完成验证。
3. 同一浏览器打开校验 API，确认能返回 JSON：
   ```text
   https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=1.600519&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65&ut=b2884a393a59ad64002292a3e90d46a5
   ```
4. DevTools → Network → 选中该请求 → Headers → Request Headers → 复制 `Cookie:` 后面的整串值，或复制完整 “Copy as cURL”。
5. 写入 `.env`：
   ```bash
   python tools/update_eastmoney_cookie.py --cookie '<cookie 或 curl>'
   ```
   也支持从管道输入：
   ```bash
   cat cookie.txt | python tools/update_eastmoney_cookie.py
   ```

### 5.2 验证 Cookie

```bash
python -m stock_data_ingestion.cli --config-dir config verify eastmoney-cookie
```

也可指定测试股票和日期：

```bash
python -m stock_data_ingestion.cli --config-dir config verify eastmoney-cookie \
  --ticker 600519.SH \
  --start-date 2026-06-01 \
  --end-date 2026-06-05
```

解释：

- 输出 `true`，退出码 `0`：Cookie 当前可用。
- 输出 `false`，退出码 `1`：Cookie 缺失、失效、验证未通过或接口失败。

Agent 职责：每日跑资金流前先验证 Cookie；失败时提醒用户刷新 Cookie，本轮可以跳过或标记 `money_flow`，其余 Tushare / BaoStock 任务仍可继续。Agent 绝不能伪造 Cookie，不能绕过验证码，不能把完整 Cookie 写入日志或回复。

---

## 6. CLI 总览

全局帮助：

```bash
python -m stock_data_ingestion.cli --help
python -m stock_data_ingestion.cli --config-dir config init-db
python -m stock_data_ingestion.cli --config-dir config fetch --help
python -m stock_data_ingestion.cli --config-dir config query --help
python -m stock_data_ingestion.cli --config-dir config verify --help
```

当前 `fetch` 命令：

```text
security-master
trade-calendar
historical-bars
valuation / valuation-metric
adj-factor
financial-indicator
financial-statement
money-flow
trading-status
corporate-action
```

当前 `query` 命令：

```text
bars
conflicts
meta-summary
```

当前 `verify` 命令：

```text
raw
eastmoney-cookie
```

### 6.1 初始化数据库

```bash
python -m stock_data_ingestion.cli --config-dir config init-db
```

### 6.2 基础信息

```bash
python -m stock_data_ingestion.cli --config-dir config fetch security-master \
  --tickers 600519.SH 000001.SZ \
  --providers tushare baostock akshare \
  --canonical-provider tushare
```

`--tickers` 可为空；具体是否返回全市场取决于 provider adapter 实现与权限。

### 6.3 交易日历

`trade-calendar` 必须传日期范围：

```bash
python -m stock_data_ingestion.cli --config-dir config fetch trade-calendar \
  --exchanges SSE SZSE BSE \
  --start-date 2026-01-01 \
  --end-date 2026-12-31 \
  --providers tushare baostock \
  --canonical-provider tushare
```

兼容单交易所：

```bash
python -m stock_data_ingestion.cli --config-dir config fetch trade-calendar \
  --exchange SSE \
  --start-date 2026-06-01 \
  --end-date 2026-06-05
```

### 6.4 日线 / 周线 / 月线 / 分钟线行情

```bash
python -m stock_data_ingestion.cli --config-dir config fetch historical-bars \
  --tickers 600519.SH 000001.SZ \
  --start-date 2026-06-01 \
  --end-date 2026-06-05 \
  --frequency 1d \
  --adjust raw \
  --providers tushare akshare baostock \
  --canonical-provider tushare \
  --cross-validate
```

说明：

- `--adjust raw` 是 CLI alias，内部规范化为 `none`。
- 标准层建议保存未复权数据；`qfq` / `hfq` 查询层动态计算。
- `--start-date` 省略时默认按 `market_data_lookback_days` 回看。
- `--end-date` 省略时默认今天。
- 港股通港股使用 `.HK` 代码，当前主要通过 Tushare HK 日线。

### 6.5 复权因子

```bash
python -m stock_data_ingestion.cli --config-dir config fetch adj-factor \
  --tickers 600519.SH \
  --start-date 2026-06-01 \
  --end-date 2026-06-05 \
  --providers tushare baostock \
  --canonical-provider tushare \
  --cross-validate
```

### 6.6 估值

```bash
python -m stock_data_ingestion.cli --config-dir config fetch valuation \
  --tickers 600519.SH \
  --start-date 2026-06-01 \
  --end-date 2026-06-05
```

`valuation-metric` 是 `valuation` 的 alias。

### 6.7 资金流

```bash
python -m stock_data_ingestion.cli --config-dir config fetch money-flow \
  --tickers 600519.SH \
  --start-date 2026-06-01 \
  --end-date 2026-06-05 \
  --providers tushare akshare \
  --canonical-provider tushare
```

资金流建议先运行：

```bash
python -m stock_data_ingestion.cli --config-dir config verify eastmoney-cookie
```

### 6.8 交易状态

```bash
python -m stock_data_ingestion.cli --config-dir config fetch trading-status \
  --tickers 600519.SH 000001.SZ \
  --start-date 2026-06-01 \
  --end-date 2026-06-05
```

包含停复牌、ST、涨跌停、可交易状态等。

### 6.9 公司行动

```bash
python -m stock_data_ingestion.cli --config-dir config fetch corporate-action \
  --tickers 600519.SH \
  --start-date 2026-01-01 \
  --end-date 2026-06-08 \
  --action-types dividend share_float repurchase \
  --event-date-field ann_date
```

`--action-types` 可选：`dividend`、`share_float`、`repurchase`。

`--event-date-field` 可选：`ann_date`、`record_date`、`ex_date`、`imp_ann_date`、`pay_date`、`div_listdate`、`base_date`、`end_date`。

### 6.10 财务指标

```bash
python -m stock_data_ingestion.cli --config-dir config fetch financial-indicator \
  --tickers 600519.SH \
  --start-date 2025-01-01 \
  --end-date 2026-06-08
```

省略日期时默认覆盖最近 `financial_lookback_quarters` 个季度附近窗口。

### 6.11 财务报表

```bash
python -m stock_data_ingestion.cli --config-dir config fetch financial-statement \
  --tickers 600519.SH \
  --statement-types income balancesheet cashflow \
  --period 20250331
```

`--statement-types` 支持：

```text
income
balancesheet
cashflow
income_statement
balance_sheet
cash_flow
```

`--period` 设置后，Tushare 会优先按报告期查询；否则按公告日期窗口查询。

### 6.12 查询行情

```bash
python -m stock_data_ingestion.cli --config-dir config query bars \
  --ticker 600519.SH \
  --start-date 2026-06-01 \
  --end-date 2026-06-05 \
  --frequency 1d \
  --adjust raw
```

给因子 / 回测用的 trading-ready qfq 查询：

```bash
python -m stock_data_ingestion.cli --config-dir config query bars \
  --ticker 600519.SH \
  --start-date 2026-06-01 \
  --end-date 2026-06-05 \
  --frequency 1d \
  --adjust qfq \
  --trading-ready \
  --minimum-quality 0.80
```

`trading-ready` 会排除隔离记录，并要求 `data_quality >= minimum_quality`。

### 6.13 查询冲突

```bash
python -m stock_data_ingestion.cli --config-dir config query conflicts
python -m stock_data_ingestion.cli --config-dir config query conflicts --ticker 600519.SH
```

### 6.14 查询健康摘要

```bash
python -m stock_data_ingestion.cli --config-dir config query meta-summary --ticker 600519.SH
```

测试目录统计可传：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" query meta-summary \
  --ticker "$TICKER" \
  --test-root "$TEST_ROOT"
```

### 6.15 验证 raw 完整性

```bash
python -m stock_data_ingestion.cli --config-dir config verify raw \
  --raw-payload-id raw_tushare_historical_bars_20260605_req_xxx
```

也可以传 `raw://local/...` 引用：

```bash
python -m stock_data_ingestion.cli --config-dir config verify raw \
  --raw-payload-id 'raw://local/provider=tushare/request_type=historical_bars/date=2026-06-05/raw_xxx.jsonl.gz'
```

如果知道期望 hash：

```bash
python -m stock_data_ingestion.cli --config-dir config verify raw \
  --raw-payload-id raw_tushare_historical_bars_20260605_req_xxx \
  --expected-hash 'sha256:...'
```

未传 `--expected-hash` 时，命令会优先从 SQLite `raw_payload_index` 查 `raw_hash`，失败时回退到 raw 文件 metadata。输出形如：

```json
{
  "raw_payload_id": "...",
  "raw_payload_ref": "raw://local/...",
  "computed_hash": "sha256:...",
  "expected_hash": "sha256:...",
  "verified": true
}
```

---

## 7. Agent 每日运维手册

盘后建议时间：`default_daily_update_time = 20:30`，按 `Asia/Shanghai` 理解；如果外层调度器使用 `Asia/Tokyo`，相当于日本时间 21:30。

### 7.1 目标交易日判断

本工具不会自动判断“今天是否交易日”，只按传入日期范围或滚动窗口取数。外层 Agent / 调度器负责决定目标日期。

推荐规则：

1. 先维护或查询 `trade_calendar`。
2. 如果当前时间已过 `Asia/Shanghai 20:30`，且今天是目标市场交易日，则 `TARGET_DATE = 今天`。
3. 否则 `TARGET_DATE = 最近一个已开市交易日`。
4. 如果用户要求“当天/前一交易日精准单日”，Agent 应显式传：
   ```bash
   --start-date "$TARGET_DATE" --end-date "$TARGET_DATE"
   ```
5. 如果用户要求“滚动补齐”，可省略日期，让工具按 400 天 / 8 季窗口幂等更新。

### 7.2 首次仅一次

```bash
python -m stock_data_ingestion.cli --config-dir config init-db
```

### 7.3 每日推荐顺序

```bash
CFG="config"
TARGET_DATE="2026-06-08"
U="600519.SH 000001.SZ"

# 0) 凭证体检
python -m stock_data_ingestion.cli --config-dir "$CFG" verify eastmoney-cookie || echo "Eastmoney Cookie may need refresh"

# 1) 低频：基础信息 + 交易日历
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch security-master --tickers $U
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch trade-calendar --exchanges SSE SZSE BSE \
  --start-date "2026-01-01" --end-date "$TARGET_DATE"

# 2) 交易状态
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch trading-status --tickers $U \
  --start-date "$TARGET_DATE" --end-date "$TARGET_DATE"

# 3) 行情
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch historical-bars --tickers $U \
  --start-date "$TARGET_DATE" --end-date "$TARGET_DATE" \
  --frequency 1d --adjust none --cross-validate

# 4) 复权因子
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch adj-factor --tickers $U \
  --start-date "$TARGET_DATE" --end-date "$TARGET_DATE" --cross-validate

# 5) 估值 / 资金流 / 公司行动
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch valuation --tickers $U \
  --start-date "$TARGET_DATE" --end-date "$TARGET_DATE"
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch money-flow --tickers $U \
  --start-date "$TARGET_DATE" --end-date "$TARGET_DATE"
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch corporate-action --tickers $U \
  --start-date "$TARGET_DATE" --end-date "$TARGET_DATE"

# 6) 财务：季频，可每天跑，也可财报季加密
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch financial-indicator --tickers $U
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch financial-statement --tickers $U

# 7) 收尾健康检查
python -m stock_data_ingestion.cli --config-dir "$CFG" query meta-summary --ticker 600519.SH
python tools/daily_mvp_smoke.py --config-dir "$CFG" --as-of "$TARGET_DATE"
```

### 7.4 Agent 决策要点

- `verify eastmoney-cookie` 返回 false：提醒用户刷新 Cookie，本轮资金流可跳过或标记失败；其他数据照常跑。
- `TOKEN_MISSING` / `AUTH_FAILED` / `PERMISSION_DENIED`：提醒补充或更换 `TUSHARE_TOKEN`，并确认接口权限。
- `RATE_LIMITED`：降速、分批、稍后重试。
- `PROVIDER_UNAVAILABLE` / `PROVIDER_TIMEOUT`：记录并稍后重试，其余源继续。
- `EMPTY_RESULT`：不一定是错误，非交易日、停牌、事件型数据无事件、短财务窗口都可能为空。
- `provider_fetch_summary` 某源长期失败：提醒维护者。
- `conflict_summary` 或 `quality_report.conflicts` 出现 `high` / `critical`：提醒人类复核，不要改数据。

---

## 8. 理解 `StockDataResponse`

每条 `fetch` 向 stdout 打印一个 JSON，结构示意如下：

```jsonc
{
  "schema_version": "stock_data_response.v0.1",
  "request_id": "req_xxx",
  "status": "success",                  // success | partial_success | failed
  "created_at": "...",
  "completed_at": "...",
  "timezone": "Asia/Shanghai",
  "canonical_provider": "tushare",
  "request": { "request_type": "historical_bars", "...": "..." },
  "provider_results": [
    {
      "provider": "tushare",
      "source_api": "daily",
      "status": "success",
      "rows_fetched": 21,
      "raw_payload_id": "raw_tushare_historical_bars_...",
      "raw_payload_ref": "raw://local/...",
      "raw_hash": "sha256:...",
      "error": null
    }
  ],
  "provider_comparisons": [],
  "data": {
    "bars": [],
    "valuation_metrics": [],
    "money_flow": [],
    "securities": []
  },
  "quality_report": {
    "data_quality_score": 0.94,
    "completeness_score": 1.0,
    "consistency_score": 1.0,
    "conflicts": [
      { "severity": "high", "field_name": "close", "...": "..." }
    ],
    "warnings": []
  },
  "persistence": {
    "saved": true,
    "tables_written": ["daily_bars", "source_fetch_logs", "raw_payload_index"],
    "parquet_refs": [],
    "raw_payload_ids": ["..."],
    "raw_payload_refs": ["raw://local/..."]
  },
  "errors": [
    {
      "error_code": "RATE_LIMITED",
      "provider": "tushare",
      "error_message": "...",
      "retryable": true,
      "suggested_action": "..."
    }
  ]
}
```

Agent 每条 fetch 后按这个顺序解析：

1. 读 `status`：
   - `success`：正常；
   - `partial_success`：部分源失败或非致命问题，继续但记录；
   - `failed`：该数据类型未成功获取，按错误码处置。
2. 读 `errors[].error_code`：判断是否需要用户介入。
3. 读 `provider_results[]`：确认哪些源成功、失败、空结果或不可用。
4. 读 `persistence.saved` 与 `tables_written`：确认确实落库。
5. 读 `quality_report.conflicts[].severity`：`high` / `critical` 必须提醒。
6. 需要审计时记录 `persistence.raw_payload_ids`，再用 `verify raw` 校验完整性。

不要只看 shell 退出码。某些非关键源失败会让整体变成 `partial_success`，但主源数据可能已经成功落库。

其它命令输出：

- `query bars` / `query conflicts`：JSON records 数组。
- `query meta-summary`：全库清单、行数、冲突、磁盘和 provider 摘要。
- `verify eastmoney-cookie`：`true` / `false`。
- `verify raw`：`computed_hash`、`expected_hash`、`verified`。

---

## 9. 查询数据

### 9.1 CLI 查询

未复权原始行情：

```bash
python -m stock_data_ingestion.cli --config-dir config query bars \
  --ticker 600519.SH \
  --start-date 2024-01-01 \
  --end-date 2026-05-29 \
  --frequency 1d \
  --adjust none
```

给因子 / 回测用的 trading-ready 前复权视图：

```bash
python -m stock_data_ingestion.cli --config-dir config query bars \
  --ticker 600519.SH \
  --start-date 2024-01-01 \
  --end-date 2026-05-29 \
  --frequency 1d \
  --adjust qfq \
  --trading-ready \
  --minimum-quality 0.80
```

查冲突：

```bash
python -m stock_data_ingestion.cli --config-dir config query conflicts --ticker 600519.SH
```

查健康摘要：

```bash
python -m stock_data_ingestion.cli --config-dir config query meta-summary --ticker 600519.SH
```

### 9.2 Python 查询

CLI 是首选。需要 Python 调用时：

```python
from stock_data_ingestion.storage.database import Database
from stock_data_ingestion.services.query_service import QueryService

with Database("data/stock_data.db").session() as session:
    qs = QueryService(session)
    bars = qs.get_trading_ready_bars(
        "600519.SH",
        "2024-01-01",
        "2026-05-29",
        frequency="1d",
        adjust="qfq",
        minimum_quality=0.80,
    )
    conflicts = qs.get_conflicts("600519.SH")
```

QueryService 能力包括：基础信息、行情、trading-ready bars、动态复权、估值、财务指标、行业/概念、资金流、指数成分、溯源查询、隔离状态查询等。返回 pandas DataFrame，并保留来源字段。

---

## 10. `meta-summary` 快速盘点

```bash
python -m stock_data_ingestion.cli --config-dir config query meta-summary --ticker 600519.SH
```

返回节选：

```jsonc
{
  "ticker": "600519.SH",
  "sqlite_path": "data/stock_data.db",
  "daily_bar_trading_days": 480,
  "daily_bar_date_range": {"min": "2024-01-02", "max": "2026-06-06"},
  "rows_by_table": {
    "daily_bars": 12345,
    "valuation_metrics": 4567,
    "money_flow": 4567
  },
  "standard_rows_total": 99999,
  "metadata_rows_total": 888,
  "disk_usage_bytes": {
    "sqlite": 123,
    "raw_objects": 456,
    "parquet": 789,
    "logs": 10
  },
  "provider_fetch_summary": [
    {"provider": "tushare", "source_api": "daily", "status": "success", "calls": 3, "rows_fetched": 63}
  ],
  "provider_comparison_summary": [
    {"record_type": "bar", "compared_provider": "akshare", "status": "matched", "comparisons": 42}
  ],
  "conflict_summary": [
    {"record_type": "bar", "field_name": "close", "severity": "high", "conflicts": 1}
  ]
}
```

Agent 用它判断：

- 当天是否落库：看 `daily_bar_date_range.max` 与 `rows_by_table`。
- 哪些源失败或空结果：看 `provider_fetch_summary`。
- 是否有高严重度冲突：看 `conflict_summary`。
- 数据/日志/Parquet/raw 是否异常膨胀：看 `disk_usage_bytes`。

---

## 11. 数据结构总览

### 11.1 request / record / 表映射

| request_type / record_type | 响应桶 `data.*` | SQLite 表 | Parquet data_type | 业务唯一键核心 |
|---|---|---|---|---|
| `security_master` | `securities` | `securities` | `securities` | `normalized_ticker + effective_provider` |
| `trade_calendar` | `trade_calendar` | `trade_calendar` | `trade_calendar` | `exchange + calendar_date + effective_provider` |
| `trading_status` | `trading_status` | `trading_status` | `trading_status` | `normalized_ticker + trade_date + effective_provider` |
| `historical_bars` 1d | `bars` | `daily_bars` | `bars` | `normalized_ticker + frequency + trade_date + adjust + effective_provider` |
| `historical_bars` 1w/1mo | `bars` | `weekly_bars` | `bars` | 同上 |
| `historical_bars` 分钟 | `bars` | `minute_bars` | `bars` | 同上 |
| `realtime_quote` | `realtime_quotes` | `realtime_quotes` | `realtime_quotes` | `normalized_ticker + quote_time_bucket + effective_provider` |
| `adj_factor` | `adj_factors` | `adj_factors` | `adj_factors` | `normalized_ticker + trade_date + effective_provider` |
| `financial_statement` | `financial_statements` | `financial_statements` | `financial_statements` | `normalized_ticker + report_period + statement_type + report_type + announcement_date + effective_provider` |
| `financial_indicator` | `financial_indicators` | `financial_indicators` | `financial_indicators` | `normalized_ticker + report_period + effective_provider` |
| `valuation_metric` | `valuation_metrics` | `valuation_metrics` | `valuation_metrics` | `normalized_ticker + trade_date + effective_provider` |
| `industry_concept` | `industry_memberships` / `concept_memberships` | 同名 | 同名 | provider-specific |
| `money_flow` | `money_flow` | `money_flow` | `money_flow` | `normalized_ticker + trade_date + effective_provider` 或 source methodology |
| `index_data` | `indices` / `index_bars` / `index_constituents` | 同名 | 同名 | 见各表 |
| `corporate_action` | `corporate_actions` | `corporate_actions` | `corporate_actions` | `normalized_ticker + action_type + announcement_date + ex_date + effective_provider` |

元数据表包括：`source_fetch_logs`、`provider_comparisons`、`data_quality_conflicts`、`raw_payload_index`、`ingestion_requests`、`ingestion_runs`、`ticker_mappings` 等。

### 11.2 标准记录公共字段

所有标准记录都带：

```text
record_id
record_type
schema_version
adapter_version
provider
effective_provider
canonical_provider
source_api
source_site
source_role
merge_method
validation_status
conflict_ids
canonical_value_suspect
supplement_flags
data_quality
quality_flags
field_provenance
raw_payload_id
raw_payload_ref
raw_hash
raw_row_index
raw_format
request_id
ingestion_run_id
idempotency_key
request_params_hash
fetch_time
ingested_at
```

涉股记录还包含：

```text
normalized_ticker
provider_symbol
exchange
market
asset_type
```

涉时记录统一使用 `Asia/Shanghai` 语义；涉金额字段保留 `currency`；行情记录含 `adjust`。

### 11.3 常用记录字段

BarRecord：`trade_date`、`timestamp`、`frequency`、`is_complete`、`open/high/low/close`、`pre_close`、`change`、`pct_change`、`volume`、`amount`、`vwap`、`turnover_rate`、`turnover_rate_free_float`、`adjust`、`adj_factor`。

TradingStatusRecord：`trade_date`、`is_trading`、`is_suspended`、`is_st`、`is_star_st`、`has_delisting_risk`、`limit_up_price`、`limit_down_price`、`hit_limit_up`、`hit_limit_down`、`tradability_status`。

ValuationMetricRecord：`trade_date`、`pe`、`pe_ttm`、`pb`、`ps`、`ps_ttm`、`pcf_ncf_ttm`、`dividend_yield`、`total_market_value`、`float_market_value`、`turnover_rate`、`volume_ratio`、`amount`。

AdjFactorRecord：`trade_date`、`adj_factor`，以及 BaoStock 事件型因子字段 `fore_adjust_factor`、`back_adjust_factor`、`event_adjust_factor`、`factor_method`。

FinancialIndicatorRecord：`report_period`、`roe`、`roa`、`gross_margin`、`net_margin`、`revenue_yoy`、`net_profit_yoy`、`debt_asset_ratio`、`current_ratio`、`ocf_to_net_profit`、`eps`、`bps`。

MoneyFlowRecord：`trade_date`、`main_net_inflow`、`super_large_net_inflow`、`large_net_inflow`、`medium_net_inflow`、`small_net_inflow`、`main_net_inflow_ratio`、`source_methodology`。

---

## 12. 存储、raw 与幂等

### 12.1 幂等

`idempotency_key` 不包含 `request_id`，而由 request type、ticker、日期范围、frequency、adjust、fields、provider set 等语义参数生成。

完全相同语义的成功请求再次执行时，runner 会跳过重复写入，可能返回 warning：

```text
idempotency_key already succeeded; skipped duplicate write
```

### 12.2 Raw Object Store

原始响应存 gzip JSON Lines（`.jsonl.gz`）：第一行 metadata，后续每行一条 raw record。

典型布局：

```text
data/raw_objects/provider=<provider>/request_type=<type>/date=<YYYY-MM-DD>/<raw_payload_id>.jsonl.gz
```

`raw_payload_ref` 形如：

```text
raw://local/provider=tushare/request_type=historical_bars/date=2026-06-05/raw_xxx.jsonl.gz
```

设计原则：

- raw 是审计留痕，不覆盖；
- SQLite 只存 `raw_payload_index`，不存大字段；
- 完全相同语义的成功请求通常被幂等检查拦住，不会新增 raw；
- 新语义请求、失败后重试或不同 idempotency key 可能新增 raw 文件。

### 12.3 SQLite

SQLite 使用 SQLAlchemy 2.x，开启 WAL。标准表使用业务唯一键与 insert-skip-duplicate。重复业务键不会更新旧行，也不会无限增行。

典型唯一键：

- bars：`normalized_ticker + frequency + trade_date + timestamp + adjust + effective_provider`
- adj_factors：`normalized_ticker + trade_date + effective_provider`
- valuation_metrics：`normalized_ticker + trade_date + effective_provider`
- money_flow：`normalized_ticker + trade_date + frequency + source_methodology + provider/effective_provider`

### 12.4 Parquet

Parquet 只存清洗后数据。写入时读取旧分区，合并新数据，按 business key 去重，然后重写对应 `part-000.parquet`。

典型分区：

```text
bars/frequency=1d/trade_date=2026-06-05/effective_provider=tushare/part-000.parquet
```

如果 `pyarrow` 缺失，SQLite/raw 可能成功，但 Parquet 导出会失败或 response 变为 `partial_success`。

---

## 13. 校验、合并与质量评分

### 13.1 标准化后比较

比较前会统一：

- ticker；
- 日期 / 时间 / 时区；
- frequency；
- adjust；
- 量额单位；
- 币种。

Bar 比较键示例：

```text
normalized_ticker + frequency + trade_date/timestamp + adjust
```

### 13.2 容忍阈值

默认容忍阈值示例：

| 字段类型 | 容忍 |
|---|---|
| 价格 | 绝对 ≤ 0.01 或相对 ≤ 0.01% |
| 成交量 | 相对 ≤ 0.5% |
| 成交额 | 相对 ≤ 1.0% |
| 换手率 | 绝对 ≤ 0.05pct |
| 复权因子 | 相对 ≤ 0.01% |
| 市值 / PE / PB / PS | 相对 ≤ 1.0% |
| 财报金额 | 相对 ≤ 0.1%，或绝对 ≤ 10000 |
| 财务比率 | 绝对 ≤ 0.01 或相对 ≤ 1.0% |

### 13.3 冲突严重度

- `low`：小数精度、名称格式、轻微四舍五入。
- `medium`：成交量、成交额、估值、财务指标明显差异。
- `high`：收盘价、复权因子、停复牌、ST、涨跌停、交易日历、财报核心字段明显不一致。
- `critical`：影响交易可用性的关键矛盾。critical 数据不得进入可交易层。

### 13.4 合并方法

`MergeMethod` 包括：

```text
canonical_only
canonical_validated
canonical_with_warning
fill_missing_from_supplement
fallback_single_source
fallback_multi_source_agreed
provider_specific_append
quarantined_due_to_conflict
manual_review_required
```

原则：主源永不被自动覆盖；缺失字段只能按白名单补；行业/概念/资金流按 provider 分别保存；critical 冲突触发隔离或人工复核。

### 13.5 质量评分

默认权重：

```text
0.25 * completeness
0.30 * consistency
0.15 * timeliness
0.10 * provider_reliability
0.10 * anomaly
0.10 * provenance
```

Provider 初始可靠性：

```text
tushare 0.90
joinquant 0.85
akshare 0.75
baostock 0.70
manual_import 0.60
unknown 0.30
```

常见调整：

- 双源验证：`+0.08`
- 单源验证：`+0.04`
- 未验证：`-0.05`
- 单源 fallback：`-0.15`
- 多源 fallback 一致：`-0.08`
- low / medium / high / critical 冲突：`-0.05 / -0.12 / -0.25 / -0.50`
- 缺 `raw_payload_ref`：`-0.30`
- 缺 `field_provenance`：`-0.20`

---

## 14. Ticker、日期与单位规范

内部 ticker：

```text
600519.SH
000001.SZ
430047.BJ
00005.HK
```

支持常见输入：

```text
600519
sh600519
sh.600519
sz000001
hk00005
600519.XSHG
000001.XSHE
```

无法识别或交易所后缀与代码前缀冲突时抛 `INVALID_TICKER`，不静默猜测。

日期支持：

```text
YYYY-MM-DD
YYYYMMDD
```

内部时间按 `Asia/Shanghai` 处理。

单位归一化：

- 成交量统一到股（`share`）。
- 成交额统一到 `CNY` / `HKD`。
- Tushare `vol` 通常按“手”转股。
- Tushare `amount` 通常按“千元”转 CNY。
- BaoStock 成交量通常已是股，成交额为 CNY。
- 标准 bars 可计算 `vwap = amount / volume`。

---

## 15. Smoke Test 标准流程

下面以 `600519.SH` 和 `2026-06-01` 到 `2026-06-05` 为例。Agent 可以按这个流程验证一套独立测试库，不污染默认 `data/stock_data.db`。

```bash
cd tools/stock_data_collector
source <你的虚拟环境>/bin/activate

export TICKER="600519.SH"
export START="2026-06-01"
export END="2026-06-05"
export TEST_ROOT="data/smoke_600519_20260601_20260605"
export CFG="$TEST_ROOT/config"

rm -rf "$TEST_ROOT"
mkdir -p "$CFG"
cp config/data_sources.yaml "$CFG/data_sources.yaml"
cp config/data_quality.yaml "$CFG/data_quality.yaml"
cat > "$CFG/storage.yaml" <<YAML
sqlite_path: $TEST_ROOT/stock_data.db
enable_wal: true
raw_object_root: $TEST_ROOT/raw_objects
parquet_root: $TEST_ROOT/parquet
compress_raw_payload: true
raw_format: jsonl.gz
timezone: Asia/Shanghai
log_path: $TEST_ROOT/logs/stock_data_ingestion.log
YAML
```

初始化：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" init-db
```

Cookie 检查：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" verify eastmoney-cookie \
  || echo "Eastmoney Cookie may need refresh"
```

拉取并保存 stdout：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" fetch security-master \
  --tickers "$TICKER" --providers tushare baostock akshare --canonical-provider tushare \
  | tee "$TEST_ROOT/01_security_master.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch trade-calendar \
  --exchange SSE --start-date "$START" --end-date "$END" \
  --providers tushare baostock --canonical-provider tushare \
  | tee "$TEST_ROOT/02_trade_calendar.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch historical-bars \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --frequency 1d --adjust raw \
  --providers tushare akshare baostock --canonical-provider tushare --cross-validate \
  | tee "$TEST_ROOT/03_historical_bars.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch adj-factor \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --providers tushare baostock --canonical-provider tushare --cross-validate \
  | tee "$TEST_ROOT/04_adj_factor.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch valuation \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --providers tushare baostock akshare --canonical-provider tushare \
  | tee "$TEST_ROOT/05_valuation.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch money-flow \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --providers tushare akshare --canonical-provider tushare \
  | tee "$TEST_ROOT/06_money_flow.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch corporate-action \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --providers tushare baostock --canonical-provider tushare \
  | tee "$TEST_ROOT/07_corporate_action.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch financial-statement \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --providers tushare baostock --canonical-provider tushare \
  | tee "$TEST_ROOT/08_financial_statement.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" fetch financial-indicator \
  --tickers "$TICKER" --start-date "$START" --end-date "$END" \
  --providers tushare baostock --canonical-provider tushare \
  | tee "$TEST_ROOT/09_financial_indicator.json"
```

查询验证：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" query bars \
  --ticker "$TICKER" --start-date "$START" --end-date "$END" \
  --frequency 1d --adjust raw \
  | tee "$TEST_ROOT/20_query_raw_bars.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" query bars \
  --ticker "$TICKER" --start-date "$START" --end-date "$END" \
  --frequency 1d --adjust qfq --trading-ready --minimum-quality 0.80 \
  | tee "$TEST_ROOT/21_query_qfq_trading_ready.json"

python -m stock_data_ingestion.cli --config-dir "$CFG" query meta-summary \
  --ticker "$TICKER" --test-root "$TEST_ROOT" \
  | tee "$TEST_ROOT/99_meta_summary.json"
```

正常偏差：

- `daily_bar_trading_days` 可能小于自然日天数，因为周末、节假日或数据源缺失。
- `financial_statements` / `financial_indicators` 在短日期区间可能为 0。
- `corporate_actions` 无事件时可能为 0。
- BaoStock 事件型复权因子在无除权除息区间可能为空；Tushare `adj_factor` 通常按交易日返回。
- `money_flow` 若 AKShare/Eastmoney 失败但 Tushare 成功，通常是 `partial_success`，应检查 Cookie 与 provider results。

---

## 16. 诊断工具

### 16.1 AKShare probe

```bash
python tools/akshare_stock_probe.py \
  --tickers 600519.SH \
  --start-date 20260601 \
  --end-date 20260605 \
  --output-dir data/smoke_600519_20260601_20260605/akshare_probe \
  --include-eastmoney-hist \
  --strict-eastmoney
```

该 probe 输出 JSON 和 Markdown，统计 `PASS`、`FAILED`、`EMPTY`、`SKIPPED`、`OPTIONAL_FAILED` 等。它按生产逻辑探测：默认 AKShare + Cookie，Eastmoney 请求注入 Cookie，失败时支持腾讯或 direct Eastmoney fallback。

### 16.2 BaoStock probe

```bash
python tools/baostock_stock_probe.py \
  --tickers 600519.SH 000001.SZ \
  --start-date 2026-06-01 \
  --end-date 2026-06-05
```

BaoStock 不需要用户凭证，但需要 SDK login。当前生产路径主要用于 SH/SZ A 股补充与验证。

### 16.3 Tushare probe

```bash
python tools/tushare_stock_probe.py \
  --tickers 600519.SH 000001.SZ \
  --start-date 2026-05-01 \
  --end-date 2026-05-29
```

### 16.4 Tushare 空结果调试

```bash
python tools/tushare_empty_debug.py --tickers 600519.SH 000001.SZ
```

用于区分 token / 权限 / 日期 / 股票代码 / 接口空结果等问题。

### 16.5 Daily MVP 配置体检

```bash
python tools/daily_mvp_smoke.py --config-dir config --as-of 2026-06-08
python tools/daily_mvp_smoke.py --config-dir config --as-of 2026-06-08 --use-env-overrides
```

该工具不访问外部 API，不写行情数据，只检查配置是否符合“日频盘后 MVP”：Tushare canonical，AKShare + BaoStock 启用，JoinQuant 关闭，lookback 窗口合理，资金流不使用 BaoStock。

---

## 17. 请求模型

`StockDataRequest` 使用 Pydantic v2，`extra="forbid"`。

支持的 request type：

```text
security_master
trade_calendar
trading_status
historical_bars
realtime_quote
adj_factor
financial_statement
financial_indicator
valuation_metric
industry_concept
money_flow
index_data
corporate_action
batch_refresh
cross_validation
```

支持的 frequency：

```text
1m
5m
15m
30m
60m
1d
1w
1mo
realtime
```

支持的 adjust：

```text
none
qfq
hfq
```

CLI 额外接受 `raw` 作为 `none` 的 alias。

要求：

- `historical_bars` / `realtime_quote` / `valuation_metric` / `financial_indicator` 必须带 tickers；
- `save_cleaned=true` 要求 `save_raw=true`；
- 幂等键包含 provider set；
- 通过 `StockDataCollector` 构造请求时，会自动套用配置里的滚动窗口和 provider 选择。

---

## 18. Python 采集用法

CLI 是首选。需要在 Python 中调用采集器时：

```python
from stock_data_ingestion.config import load_config
from stock_data_ingestion.logging_config import setup_logging
from stock_data_ingestion.services.collector import StockDataCollector
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.database import Database
from stock_data_ingestion.storage.raw_object_store import RawObjectStore

config = load_config("config")
setup_logging(config.storage.log_path)

db = Database(config.storage.sqlite_path, enable_wal=config.storage.enable_wal)
db.init()
raw_store = RawObjectStore(config.storage.raw_object_root)
runner = IngestionRunner(config, raw_store, database=db)
collector = StockDataCollector(runner)

response = collector.fetch_historical_bars(
    ["600519.SH", "000001.SZ"],
    start_date="2026-06-05",
    end_date="2026-06-05",
    frequency="1d",
    adjust="none",
    cross_validate=True,
)
print(response.status)
print(response.persistence.saved)
```

Agent 日常任务优先用 CLI，不要绕过 runner 直接调用 provider SDK，否则会丢失 raw、质量评分、冲突、幂等和落库逻辑。

---

## 19. 常见问题

### 19.1 `query bars` 返回 `[]`

通常原因：

- 还没跑 `fetch historical-bars`；
- query 使用了不同 `--config-dir` / SQLite；
- 日期范围不含交易日；
- qfq/hfq 查询缺少 raw bars 或 adj_factor；
- 记录被 `trading-ready` 过滤。

先查：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" query meta-summary --ticker "$TICKER" --test-root "$TEST_ROOT"
```

### 19.2 `money_flow` 是 `partial_success`

看 `provider_results` 和 `errors`。如果 Tushare 成功、AKShare 失败，常见原因是 Eastmoney 连接问题或 Cookie 过期。先跑：

```bash
python -m stock_data_ingestion.cli --config-dir "$CFG" verify eastmoney-cookie
```

### 19.3 `verify eastmoney-cookie` 返回 false

提醒用户刷新 Cookie。Agent 不应自动处理验证码，不应尝试绕过风控。

### 19.4 财务报表 / 财务指标为空

短日期区间没有公告或报告期数据时正常。日常任务可使用较长财务窗口，例如最近 8 个季度。

### 19.5 BaoStock 某些接口为空

事件型接口在无事件日期范围内为空是正常的，例如配股、分红、停牌、部分复权事件。

### 19.6 `PROVIDER_SCHEMA_CHANGED`

供应商字段变了。用对应 probe 查看真实返回字段，然后提醒维护者更新 adapter 字段映射。

### 19.7 Parquet 失败但 SQLite/raw 成功

检查是否安装 `pyarrow`。缺失时可能出现 Parquet 相关失败或 skip。

---

## 20. 错误码与处置

常见 `ErrorCode`：

```text
AUTH_FAILED
PERMISSION_DENIED
TOKEN_MISSING
RATE_LIMITED
PROVIDER_TIMEOUT
PROVIDER_UNAVAILABLE
PROVIDER_SCHEMA_CHANGED
EMPTY_RESULT
INVALID_REQUEST
INVALID_TICKER
INVALID_DATE_RANGE
NORMALIZATION_FAILED
CROSS_VALIDATION_FAILED
RAW_SAVE_FAILED
STORAGE_FAILED
IDEMPOTENCY_CONFLICT
UNKNOWN_ERROR
```

| 现象 | error_code | Agent 处置 |
|---|---|---|
| Tushare token 缺失 | `TOKEN_MISSING` | 提醒补 `TUSHARE_TOKEN` |
| Tushare 鉴权失败 | `AUTH_FAILED` / `PERMISSION_DENIED` | 提醒检查 token、积分、接口权限 |
| 资金流失败 | AKShare/Eastmoney 异常 | 跑 `verify eastmoney-cookie`，提醒刷新 Cookie |
| 限频 | `RATE_LIMITED` | 降速、分批、稍后重试 |
| 源不可用 | `PROVIDER_UNAVAILABLE` / `PROVIDER_TIMEOUT` | 记录并稍后重试，其他源继续 |
| 字段变化 | `PROVIDER_SCHEMA_CHANGED` | 用 probe 排查，提醒更新字段映射 |
| 空结果 | `EMPTY_RESULT` | 非阻塞；非交易日、停牌、事件稀疏都可能正常 |
| SQLite / Parquet 写入失败 | `STORAGE_FAILED` | 检查路径、权限、依赖、磁盘 |
| raw 保存失败 | `RAW_SAVE_FAILED` | 检查 raw root 路径、权限、磁盘 |

`EMPTY_RESULT` 通常不应单独视为任务失败；非空错误或 provider failed/unavailable 才需要更高优先级处置。

---

## 21. 测试

运行：

```bash
python -m pytest -q
python -m pytest -ra
```

常用聚焦测试：

```bash
python -m pytest tests/test_cli.py
python -m pytest tests/test_akshare_adapter_manual_coverage.py tests/test_env_loading.py
python -m pytest tests/test_provider_selection.py tests/test_provider_selection_config.py
```

本地缺少 `pyarrow` 时，Parquet 测试会 skip。缺少 `SQLAlchemy` 时，SQLite / QueryService 相关测试会 skip。真实外部 API 可用性不要靠单元测试判断，使用 `tools/*_probe.py`。

---

## 22. 已知限制

- 无内置 scheduler；Agent 或外部调度系统负责定时。
- 工具本身不判断今天是否交易日；Agent / 调度器负责目标交易日计算。
- 默认目标是日频盘后数据；分钟线能力依赖具体 provider 接口，不作为第一阶段生产主路径。
- CLI 尚未把底层所有 schema 都暴露成 fetch 子命令，例如 `index_data`、`industry_concept`、`realtime_quote` 目前主要是底层能力 / 未来扩展。
- Tushare 权限、积分和字段权限会影响返回；空结果不一定是程序错误。
- Eastmoney Cookie 会过期，需要周期性刷新。
- AKShare/Eastmoney 可能因网站防护、Cookie 过期、接口变更而失败。
- AKShare 无独立复权因子表；qfq/hfq 主要依赖 Tushare/BaoStock 复权因子在查询层动态生成。
- BaoStock 当前主要用于 SH/SZ A 股补充与验证；HK 不走 BaoStock，BJ/BSE 依实际 adapter 能力处理。
- BaoStock 无个股 money-flow。
- 港股通港股日线主要通过 Tushare HK daily；不要直接请求 HK qfq/hfq bars。
- 供应商口径不同的数据，如行业、概念、资金流，不应强行合并成单一真值。
- Raw Object Store 当前为本地目录；未来迁移对象存储时应保持 `raw_payload_ref` 抽象。
- 生产接入前应根据真实权限和返回样例继续固化 provider-specific 字段映射。

---

## 23. 项目结构

```text
stock_data_collector/
  pyproject.toml
  requirements.txt
  README.md
  .env.example
  config/
    data_sources.yaml
    storage.yaml
    data_quality.yaml
  stock_data_ingestion/
    cli.py
    config.py
    env.py
    logging_config.py
    adapters/
      base.py
      tushare_adapter.py
      akshare_adapter.py
      baostock_adapter.py
      joinquant_adapter.py
    schemas/
      requests.py
      responses.py
      records.py
      quality.py
      errors.py
    storage/
      database.py
      models.py
      repositories.py
      raw_object_store.py
      parquet_store.py
      migrations.py
    normalization/
      ticker.py
      datetime_utils.py
      units.py
      field_mapping.py
    validation/
      comparison.py
      conflict.py
      quality_score.py
      merge_policy.py
    services/
      collector.py
      query_service.py
      ingestion_runner.py
    utils/
      hashing.py
      idempotency.py
      retry.py
  tests/
  tools/
    tushare_stock_probe.py
    akshare_stock_probe.py
    baostock_stock_probe.py
    tushare_empty_debug.py
    daily_mvp_smoke.py
    update_eastmoney_cookie.py
```

---

## 24. 交付与安全

源码交付应排除：

```text
.env
data/
logs/
*.db
*.sqlite
*.parquet
*.jsonl.gz
*.egg-info/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
.venv/
```

安全要求：

- 不要把真实 token、账号、密码、手机号、Cookie 写入 README、commit message、PR 描述、日志或 Agent 回复。
- `.env.example` 只保留空值和操作说明。
- Agent 需要提醒用户时，只说明哪个变量缺失或疑似过期，例如：
  ```text
  EASTMONEY_COOKIE 可能已过期，请从已验证的浏览器会话复制 Cookie，并运行 tools/update_eastmoney_cookie.py 更新。
  ```
- Agent 不应尝试绕过验证码、破解风控、伪造 Cookie 或保存用户完整 Cookie 到外部系统。
