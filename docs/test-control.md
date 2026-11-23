# 测试机按项目触发测试

这是 Linux 测试机上的可选控制模块。报告仍在 `/report/`；钱包身份授权后的 `/report/control/` 控制台可以选择“全量测试”或一个项目，手动测试**当前已部署的环境**。手动测试不会拉取或部署产品代码。控制台内的“查看测试用例脑图”（`/report/control/cases`）按项目展示 Markdown 中的功能模块与用例，可展开节点、搜索编号/名称，并查看优先级、类型、状态、前置条件、步骤和预期结果；脑图数据与测试状态使用同一登录会话保护。

控制服务每 5 分钟检查一次已启用产品仓库的上游 `main`，通过项目 checkout 的 `origin` 执行 `git ls-remote origin refs/heads/main` 获取最新 SHA。产品有新提交时，按目标 SHA 入队；仅对已启用且有对应部署适配器的项目执行源码更新和定向测试。当前已适配 `books`、`chat`、`marketplace`、`router`、`wallet`。artifact mode 下 Tester 自身的版本切换由 Deployer 标准制品升级流程负责，控制服务不轮询 Tester main。每日全量计划由 Tester 控制服务按照 runtime 配置调度，默认关闭。所有测试入口共用运行队列和运行锁，避免并发测试或部署。

控制服务使用 `TESTER_CONTROL_CONFIG` 指定项目 watcher 配置；GPU 机当前文件为 `/data/test-results-private/control/consolidated-projects.json`。该文件中的 `enabled` 只控制 main 轮询/更新，不决定 Playwright 全量项目范围，也不会启停共享中间件或其他产品服务。`agent` 不是当前 Tester 的 Playwright 项目，也没有部署适配器，不应启用。`orchestrator` 的隔离部署尚未完成；`node`、`project`、`social`、`warehouse` 也保持 watcher disabled。禁用 watcher 不会从全量 Playwright 运行中排除这些项目；全量范围和当前环境启动步骤见 [测试机架构与环境运行手册](test-machine-architecture.md)。

## 钱包身份授权

Tester 控制服务使用内置 SQLite 管理钱包身份和成员权限，不需要另行启动数据库服务。首次启动和每次升级时会自动执行版本化迁移；数据库位于 `TESTER_PRIVATE_REPORT_DIR/control/tester-auth.sqlite3`，默认 `/data/tester/private/control/tester-auth.sqlite3`，父目录权限 `700`、数据库权限 `600`。配置 `TESTER_ADMIN_DID` 指定管理员 DID 即可。管理员用 Web3-BS Wallet Identity 登录后，可在控制台“成员授权”中批准待审核的身份或撤销已有成员。

首次部署或迁移到此版本时，在受保护的 runtime JSON 中设置唯一管理员 DID；修改后通过 Tester 的标准升级流程或 `scripts/starter.sh restart` 重启服务，使它自动建表并引导管理员记录：

```json
{
  "TESTER_ADMIN_DID": "did:yeying:wid_...",
  "TESTER_IDENTITY_ISSUER_URL": "https://node.yeying.pub"
}
```

管理 DID 必须对应已在配置的 Node issuer 完成身份注册、邮箱验证和 EVM 钱包绑定的身份。一般成员也需先完成同样的身份凭证流程：第一次登录会建立待审批账号；管理员打开“成员授权”批准后，成员重新登录即可。停止或撤销成员后，该账号现有会话立即失效。建议首次管理员登录并完成批准测试后，按常规流程备份配置和数据库。

新成员使用钱包登录后会创建“等待授权”申请，批准后身份文档固定保存于 SQLite，后续登录必须提供完全匹配的签名文档。管理员身份也会在首次成功登录时固定文档。登录要求一次性 nonce、DID 文档和登录签名，以及 Node 为该 DID 签发且状态有效的 `WalletAccountCredential`、`UsernameCredential`、`EmailCredential`。因此管理员和成员身份都要先完成社区 Node 身份注册、邮箱验证和钱包绑定；仅在客户端创建的自签名身份不会通过校验。默认信任发行方 `https://node.yeying.pub`，可通过私有 runtime 的 `TESTER_IDENTITY_ISSUER_URL` 指定社区部署的 Node origin。缺少或撤销凭证会拒绝登录。会话有效期为 8 小时，退出或控制服务重启后会话失效；执行操作仍要求同源检查和 CSRF token。

SQLite 状态通过 `scripts/config_backup.sh` 使用 SQLite 在线备份接口安全快照并归档。数据库迁移只会向前执行，不会在升级时覆盖或重建用户授权数据。

## 测试机配置

