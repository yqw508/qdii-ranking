# QDII 美股含量榜单

筛选中国境内场外可申购的人民币 QDII 基金，按保守确认的美股持仓下限排名。机构持仓仅作为同美股占比时的第一参考指标。

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
https://<CloudBase 默认域名>/qdii/?v=<榜单日期>
```

日期查询参数用于减少微信继续使用旧页面缓存。默认域名适合初期使用；长期公开访问可再绑定已备案的自定义域名和 HTTPS 证书。
