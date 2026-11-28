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
| [2026-11-28 10:52](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-28_10-52-50+0800/index.html) | 本地源码、隔离数据库，启用真实机器人投递 | `18f3573` / `d7a20dc` | 59 | 2 | 0 |
| [2026-11-28 11:02](https://tester.tidukongjian.com/report/projects/node/runs/2026-11-28_11-02-41+0800/index.html) | 本地源码、隔离数据库，使用 Node Web 构建产物 | `dbe1aa2` / `d7a20dc` | 61 | 0 | 0 |

GPU 上的 PostgreSQL、Redis 和 Mailpit 可访问，Node 测试由 Tester 临时拉起源码 API 和 Web。Node 单项目批次创建专用 PostgreSQL 数据库和每批一次性 vault、管理员钱包。服务运行自动迁移后执行用例，结束时清除数据库及临时凭证。数据库断连、审核批准、发布及下线用例均已实际执行。

WebAuthn 初始失败由 WebAuthn origin 与浏览器 origin 不匹配导致。后续统一以 `localhost` 配置 Node Web、WebAuthn RP 和浏览器入口。市场详情首次失败是断言没有等待无钱包插件环境中的 Provider 发现窗口。通过 role 选择菜单并调整路由断言超时后复测通过。草稿初始跳转失败原因仍未确认，同场景后续通过。首次新增隔离数据库用例时有五项 fixture 失败，调整数据初始状态与签名载荷后通过。

11 月 28 日 10:52 批次中，钉钉 `ND-API-035` 和飞书 `ND-API-036` 均收到平台成功响应，结束了因缺少真实机器人配置而跳过的状态。本轮两项浏览器测试失败。`ND-E2E-001` 草稿 API 返回成功，页面留在编辑页。`ND-UI-007` 已进入详情路由，详情组件在断言时限内未渲染。同期 Vite 开发服务器记录了依赖优化触发的页面重载。

11:02 批次由 Tester 构建同一份 Node Web 源码并通过 Vite Preview 提供页面，消除了开发服务器在浏览器测试期间的依赖优化重载。两个浏览器场景沿用原断言，均通过。钉钉、飞书、Passkey 和邮箱 OTP 也在同一轮通过。本轮使用一个 worker、零重试，61 个执行场景全部首次通过。

Node 用例文档包含 58 个唯一业务 ID，均已实现。Playwright 列出 61 个执行场景，其中 3 个冒烟检查没有单独的业务 ID，`ND-UI-002` 有两个执行场景，另有一个场景同时覆盖 `ND-UI-001` 和 `ND-UI-004`。真实机器人用例从 Tester 的 `.env.local` 读取 `NODE_DINGTALK_WEBHOOK_URL`、`NODE_DINGTALK_WEBHOOK_SECRET`、`NODE_FEISHU_WEBHOOK_URL` 和 `NODE_FEISHU_WEBHOOK_SECRET`。

## Router

| GPU 批次 | 检出版本 | 通过 | 失败 | 跳过 |
| --- | --- | ---: | ---: | ---: |
| [2026-11-28 12:23](https://tester.tidukongjian.com/report/projects/router/runs/2026-11-28_12-23-58+0800/index.html) | Tester `e044b06`，Router 社区 main `d3fe91c4` | 100 | 0 | 17 |
| [2026-11-28 12:53](https://tester.tidukongjian.com/report/projects/router/runs/2026-11-28_12-53-44+0800/index.html) | Tester `8db437c`，Router PR #318 `abe41366` | 115 | 2 | 0 |
| [2026-11-28 13:02](https://tester.tidukongjian.com/report/projects/router/runs/2026-11-28_13-02-34+0800/index.html) | Tester `dcd7a22`，Router PR #318 `abe41366` | 116 | 1 | 0 |

各批次使用 Node `d7a20dc`。候选批次使用批次专用 PostgreSQL 数据库、一个 worker 和零重试。13:02 的测试代码与 Tester PR #50 中的 `b619a90` 文件树相同。Responses 的七个场景、个人渠道的九个场景、告警迁移、钱包登录、订单全流程均已执行。RT-API-037 从订单详情取得额度，验证支付回调首次履约、重复回调只入账一次、失败回调保持余额。

唯一失败的执行项是分组路由。GR-E2E-001 至 013 共用一个 Playwright 用例。配置 B→A 后上游实际命中 A，且 Token API 未返回 `auto_failover`。Router 社区源码已实现分组顺序存取，设计文档将实际跨组中继列为阶段三。整组保留在全量报告中，候选基线仍未达到 117/117。
