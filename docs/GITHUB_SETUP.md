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

建议值：

- `VOLCENGINE_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`
- `VOLCENGINE_MODEL=deepseek-r1-250528`
- `VOLCENGINE_ENABLED=true`

## 对应工作流

- 价格同步：`.github/workflows/price-sync.yml`
- 财务同步：`.github/workflows/financial-sync.yml`

## 运行频率

### `price-sync`

- 工作日盘中高频运行
- 支持手动触发

### `financial-sync`

- `1/2/3/4/5/8/10/11` 月：每周一运行
- `6/7/9/12` 月：每月 1 日运行
- 支持手动触发

## 本地运行

价格同步：

```bash
python main_price.py
```

财务同步：

```bash
python main_financial.py
```

## 注意事项

- 港股财务同步依赖 `VOLCENGINE_API_KEY`、`IFIND_REFRESH_TOKEN`
- 若 `IFIND_ACCESS_TOKEN` 过期，港股逻辑会优先尝试用 `IFIND_REFRESH_TOKEN` 刷新
- `其他` 市场默认回写空值，不做财务抓取
