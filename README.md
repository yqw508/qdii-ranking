# QDII 美国主榜与全球补充榜

本项目每天筛选中国境内场外可申购的人民币 QDII 基金，并生成两张最多各 10 只的榜单：

- 美国主榜：名称不含亚洲、中国、港等地域关键词且美股确认下限不低于 50% 的主动或被动基金，按
  人民币口径 Nasdaq-100 三年周收益相关性排序，Beta 接近 1 作为第一并列规则。
- 全球补充榜：其余符合条件的全球、非美、多市场、REIT 或波动率策略，按三年年化
  收益与最大回撤绝对值之比排序。基金名称命中亚洲、中国或港时固定进入全球补充榜，
  不再因地域名称从候选池剔除；债券和商品仍排除。

共同门槛不限制基金规模，要求成立超过 3 年、三年复权收益不低于 30%；若有完整
5 年历史则五年收益不低于 60%，若有完整 10 年历史则十年收益不低于 100%，对应历史不足
时跳过该项；直销额度不低于 200 元。合同基准继续解析和展示，但其市场、地区、权重、复合结构、识别状态及
产品概要冲突一律不参与筛选或分榜。候选范围限定为 QDII 和 `指数型-海外股票`，主动与
被动策略均可入选；普通境内基金、独立 ETF 本体和非人民币 A 类份额除外。榜单还
展示完整区间可计算时的 5 年、10 年复权收益，以及产品概要披露的基金运作综合费率（年化）；
长期收益按上述条件参与筛选但不参与排序，费率只展示。完整规则见
[`references/methodology.md`](references/methodology.md)。

网页另有“场内溢价”页签，展示 25 只中国场内美股权益 ETF 的价格、IOPV、溢价率、涨跌幅、
基金运作综合费率（年化）、成交额和行情时间。07:07 日报保存上一交易日快照；页签内可以手动刷新约
15 分钟延迟行情。页面使用一张紧凑表格按溢价从高到低排列，点击单只 ETF 可展开行情、综合费率、
产品概要日期和来源。综合费率已从基金资产中扣除，不包含券商收取的场内交易佣金。辅助数据失败不会
阻止基金榜单发布；行情会按 ETF 保留旧值，费率仅在公告索引不可访问时保留并标记上次成功值。

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
node --test scripts/test_premium_refresh.mjs
```

JSON schema 为 12：顶层 `records` 是美国主榜，`global_supplement.records` 是全球补充榜，
`exchange_premium.records` 是场内美股 ETF 溢价快照，
每条记录的 `routing_reason` 说明按美股确认占比分流或按地域名称覆盖分流；
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

`.github/workflows/update-ranking.yml` 每天北京时间 07:07 执行，也支持手动触发。流程依次为
刷新、质量校验、测试、提交生成页、CloudBase 部署、线上验证和 QQ 邮件通知。首次启用时
先手动运行一次并验收。

Repository Secrets：

- `QQ_SMTP_USER`：已开启 SMTP 的 QQ 邮箱。
- `QQ_SMTP_AUTH_CODE`：QQ 邮箱 SMTP 授权码。
- `QQ_MAIL_TO`：可选，多个收件地址用英文逗号或分号分隔；默认发给发件人。
- `TCB_SECRET_ID`、`TCB_SECRET_KEY`：用于 CloudBase CI 的腾讯云 CAM 密钥。
