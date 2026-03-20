# GitHub Actions 配置清单

## 必填 Secrets

在 GitHub 仓库 `Settings -> Secrets and variables -> Actions` 中新增以下 Secrets：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_APP_TOKEN`
- `FEISHU_TABLE_ID`
- `FEISHU_LOG_TABLE_ID`
- `IFIND_ACCESS_TOKEN`
- `IFIND_REFRESH_TOKEN`
- `VOLCENGINE_API_KEY`

建议同时配置：

- `VOLCENGINE_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`
- `VOLCENGINE_MODEL=deepseek-r1-250528`
- `VOLCENGINE_ENABLED=true`
- `SILICONFLOW_ENABLED`
- `SILICONFLOW_API_KEY`
- `SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1`
- `SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V3.2`

## 对应工作流

- A/H 日间价格同步：`.github/workflows/price-sync-ah.yml`
- 美股夜间价格同步：`.github/workflows/price-sync-us.yml`
- 财务同步：`.github/workflows/financial-sync.yml`
- 单条价格初始化：`.github/workflows/watchlist-price-init.yml`

其中 A/H 日间价格同步会分别执行：

- `python main_price.py --market hk`
- `python main_price.py --market a`

其中美股夜间价格同步按纽约时间以下四个时点执行：

- `09:45`
- `11:45`
- `13:45`
- `16:15`

命令行市场筛选支持中文和值别名：

- `港股` / `hk`
- `A股` / `a`
- `美股` / `us`

## 当前推荐链路

Price-only 初始化当前推荐直接由飞书自动化调用 GitHub `repository_dispatch`：

`飞书自动化 -> GitHub repository_dispatch -> watchlist-price-init workflow`

这条链路不需要日常运行本地 `ngrok` 或 `main_webhook_dispatch.py`。

## 飞书直连 GitHub 配置

飞书自动化 HTTP 请求配置如下：

### URL

```text
https://api.github.com/repos/<GITHUB_REPOSITORY_OWNER>/<GITHUB_REPOSITORY_NAME>/dispatches
```

### Headers

```text
Accept: application/vnd.github+json
Authorization: Bearer <YOUR_GITHUB_TOKEN>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

### JSON Body

```json
{
  "event_type": "watchlist_price_init",
  "client_payload": {
    "record_id": "{{当前记录的record_id}}",
    "code": "{{当前记录的代码}}",
    "event_time": "{{当前时间或飞书变量}}",
    "source": "feishu_watchlist"
  }
}
```

说明：

- `event_type` 必须和 `.env` / workflow 中的 `watchlist_price_init` 保持一致
- `record_id` 必须传飞书多维表格这条记录的真实 `record_id`
- `code` 会被 workflow 传给 `main_price_single.py`

## 本地备用方案

如果以后需要保留本地中转方案，仍可使用：

```bash
python main_webhook_dispatch.py
```

此时飞书自动化改为调用本地 webhook，而不是 GitHub API。

本地中转只在以下场景需要：

- 不希望把 GitHub token 配到飞书侧
- 需要在 webhook 层做额外验签或字段转换
- 需要本地调试 webhook 入参

## 本地 webhook 备用环境变量

以下变量仅在使用本地中转时需要，不是飞书直连 GitHub 的日常必需项：

- `GITHUB_DISPATCH_TOKEN`
- `GITHUB_REPOSITORY_OWNER`
- `GITHUB_REPOSITORY_NAME`
- `GITHUB_DISPATCH_EVENT_TYPE=watchlist_price_init`
- `WEBHOOK_SHARED_SECRET`
- `WEBHOOK_HOST=0.0.0.0`
- `WEBHOOK_PORT=8787`

## 验证步骤

1. 在飞书 watchlist 新增一条记录。
2. 确认飞书自动化 HTTP 请求返回 2xx。
3. 在 GitHub Actions 中确认出现一次 `watchlist-price-init` workflow run。
4. 检查 workflow 入参：
   - `github.event.client_payload.record_id`
   - `github.event.client_payload.code`
5. 确认飞书记录成功回写：
   - `实时股价`
   - `涨跌幅`
   - `总市值`
   - `价格初始化状态`

## 注意事项

- `watchlist-price-init` 依赖 `repository_dispatch` 事件，不需要额外 workflow 输入
- 如果触发失败，优先检查 GitHub token 是否有目标仓库的调用权限
- 如果 workflow 成功启动但未回写，优先检查飞书 `record_id`、`代码` 字段值和 GitHub Secrets
