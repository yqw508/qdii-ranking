# QDII 美股含量榜单

筛选中国境内场外可申购的人民币主动管理 QDII 基金，按保守确认的美股持仓下限排名。机构持仓仅作为同美股占比时的第一参考指标。数据源归类为“指数型-海外股票”等指数策略不在当前榜单范围内。

## 本地更新

使用包含 `pypdf` 的 Python 环境在仓库根目录执行：

```powershell
python scripts/update_qdii_ranking.py `
  --output-dir .\output\qdii-ranking `
  --publish-dir .\public
```

更新完成后必须检查 `output/qdii-ranking/latest.json` 和 `latest.md` 中的全部警告。排名关键数据读取失败时脚本会终止，不会覆盖榜单。

运行测试：

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```

`output/qdii-ranking/latest.html` 用于本地查看；内容完全相同的 `public/index.html` 用于 CloudBase 静态部署。

## 发布

确认榜单和警告后提交生成页：

```powershell
git add public/index.html
git commit -m "Update QDII ranking"
git push origin main
```

推送完成后，使用已登录目标环境的 CloudBase CLI 部署静态应用：

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

CloudBase 部署配置：

- 环境：`run-cool-d2gy0iw957219659c`
- 服务：`qdii-ranking-web`
- Git 仓库：`https://github.com/yqw508/qdii-ranking.git`
- 分支：`main`
- 框架：`static`
- 安装命令、构建命令：留空
- 构建产物目录：`public`
- 部署路径：`/qdii`

微信分享链接使用：

```text
https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com/?v=<榜单日期>
```

日期查询参数用于减少微信继续使用旧页面缓存。`/qdii` 是 CloudBase 内部部署路径；应用独立域名直接使用根路径，不要再次拼接 `/qdii/`。

CloudBase 默认域名会在访客首次打开时显示腾讯云风险提醒，需要点击“确定访问”后进入榜单。要在微信中直接进入榜单，必须为该静态应用绑定已完成 ICP 备案的自定义域名和 HTTPS 证书。

## 每日自动更新

GitHub Actions 工作流 `.github/workflows/update-ranking.yml` 每天北京时间 09:07 运行，也可以在仓库的 Actions 页面手动触发。工作流按顺序完成数据刷新、质量校验、测试、提交 `public/index.html`、CloudBase 部署、线上验证和 QQ 邮件通知。任何一步失败都会停止后续发布并尝试发送失败邮件。

在 GitHub 仓库的 `Settings > Secrets and variables > Actions` 中配置：

- `QQ_SMTP_USER`：已开启 SMTP 服务的 QQ 邮箱地址。
- `QQ_SMTP_AUTH_CODE`：QQ 邮箱生成的 SMTP 授权码，不是登录密码。
- `QQ_MAIL_TO`：可选收件地址，多个地址用英文逗号或分号分隔；留空时发给 `QQ_SMTP_USER`。
- `TCB_SECRET_ID`、`TCB_SECRET_KEY`：用于 CloudBase CI 部署的最小权限腾讯云 CAM 子账号密钥。

首次启用时先通过 `workflow_dispatch` 手动运行并确认提交、网页部署及成功邮件全部正常。工作流使用公开仓库的 GitHub 托管 Runner，定时任务可能比 09:07 略有延迟。
