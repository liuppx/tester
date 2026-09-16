# 端到端测试用例总览

以测试人员视角整理的各产品**应有** E2E 测试用例,作为自动化实现的依据。
每个产品一份文档,用例按功能模块分组,标注优先级(P0/P1/P2)、类型(UI/API/DAPP/DATA/E2E)、
状态(✅ 已实现并链接到 spec / ⬜ 待实现)与完整的前置条件·步骤·预期结果。

## 进度总览

| 产品 | 文档 | 用例数 | ✅ 已实现 | ⬜ 待实现 | 完成度 |
| --- | --- | ---: | ---: | ---: | ---: |
| 仓库 Warehouse | [warehouse.md](warehouse.md) | 91 | 38 | 53 | 42% |
| 节点 Node | [node.md](node.md) | 50 | 26 | 24 | 52% |
| 路由 Router | [router.md](router.md) | 79 | 38 | 41 | 48% |
| 钱包 Wallet | [wallet.md](wallet.md) | 56 | 24 | 32 | 43% |
| 对话 Chat | [chat.md](chat.md) | 92 | 5 | 87 | 5% |
| 社交 Social | [social.md](social.md) | 87 | 29 | 58 | 33% |
| 项目 Project | [project.md](project.md) | 141 | 24 | 117 | 17% |
| 知识库 Knowledge | [knowledge.md](knowledge.md) | 110 | 5 | 105 | 5% |
| 智能体 Agent | [agent.md](agent.md) | 58 | 4 | 54 | 7% |
| 应用市场 Marketplace | [marketplace.md](marketplace.md) | 59 | 5 | 54 | 8% |
| 文档 Books | [books.md](books.md) | 35 | 4 | 31 | 11% |
| **合计** | | **858** | **202** | **656** | **24%** |

## 使用方式

- **实现测试时**:挑一个产品文档,从 P0 的 ⬜ 待实现用例做起;每落地一条,把状态改为
  ✅ 并补上对应 spec 路径,同步更新该文档的覆盖总览表与本页进度表的计数。
- **状态口径**:「已实现」以*逻辑用例*是否被现有 spec 覆盖为准(一条逻辑用例可能对应多个
  spec test 块,也可能多条逻辑用例合并进一个 spec)。本目录是用例的唯一事实来源:记录
  「每个产品应该有哪些用例、还差哪些」,而 `products/<name>/tests/*.spec.ts` 是这些用例的
  自动化实现。

## 各产品重点缺口(P0 待实现)

**Warehouse** — P0 已补齐(负向鉴权基线、撤销 AccessKey 拒绝、S3 SigV4 真实 ListBuckets、公开/定向分享创建与匿名访问、只读分享写拒绝、admin 越权 403、心跳/就绪探针)。剩余待建:通知、分组、管理员用户管理整片,以及回收站、配额、资料/密码的 P1/P2。

**Node** — P0 主链路已覆盖(未过审发布 403、写操作签名信封校验、真实 window.ethereum 端到端 SIWE 登录、受保护路由守卫);仅审核工作流全链路(ND-E2E-003/004)因环境无管理员审批能力降级为干净跳过。剩余待建:refresh 轮换/logout 撤销、应用可见范围与非属主 403、身份能力(TOTP/Passkey/授权码)。

**Router** — OpenAI 兼容中继整块(models / chat/completions 扣额度)零覆盖、混合信封契约、
SIWE 负路径与 nonce 一次性语义、admin 越权 403、未登录路由重定向。

**Wallet** — dApp 侧负路径(拒绝连接/签名返回 4001)、EIP-2255 权限与链切换审批、
私钥/keystore 导入、收款地址 EIP-55 校验、dApp 触发的交易审批窗。

**Chat** — LLM 会话主路径(流式回复 / 停止响应中断 / 无效 key 优雅报错)、钱包 SIWE 登录准入、
Router 令牌选择、Provider 代理转发。整个业务链路(登录门槛之后)现为冒烟层覆盖。

**Social** — P0 主链路已覆盖(邮箱+密码与 web3-identity SIWE 双路径签发 LoginVO、错误口令/未知邮箱拒绝、自定义 `accessToken` 头鉴权守卫、SIWE nonce 一次性与错误签名拒绝、双用户好友/建群/私聊+群聊消息收发、seeded 会话首页渲染与发消息)。注:web3-graph/incentive 后端为空壳,未纳入。剩余待建:会话/联系人整片 UI、消息已读/撤回、群管理。

**Project**(夜莺/DooTask)— P0 主链路已覆盖(登录签发 token、`ret=-1`/身份失效鉴权守卫、admin 越权 403、项目/列/任务 CRUD、任务完成与撤销、flow 状态流转、成员更新、仪表盘/侧边栏渲染、UI 建项目向导与看板加卡)。因 admin 账号被验证码锁、钱包 SIWE 命中 setup_token 门,用例改用开放注册的临时账号跑通。剩余待建:文件/文档、消息/审批通知、日历、报表等业务面。

**Knowledge** — SIWE 登录换双令牌、受保护接口 401 基线、知识库创建、Search Lab 三模式检索对比、
面向 Agent 的服务检索(`X-Service-Api-Key`)。实为完整知识运营系统,17 个模块业务链路全待建。

**Agent** — 钱包 SIWE 换会话 Cookie 主流程、错误签名拒绝、会话校验 401、受保护目录 401、
Messenger 实例创建与生命周期。

**Marketplace** — 完整技能包 schema 全字段校验、index↔packages 一致性、path/toolServers 引用完整性、
(id,lang) 唯一性。现有 spec 仅校验轻量索引的少数字段。

**Books** — SUMMARY 链接目标文件存在(死链校验)、章节孤儿检测、硬断言而非缺失即跳过。
已发现真实死链:`agent/README.md` 内 SUMMARY 链接写成绝对路径且缺 `opensource/` 段。
