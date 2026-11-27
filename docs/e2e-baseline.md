# 社区产品 E2E 基线

每个产品按同一批次记录 Tester commit、被测产品 commit、运行模式、证据类型和场景计数。用例目录的业务场景数与 Playwright 执行数各自独立。通过率使用「一次通过 + 重试后通过」除以「通过 + 失败」，同时记录应执行用例的通过比例。跳过、预期失败、未执行和预检阻塞都需要单独说明。验收目标是应执行用例全部通过。

## 历史全量批次

首次通过 `yeying-tester overview` 采集的最近全量批次为 `2026-11-25_22-40-12+0800`。报告汇总 796 项，197 通过、413 失败、186 跳过。项目计数来自可归属的历史 JUnit 结果。该批次的运行模式、被测版本和重试细节没有保存。 [全量报告](https://tester.tidukongjian.com/report/runs/2026-11-25_22-40-12+0800/index.html)

| 产品 | 通过 | 失败 | 跳过 | 备注 |
| --- | ---: | ---: | ---: | --- |
| Book | 28 | 2 | 5 | 该批次仍使用历史标识 `books` |
| Chat | 3 | 65 | 26 | |
| Marketplace | 53 | 0 | 5 | |
| Node | 0 | 51 | 9 | 51 个失败均为连接拒绝 |
| Orchestrator | 0 | 0 | 10 | 只有跳过记录 |
| Project | 1 | 84 | 59 | |
| Router | 61 | 40 | 16 | |
| Social | 1 | 63 | 26 | |
| Wallet | 50 | 37 | 7 | |
| Warehouse | 0 | 71 | 23 | |
| Knowledge | — | — | — | 当前批次无可归属证据 |

以上表格仅使用本次全量归属结果。单产品历史报告保留各自的批次和运行条件。

## Node

| 批次 | 模式 | Tester / Node 提交 | 通过 | 失败 | 跳过 |
| --- | --- | --- | ---: | ---: | ---: |
| [2026-11-27 08:47](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_08-47-27+0800/index.html) | 本地源码 | `db1b1c6` / `d7a20dc` | 49 | 2 | 9 |
| [2026-11-27 08:53](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_08-53-34+0800/index.html) | 本地源码，Tester 候选代码 | `8882d69` / `d7a20dc` | 54 | 0 | 6 |
| [2026-11-27 09:04](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_09-04-23+0800/index.html) | 本地源码，Tester 候选代码 | `3273676` / `d7a20dc` | 54 | 1 | 6 |
| [2026-11-27 09:13](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_09-13-04+0800/index.html) | 本地源码，Tester 候选代码 | `76e9185` / `d7a20dc` | 55 | 0 | 6 |
| [2026-11-27 09:19](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_09-19-58+0800/index.html) | 本地源码，首次启用隔离审批和数据库场景 | `2e2a59c` / `d7a20dc` | 54 | 5 | 2 |
| [2026-11-27 09:23](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_09-23-14+0800/index.html) | 本地源码、隔离数据库 | `d6eba97` / `d7a20dc` | 59 | 0 | 2 |
| [2026-11-27 09:32](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-27_09-32-25+0800/index.html) | 本地源码、隔离数据库，重复批次 | `7f653d7` / `d7a20dc` | 59 | 0 | 2 |

GPU 上的 PostgreSQL、Redis 和 Mailpit 可访问，Node 测试由 Tester 临时拉起源码 API 和 Web。Node 单项目批次创建专用 PostgreSQL 数据库和每批一次性 vault、管理员钱包。服务运行自动迁移后执行用例，结束时清除数据库及临时凭证。最后两轮各运行 61 个场景，以一个 worker、零重试完成，每轮均为 59 通过、0 失败、2 跳过。数据库断连、审核批准、发布及下线用例均已实际执行。两轮已执行场景通过率为 100%（59/59），全场景通过比例为 96.7%（59/61）。

WebAuthn 初始失败由 WebAuthn origin 与浏览器 origin 不匹配导致。后续统一以 `localhost` 配置 Node Web、WebAuthn RP 和浏览器入口。市场详情首次失败是断言没有等待无钱包插件环境中的 Provider 发现窗口。通过 role 选择菜单并调整路由断言超时后复测通过。草稿初始跳转失败原因仍未确认，同场景后续通过。首次新增隔离数据库用例时有五项 fixture 失败，调整数据初始状态与签名载荷后通过。

| 尚未完成的场景 | 需要补齐的证据 |
| --- | --- |
| `ND-API-035` | 配置真实钉钉群机器人的 webhook URL 和加签密钥，并验证群内收到消息 |
| `ND-API-036` | 配置真实飞书群机器人的 webhook URL 和加签密钥，并验证群内收到消息 |

Node 用例文档包含 58 个唯一业务 ID，均已实现。Playwright 列出 61 个执行场景，其中 3 个冒烟检查没有单独的业务 ID，`ND-UI-002` 有两个执行场景，另有一个场景同时覆盖 `ND-UI-001` 和 `ND-UI-004`。已检查 GPU 上 Tester 的 `/opt/deploy/tester/.env.local`，以下配置项均未设置：`NODE_DINGTALK_WEBHOOK_URL`、`NODE_DINGTALK_WEBHOOK_SECRET`、`NODE_FEISHU_WEBHOOK_URL`、`NODE_FEISHU_WEBHOOK_SECRET`。Node 功能用例与产品缺陷排查已完成，严格的全场景 100% 门禁仍需实际运行上述两个外部集成场景。
