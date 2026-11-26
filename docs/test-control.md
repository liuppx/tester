# 控制服务与钱包身份授权

Tester 控制服务提供 `/api/`、成员授权、项目用例/依赖信息、运行队列、结果状态和受授权保护的报告访问。产品代码执行由 runner 完成。服务监听回环地址，由 Nginx 同源代理；浏览器页面和 API 均使用 `tester-web` 与控制服务的同一 HTTPS origin。

## 钱包登录与成员授权

控制服务使用 SQLite 保存 DID、管理员/成员角色、身份文档和授权状态。SQLite migration 在服务启动时自动执行；数据库路径由 `TESTER_AUTH_DATABASE` 指定，否则位于 `${TESTER_PRIVATE_REPORT_DIR}/control/tester-auth.sqlite3`。GPU 当前实例把私有报告路径配置在 `/data/test-results-private`，实际数据库为 `/data/test-results-private/control/tester-auth.sqlite3`。备份使用 `scripts/config_backup.sh` / `scripts/config-backup.py` 的 SQLite 在线备份机制，不要直接复制正在写入的 SQLite 文件。

首次配置须在 `/opt/deploy/tester/runtime.json` 中设置管理员 DID 和身份发行方，例如：

```json
{
  "TESTER_ADMIN_DID": "did:yeying:wid_...",
  "TESTER_IDENTITY_ISSUER_URL": "https://node.yeying.pub"
}
```

DID 必须已在对应 Node issuer 完成身份注册、钱包绑定、用户名和邮箱凭证验证。管理员第一次登录后可从控制台批准待审核成员或撤销成员。匿名用户不能访问测试用例、依赖信息或执行操作；匿名 `/api/auth/status` 返回 `401` 是预期结果。

### 登录失败时的检查

1. 确认 HTTPS 页面 origin 与 `TESTER_CONTROL_ORIGIN` 完全一致。
2. 确认浏览器钱包插件可用，钱包身份凭证包含 Node issuer 要求的有效凭证。
3. 确认 Node issuer URL 可达、发行方元数据/JWKS 可读取。
4. 登录已完成但页面显示等待授权时，使用管理员身份批准该 DID。
5. Nginx 的 `/api/` location 不得增加 HTTP Basic Auth 或报告 Cookie 门禁；请求体上限须覆盖 wallet presentation。

## 队列与源码 watcher

单项目、全量、每日计划和 watcher 触发的任务共用一个队列和 worker，任务按顺序执行，不会并行运行测试或部署任务。不同测试范围可以分别入队，同一范围且同一模式的重复请求会合并到现有任务。控制台展示当前任务和等待任务。选择有历史记录的范围后，“重跑上次”会重新运行该范围，重新生成报告。

`/api/status` 返回 `queue.parallelism`、当前任务和等待任务，前端可据此显示队列。公开队列字段只包含项目名、任务模式和时间，不包含内部任务 ID、日志路径或命令行参数。`TESTER_WATCH_UPSTREAM_MAIN` 控制是否轮询仓库；`TESTER_ALLOW_PRODUCT_SOURCE_DEPLOY` 控制 watcher 是否允许调用项目部署适配器。项目清单的 `enabled` 只参与 watcher，不会从 Playwright 全量集合移除项目。

当前已有隔离部署适配器的项目为 Book、Chat、Marketplace、Router、Wallet。其他项目即使配置在 Playwright 中，也不等于 watcher 能更新/部署它们。Tester 自身制品升级由 Deployer 负责，与产品 watcher 分离。

`TESTER_LOCAL_SOURCE_MODE=1` 时，手动测试调用本地源码启动器，按 `local-test-dependencies.json` 启动产品源码 worktree；默认制品模式连接已部署产品。两种模式下报告都应记录运行模式和被测 commit。

## 报告目录与访问

报告正文和附件继续以静态文件保存在 `TESTER_REPORT_DIR`，SQLite `tester_reports` 表维护项目、运行时间、结果计数和对应文件目录。控制服务启动时自动执行 migration，并在报告列表读取时同步已发布的 `summary.json`。旧报告因此可自动进入目录，无需重新生成。

浏览器使用同源 `/api/reports` 读取报告目录，并通过 `/api/reports/files/<相对路径>` 查看 HTML 和下载报告文件。两个 API 都要求有效钱包会话。Nginx 在 `/report/` 提供 `tester-web` 中的报告中心页面，并将具体 `/report/<相对路径>` 转到报告文件 API；报告数据和详情都使用同一个 Tester 登录态，不依赖独立报告 Cookie。服务端只允许读取报告根目录内的普通文件，并拒绝路径穿越和符号链接。

GPU 当前 runtime 中 `TESTER_WATCH_UPSTREAM_MAIN=1`、`TESTER_ALLOW_PRODUCT_SOURCE_DEPLOY=1`；每日运行配置未设置，因此使用默认关闭状态。启动产品 watcher 前必须确认项目有隔离部署适配器并完成单项目 dry-run/回滚验证。

## 常用维护命令

检查控制 service 与健康状态：

```bash
systemctl is-active tester-control.service
/opt/deploy/tester/scripts/health-check.sh --level readiness --format json
/opt/deploy/tester/scripts/health-check.sh --level all --format json
curl -i https://tester.tidukongjian.com/api/auth/status
```

修改 runtime 后先备份，再通过安装包的 starter 重启：

```bash
/opt/deploy/tester/scripts/config_backup.sh
cd /opt/deploy/tester
scripts/starter.sh restart
scripts/health-check.sh --level all --format json
```

产品 URL、私钥、API key 和 webhook secret 放在 `/opt/deploy/tester/.env.local` 等权限受限配置中。服务端和产品 watcher 的真实值不要粘贴到公开文档、命令输出、报告或 PR。

部署、Nginx、升级、回滚详见[部署手册](deployment.md)；GPU 产品服务和中间件状态详见[测试环境现状](test-machine-architecture.md)；源码服务启动详见[本地源码测试模式](local-source-mode.md)。

## Agent 质量分析

管理员用钱包登录控制台，在「成员授权」中的「Skill 分析 Token」生成凭据。Token 有效期可设为 1 至 90 天，默认 30 天。控制台仅在创建时返回明文，SQLite 保存摘要和到期时间。管理员可在相同面板撤销凭据。

将控制台 HTTPS origin 和 Token 配置为 Agent 的 `YEYING_TESTER_URL`、`YEYING_TESTER_TOKEN`。Bearer 凭据的读取范围为 `/api/v1/analysis/`，包括总体概览、项目用例和配置、批次及稳定性。网页报告继续使用钱包会话。配置接口提供环境变量名和配置状态，依赖可用性应结合运行结果确认。

每次受管测试写入私有批次元数据和 Playwright 执行记录，记录运行模式、可用的源码版本、运行配置、用例 ID、重试和耗时。旧批次按保留的 JUnit 或报告汇总提供历史数据。部署更新时同时升级 `tester-service` 与 `tester-web`，服务启动时自动迁移 Token 表。Agent 使用方式和用例规范见 [Marketplace 的 yeying-tester](https://github.com/yeying-community/marketplace/tree/main/skills/yeying-tester)。
