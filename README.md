# 超声波设备领域 · 目标公司舆情看板

静态网站，可直接部署到 GitHub Pages（或任意静态托管）。数据通过 Python 管线每日自动更新。

## 文件说明

| 文件 | 作用 |
|---|---|
| `index.html` | 看板页面（舆情看板 + 公司简介两个页签） |
| `companies.json` | 监控公司名单（评级/国家/财务/产品/渠道/背景/官网/设备分类） |
| `data.json` | 舆情数据（管线每日自动更新） |
| `pending.json` | 管线自动发现的名单外新企业（看板「待确认」区） |
| `scripts/fetch_news.py` | 数据更新管线（Google News RSS + DeepSeek） |
| `companies_import_template.csv` | 企业批量导入模板（UTF-8，Excel 可直接打开） |

## 部署到 GitHub Pages

1. 新建仓库（例如 `你的用户名.github.io`，或任意仓库）。
2. 把本包全部文件提交到 `main` 分支。
3. 仓库 Settings → Pages → Source 选择 `main` 分支根目录。
4. 访问 `https://你的用户名.github.io/`（或仓库对应的 Pages 地址）。

## 日常维护

### 添加企业（三种方式）

- **手动添加**：看板「公司简介」页 →「＋ 手动添加企业」。
- **批量导入**：按 `companies_import_template.csv` 的表头准备 Excel/CSV，点「Excel / CSV 批量导入」。表头：`类型,国家,企业名称,维度一：财务情况,维度二：产品技术,维度三：市场渠道,维度四：公司背景,企业网址,设备分类`（类型填 A/B/C）。
- **自动发现**：管线每天会从行业新闻中识别名单外的超声波/压电企业，出现在「待确认」区，可一键入库或忽略。

> 手动/批量添加先保存在当前浏览器（localStorage）。要让所有设备生效并纳入每日舆情监控：点「导出最新名单 JSON」，用下载的文件替换仓库里的 `companies.json` 并提交推送。

### 更新舆情数据

需要 Python 3 和 DeepSeek API Key：

```bash
# Windows cmd
set DEEPSEEK_API_KEY=sk-你的key
set HTTPS_PROXY=http://127.0.0.1:7890   :: 国内网络访问 Google News 需要代理，按实际端口改
python scripts\fetch_news.py

# macOS / Linux
export DEEPSEEK_API_KEY=sk-你的key
export HTTPS_PROXY=http://127.0.0.1:7890   # 同上，可选
python3 scripts/fetch_news.py
```

运行后提交并推送 `data.json`、`pending.json` 即可。可用 Windows 任务计划或 cron 设为每日自动运行。

## 数据来源与声明

新闻来自 Google News 检索，摘要与情绪判断由 DeepSeek 生成，仅供内部研究参考，不构成投资建议。
