# Feishu Watchlist Automation

Automates price and financial-data sync for a Feishu Bitable watchlist across HK, A-share, and US equities.

## Default Price-Init Path

The recommended price-init path is now:

`Feishu automation -> GitHub repository_dispatch -> watchlist-price-init workflow`

`main_webhook_dispatch.py` is kept as a local fallback bridge and is no longer required for the default day-to-day flow.

这个项目用于把多市场股票 watchlist 的价格与财务字段自动回写到飞书多维表格，支持本地运行，也支持通过 GitHub Actions 定时执行。

## Highlights

- 多市场支持：港股、A 股、美股
- 双主流程：价格同步 + 财务同步
- 港股财务链路：HKEX + iFind + LLM 路由
- 可部署方式：本地运行 / GitHub Actions 定时任务
- 已包含基础单测与配置示例

## Quick Start

1. 创建虚拟环境并安装依赖
2. 复制 `.env.example` 为 `.env`
3. 填入所需配置
4. 运行 `python main_price.py` 或 `python main_financial.py`

## 功能

- `main_price.py`：同步价格相关字段
- `main_financial.py`：按目标市场分流，同步财务相关字段
- `main_price_single.py`：只同步单条 watchlist 记录的价格字段
- `main_webhook_dispatch.py`：接收飞书 webhook 并转发到 GitHub `repository_dispatch`

## 市场支持

- `港股`：HKEX + iFind + Volcano Ark
- `A股`：AKShare + iFind
- `美股`：AKShare + `yfinance`
- `其他`：默认留空

## 目录

- `main_price.py`
- `main_financial.py`
- `main_price_single.py`
- `main_webhook_dispatch.py`
- `clients/`：飞书、iFind、LLM 客户端
- `data_processors/`：A 股、港股、美股处理器
- `services/`：同步编排
- `utils/`：通用工具
- `tests/`：本地可重复运行的轻量测试
- `.github/workflows/`：GitHub Actions

A/H day refresh is handled by `.github/workflows/price-sync-ah.yml`.
US-only night refresh is handled by `.github/workflows/price-sync-us.yml`.

```bash
python main_price.py
python main_financial.py
python main_price_single.py --record-id recxxxxx --code 700.HK
python main_webhook_dispatch.py
```

## 环境变量

参考 `.env.example`。

港股财务同步当前推荐使用火山方舟 OpenAI 兼容接口：

- `VOLCENGINE_API_KEY`
- `VOLCENGINE_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`
- `VOLCENGINE_MODEL=deepseek-r1-250528`
- `VOLCENGINE_ENABLED=true`

代码同时兼容火山文档里的别名配置：

- `ARK_API_KEY`
- `ARK_BASE_URL`
- `ARK_MODEL`

如需备用渠道，也支持：

- `SILICONFLOW_API_KEY`
- `SILICONFLOW_BASE_URL`
- `SILICONFLOW_MODEL`
- `SILICONFLOW_ENABLED=true`
- `GITHUB_DISPATCH_TOKEN`
- `GITHUB_REPOSITORY_OWNER`
- `GITHUB_REPOSITORY_NAME`
- `GITHUB_DISPATCH_EVENT_TYPE=watchlist_price_init`
- `WEBHOOK_SHARED_SECRET`
- `WEBHOOK_HOST=0.0.0.0`
- `WEBHOOK_PORT=8787`

- 美股 `earnings_dates` 默认会优先使用 `YFINANCE_CACHE_DIR`
- 如果未设置，GitHub Actions 会落到 `RUNNER_TEMP/yfinance-cache`
- 本地未设置时会落到项目内 `.cache/yfinance`

GitHub Secrets 配置说明见 `docs/GITHUB_SETUP.md`。

## Price-Only 事件试点

- dashboard 新增辅助字段：`价格初始化状态`
- 推荐状态值：`重新拉取`、`处理中`、`完成`、`失败`
- 飞书自动化在 `代码` 从空变非空时调用 webhook
- webhook 只校验密钥并触发 GitHub `.github/workflows/watchlist-price-init.yml`
- workflow 只处理单条记录，不扫描整张表

## 说明

- `.env` 已被 `.gitignore` 忽略，不应提交到 GitHub
- 港股财务同步依赖 `VOLCENGINE_API_KEY`，也兼容 `ARK_API_KEY`
- 港股 iFind 调用优先使用 `IFIND_REFRESH_TOKEN` 刷新令牌