1. 检查 runtime 指定的项目配置文件中的仓库路径、启用状态和禁用理由。自动更新只允许有已验证适配器的项目。
2. 检查 `scripts/tester-control.service` 的工作目录、Node 路径、运行用户和端口；作为 systemd 服务安装。控制程序默认只监听 `127.0.0.1:18082`。
3. 在 HTTPS 站点的 Nginx `server` 块加入或更新 `scripts/nginx-control-location.conf` 的 `location`。根路径 `/` 应重定向到 `/report/control/`，控制路由包含 `auth_basic off`、`client_max_body_size 1m` 和本机代理。不要在此 location 上叠加报告专用 Cookie 或 HTTP Basic Auth 门禁：钱包身份登录由 Tester 控制服务验证，额外门禁会挡住登录请求。较大的 Wallet Identity Presentation 经 JSON 包装后可能超过 64 KiB，因此预留 1 MiB 请求体上限。修改后运行 `nginx -t` 并平滑重载 Nginx。TLS 必须在公网入口启用；应用会通过 Secure Cookie 建立登录会话。公开报告 `/report/` 继续保持只读访问，不能代理到控制接口。
4. `TESTER_CONTROL_ORIGIN` 要与用户打开页面的 HTTPS 域名一致。路径、端口、轮询间隔、报告目录和锁文件可通过 `TESTER_*` 环境变量调整；不要把凭证写在 systemd 单元、命令行或报告中。
5. artifact mode 若要自动跟进产品 `main`，runtime 中应设置 `TESTER_WATCH_UPSTREAM_MAIN=true` 和 `TESTER_ALLOW_PRODUCT_SOURCE_DEPLOY=true`，并只启用已有隔离部署适配器的项目。关闭部署许可时仍可在控制页手动测试当前部署环境。Tester 自身制品升级不由这个 watcher 执行。
6. artifact mode 要运行每日全量任务时，在 runtime 配置中设置 `TESTER_DAILY_RUN_ENABLED` 为 `true`，并指定 `TESTER_DAILY_RUN_TIME`、`TESTER_DAILY_RUN_TIMEZONE`。之后通过 `starter.sh restart` 让新配置生效。删除旧 cron 条目，避免重复触发。

Nginx 将 `/report/control/` 同源代理到仅监听本机的控制服务；Tester 的钱包身份会话保护控制页面中的状态和操作 API。部署后检查匿名访问 `/report/control/api/status` 返回 `401`，登录后可读取状态；未授权 DID 登录返回 `403`，名单为空时不允许任何登录。

Tester 前端是独立的 Vite 多页面项目，依赖、脚本和锁文件都在 `web/`。页面源码在 `web/*.html` 与 `web/assets/`，开发时运行 `cd web && npm ci && npm run dev`；它会把 `/api` 和 `/web3-bs.js` 代理到本机控制服务 `127.0.0.1:18082`。发布时 Deployer 在隔离源码中运行 `npm ci && npm run build`，由服务托管 `web/dist/` 的 HTML、哈希 CSS/JS 和运行时 Wallet SDK；开发依赖会在打包前移除。浏览器通过同一 `/report/control/` 前缀访问资源。验证 Nginx 时确认 `/report/control/` 下的 HTML、`/assets/*.css`、`/assets/*.js`、`/web3-bs.js` 均代理到控制服务，且 API 登录路径没有额外 Cookie 门禁或 HTTP Basic Auth 挑战。

## 报告与源码

全量报告仍发布到 `/report/` 及 `/report/runs/<run>/`。单项目报告单独发布到 `/report/projects/<project>/`，摘要记录触发来源、目标提交和报告链接。失败或跳过不会写成“通过”；部署失败时不会发布该提交的测试成功报告。

`scripts/test-tester-main.sh` 默认使用 `/root/code/tester-upstream` 作为受管理的独立 worktree，保留运行控制服务的 `/root/code/tester` 工作树及其本地修改。它只接受上游 `main` 的提交，并检查目录标记、仓库归属及前置依赖；现有目录不符合条件时会拒绝覆盖。其他路径可通过 `TESTER_UPSTREAM_CHECKOUT` 指定。

## 定向验证

```bash
python3 -m unittest scripts.test_tester_control -v
python3 -m unittest discover -s scripts/tests -v
bash -n scripts/run-full-on-test-machine.sh scripts/test-tester-main.sh scripts/deploy-and-test-*.sh
bash scripts/test_deploy_source_project.sh
bash scripts/test_tester_main.sh
```

部署后还要检查控制页匿名访问返回 401、认证后能读取状态，手动选择单个项目会生成对应项目报告；确认服务日志显示每日调度已启用且只存在一个日程配置。产品用例自身的失败应在报告中单独处理，不作为控制服务单测通过的证明。
