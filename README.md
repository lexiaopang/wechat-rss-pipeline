# 微信公众号 RSS 聚合

15个公众号合并为一条 RSS。Python标准库，无第三方运行依赖，每小时更新，保留至少14天（按发布日期/首次发现时间较晚者）。

## 订阅

https://raw.githubusercontent.com/lexiaopang/wechat-rss-pipeline/main/public/feed.xml

运行状态：public/status.json；源清单：sources.json。

## 行为

- 规范化原文链接生成SHA-256 GUID；输出保留完整原文链接。
- state.json持久化并随feed提交；跨次去重，不使用可能淘汰的cache存状态。
- 每小时第23分钟运行；支持Actions手动Run workflow。定时可能延迟或漏跑，下次抓取上游仍保留的文章补齐，不能恢复上游已删除且从未抓到的文章。
- 单源超时/坏XML隔离，两次尝试后仅量子位、小众软件可切换网站备用源；主源成功时不抓备用。
- 全部失败时不覆盖旧feed/state，工作流失败以便排查。
- 不截断文章数量；测试覆盖10000篇峰值。
- 正文为源RSS提供的正文或摘要，不承诺公众号全文。
- 公共仓库和公共RSS，订阅源与生成内容均公开。

## 本地

python3 -m unittest -v
python3 aggregate.py

## Make

聚合器每小时更新；Make计划每6小时读取。尚未接入Make或Drive。目标文件夹为用户已有“公众号文章库”。不得自动新建替代Scenario。
必须在端到端验收时检查RSS模块条数上限、游标、固定GUID归档查重，以及免费额度。
免费方案不保证无期限准点：GitHub计划任务可能延迟/丢弃，长期无仓库活动可能禁用；成功更新会提交状态。
