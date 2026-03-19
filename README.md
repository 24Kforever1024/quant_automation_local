# Feishu Watchlist Automation

用于自动更新飞书多维表格 watchlist 的价格与财务字段。

## 功能

- `main_price.py`：同步价格相关字段
- `main_financial.py`：按目标市场分流，同步财务相关字段

## 市场支持

- `港股`：HKEX + iFind + Volcano Ark
- `A股`：AKShare + iFind
- `美股`：AKShare + `yfinance`
- `其他`：默认留空

## 目录

- `main_price.py`
- `main_financial.py`
- `clients/`：飞书、iFind、LLM 客户端
- `data_processors/`：A 股、港股、美股处理器
- `services/`：同步编排
- `utils/`：通用工具
- `tests/`：本地可重复运行的轻量测试
- `.github/workflows/`：GitHub Actions

## 本地运行

1. 创建虚拟环境并安装依赖
2. 复制 `.env.example` 为 `.env`
3. 填入所需配置
4. 运行

```bash
python main_price.py
python main_financial.py
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

- 美股 `earnings_dates` 默认会优先使用 `YFINANCE_CACHE_DIR`
- 如果未设置，GitHub Actions 会落到 `RUNNER_TEMP/yfinance-cache`
- 本地未设置时会落到项目内 `.cache/yfinance`

GitHub Secrets 配置说明见 `docs/GITHUB_SETUP.md`。

## 说明

- `.env` 已被 `.gitignore` 忽略，不应提交到 GitHub
- 港股财务同步依赖 `VOLCENGINE_API_KEY`，也兼容 `ARK_API_KEY`
- 港股 iFind 调用优先使用 `IFIND_REFRESH_TOKEN` 刷新令牌
