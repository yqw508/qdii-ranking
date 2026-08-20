# QDII 美国主榜与全球补充榜

本项目每天筛选中国境内场外可申购的人民币 QDII 基金，并生成两张最多各 10 只的榜单：

- 美国主榜：合同基准明确的美国权益主动或被动基金，要求美股确认下限不低于 50%，按
  人民币口径 Nasdaq-100 三年周收益相关性排序，Beta 接近 1 作为第一并列规则。
- 全球补充榜：其余合同基准明确的全球、非美、多市场、REIT 或波动率策略，按三年年化
  收益与最大回撤绝对值之比排序。债券、商品以及以中国、香港、泛亚洲为主要目标的策略
  不进入榜单。

共同门槛包括规模严格大于 3 亿元、成立超过 3 年、三年复权收益不低于 50%、直销额度
严格大于 1,000 元，以及最新招募说明书中的单一主要市场基准权重不低于 80%。主动与被动
策略均可入选，独立 ETF 本体和非人民币 A 类份额除外。完整规则见
[`references/methodology.md`](references/methodology.md)。

## 本地更新

使用包含 `pypdf` 的 Python 环境在仓库根目录执行：

```powershell
python scripts/update_qdii_ranking.py `
  --output-dir .\output\qdii-ranking `
  --publish-dir .\public
```

更新后检查 `output/qdii-ranking/latest.json`、`latest.md` 和全部警告，再运行：

```powershell
python scripts/validate_qdii_ranking.py `
  --output-dir .\output\qdii-ranking `
  --publish-dir .\public `
  --expected-date <北京时间当天日期>
python -m unittest discover -s scripts -p "test_*.py"
```

JSON schema 为 8：顶层 `records` 是美国主榜，`global_supplement.records` 是全球补充榜，
`exclusion_summary` 汇总候选剔除原因。CSV、Markdown、`latest.html` 和 `public/index.html` 均由
同一份 JSON 数据生成，两个 HTML 必须字节一致。

## 发布

验证通过后提交 `public/index.html` 并推送 `main`，再使用目标环境的 CloudBase CLI：

```powershell
tcb app deploy qdii-ranking-web `
  --env-id run-cool-d2gy0iw957219659c `
  --framework static `
  --output-dir public `
  --deploy-path /qdii `
  --cwd . `
  --force `
  --json
```

网页地址：

```text
https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com/?v=<榜单日期>
```

`/qdii` 是 CloudBase 内部部署路径，独立域名直接使用根路径。日期参数用于降低微信命中旧
页面缓存的概率。默认域名首次访问可能显示腾讯云风险提示；微信无提示直达需要已备案的
自定义域名和 HTTPS 证书。

## 每日自动更新

`.github/workflows/update-ranking.yml` 每天北京时间 09:07 执行，也支持手动触发。流程依次为
刷新、质量校验、测试、提交生成页、CloudBase 部署、线上验证和 QQ 邮件通知。首次启用时
先手动运行一次并验收。

Repository Secrets：

- `QQ_SMTP_USER`：已开启 SMTP 的 QQ 邮箱。
- `QQ_SMTP_AUTH_CODE`：QQ 邮箱 SMTP 授权码。
- `QQ_MAIL_TO`：可选，多个收件地址用英文逗号或分号分隔；默认发给发件人。
- `TCB_SECRET_ID`、`TCB_SECRET_KEY`：用于 CloudBase CI 的腾讯云 CAM 密钥。
