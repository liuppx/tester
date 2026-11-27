# Tester HTTP API

机器可读契约见 [OpenAPI 3.1](openapi.yaml)。契约随仓库提交维护，覆盖控制台、管理员、Skill 和本机探活的现有接口。接入时以目标环境的 service 制品版本为准。部署包含分析功能的 service 后，匿名请求 `/api/v1/analysis/overview` 应返回 `401` 和 `WWW-Authenticate: Bearer`。Token 管理页面同时依赖对应版本的 Web 制品。

## 调用入口

| 分类 | 调用方 | 路径 | 鉴权 |
| --- | --- | --- | --- |
| public | Web 登录 | `/api/auth/identity/login/{session,verify}` | 同源 Origin，校验一次性挑战及钱包凭证 |
| public | Tester Web | `/api/auth/status`、`/api/auth/logout`、`/api/cases`、`/api/overview`、`/api/status`、`/api/run`、`/api/reports`、`/api/reports/files/{relative_path}` | 钱包会话。POST 同时校验 Origin 与 `X-Tester-CSRF` |
| public | Web 依赖关系图 | `/api/architecture` | 允许匿名读取 |
| public | Marketplace 的 `yeying-tester` Skill | `/api/v1/analysis/…` | 管理员签发的只读 Bearer Token |
| admin | Tester 管理员 | `/api/auth/users…`、`/api/auth/analysis-tokens…` | 有效钱包会话及管理员角色。POST 同时校验 Origin 与 `X-Tester-CSRF` |
| internal | 本机 starter 和健康检查 | `/healthz` | 控制服务默认监听回环地址。项目的 HTTPS Nginx 片段只代理 `/api/` |

### 钱包会话与任务

1. Web 调用登录挑战接口，经 Web3 BS 生成 Presentation，再提交至验证接口。通过后使用 `tester_session` Cookie。登录挑战和会话保存在进程内存，服务重启后浏览器需重新登录。成员授权和分析 Token 保存在 SQLite 中。
2. Web 通过 `GET /api/auth/status` 获取角色和当前会话的 CSRF 值。成员和管理员均可提交单项目或全量任务。
3. `POST /api/run` 返回 `202` 和任务 ID，表示任务入队或复用了同范围同模式的任务。通过 `GET /api/status` 追踪执行，再从报告中心读取结果。报告页面和附件使用钱包会话。

### Skill 分析

| 目标 | 查询 |
| --- | --- |
| 整体质量和各项目状态 | `GET /api/v1/analysis/overview` |
| 用例目录、依赖和当前配置键状态 | `GET /api/v1/analysis/projects/{project}` |
| 已保留的批次列表 | `GET /api/v1/analysis/projects/{project}/runs` |
| 运行配置、用例结果和尝试记录 | `GET /api/v1/analysis/projects/{project}/runs/{scope}/{run}` |
| 当前页批次的稳定性分析 | `GET /api/v1/analysis/projects/{project}/stability` |

列表按批次时间倒序，`limit` 默认为 20，允许 1 至 50。将 `next_cursor` 作为下一页 `before` 参数，并保留原筛选条件。URL 编码时将游标里的时区加号编码为 `%2B`。从列表复制 `scope` 和 `run` 查询详情。全量批次的 `scope` 为 `__full__`，只有证据能将项目归属到该批次时才会入列。

`playwright` 证据含尝试状态、耗时和可用失败位置。历史 `junit` 只提供可识别用例的最终结果，`summary` 和 `metadata` 只提供各自保留的汇总或批次元数据。无法取得的信息使用 `null` 或缺省字段，项目预检被阻塞的批次可能没有任何用例结果。配置读取只披露键名和是否非空，依赖清单反映声明的关系。报告链接使用钱包会话，分析 Token 的权限止于上述五个读取接口。

`stability` 按运行模式和证据类型分组，每次只统计返回的批次页。跨页汇总时应按可比较条件合并计数，从总分子和总分母计算比例。P95 应基于原始样本重新计算。

## 与社区规范的对照

社区接口规范要求新路径采用 `/api/v<version>/<category>/<resource>`，动作使用 `:action`，并为已有接口保留兼容迁移空间。现有路径和响应字段以 OpenAPI 为准。 [社区接口规范](https://github.com/yeying-community/book/blob/main/yeying/specification/API_SPECIFICATION.md)

| 规范项 | 现状 | 后续变更的验收要求 |
| --- | --- | --- |
| 路径与分类 | 浏览器和管理接口使用旧 `/api/` 路径。分析接口已有 `v1`，缺少 `public` 段。 | 新项目读取接口使用 `/api/v1/public/projects/{project}` 这类路径。若迁移已发布接口，先提供兼容路径，更新 Web、Skill 与 Nginx 调用方，并约定旧路径退役时间。 |
| HTTP 方法语义 | 手动运行使用 POST。`GET /api/reports` 和 `GET /api/overview` 会刷新 SQLite 报告索引。 | 将报告索引更新移到发布流程或后台同步；查询从索引读取。 |
| 错误结构 | 常用错误使用 `{error}`，钱包验证使用 `{code, message, data?}`，会话探查的 `401` 使用 `{authenticated: false}`。 | 新契约定义稳定错误码、`message` 和 `request_id`。调用方使用 HTTP 状态与稳定错误码分支。 |
| 鉴权、限流和审计 | 已定义钱包、角色、同源与 CSRF、只读 Token 边界。应用和仓库 Nginx 片段未定义统一限流。成员记录含最近审批操作者，Token 记录含签发者，当前缺少完整的管理操作审计事件。 | 为新增公开接口定义限流策略，为管理操作持久化操作者、时间、目标和结果。 |
| 兼容性 | Web、Skill 和外部调用方按已部署版本访问。 | 新增可选字段可沿用版本。删除字段、变更类型或语义需版本迁移方案，并同步更新契约与调用方。 |

## 契约校验

新增或修改接口时，在同一个改动里更新 OpenAPI 的路径、分类、参数、响应与鉴权，并用实际处理器的成功及错误响应核对 schema。仓库根目录可执行以下校验。

```bash
uvx openapi-spec-validator docs/openapi.yaml
python3 -m unittest discover -s scripts -p 'test_tester_control.py'
```
