# 飞书直连 GitHub `repository_dispatch`

## 目标

把 price 初始化链路从：

`飞书自动化 -> ngrok -> main_webhook_dispatch.py -> GitHub Actions`

切换为：

`飞书自动化 -> GitHub repository_dispatch -> GitHub Actions`

## 适用范围

本说明只针对 watchlist 新增条目后的单条价格初始化，不涉及 financial 自动触发。

## 飞书自动化配置

### 请求 URL

```text
https://api.github.com/repos/<owner>/<repo>/dispatches
```

示例：

```text
https://api.github.com/repos/example-org/quant_automation_local/dispatches
```

### 请求 Headers

```text
Accept: application/vnd.github+json
Authorization: Bearer <your_github_token>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

### 请求 Body

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

## 字段要求

- `record_id`：必须是这条多维表格记录的真实 `record_id`
- `code`：必须对应表内 `代码` 列，例如 `700.HK`
- `event_type`：必须保持为 `watchlist_price_init`

## GitHub 侧要求

以下 workflow 无需修改：

- `.github/workflows/watchlist-price-init.yml`

以下 GitHub Secrets 必须已经存在：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_APP_TOKEN`
- `FEISHU_TABLE_ID`
- `FEISHU_LOG_TABLE_ID`
- `IFIND_ACCESS_TOKEN`
- `IFIND_REFRESH_TOKEN`

## 切换后可停用的本地依赖

切换成功后，日常不再需要：

- `ngrok http 8787`
- `python main_webhook_dispatch.py`

本地 webhook 中转代码仍保留，可作为备用或调试用途。

## 验证

1. 新增一条 watchlist 记录。
2. 观察飞书自动化 HTTP 步骤返回 2xx。
3. 打开 GitHub Actions，确认 `watchlist-price-init` 已被触发。
4. 确认目标记录回写成功。

## 常见问题

### GitHub 返回 401 / 403

- 检查 token 是否有效
- 检查 token 是否有目标仓库触发 dispatch 的权限
- 检查仓库 owner / repo 名是否正确

### GitHub Actions 没有触发

- 检查 `event_type` 是否为 `watchlist_price_init`
- 检查请求 URL 是否是 `/dispatches`
- 检查请求是否真的返回 2xx

### Workflow 启动但未回写飞书

- 检查 `record_id` 是否为真实值
- 检查 `code` 是否与表中的 `代码` 一致
- 检查 GitHub Secrets 中的飞书配置
