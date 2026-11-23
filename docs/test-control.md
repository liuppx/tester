# 测试机按项目触发测试

这是 Linux 测试机上的可选控制模块。报告仍在 `/report/`；受保护的 `/report/control/` 页面可以选择“全量测试”或一个项目，手动测试**当前已部署的环境**。手动测试不会拉取或部署产品代码。

控制服务每 5 分钟检查一次已启用产品仓库的上游 `main`，通过项目 checkout 的 `origin` 执行 `git ls-remote origin refs/heads/main` 获取最新 SHA。产品有新提交时，按目标 SHA 入队；仅对已启用且有对应部署适配器的项目执行源码更新和定向测试。当前已适配 `books`、`chat`、`marketplace`、`router`、`wallet`。artifact mode 下 Tester 自身的版本切换由 Deployer 标准制品升级流程负责，控制服务不轮询 Tester main。每日全量计划由 Tester 控制服务按照 runtime 配置调度，默认关闭。所有测试入口共用运行队列和运行锁，避免并发测试或部署。

控制服务使用 `TESTER_CONTROL_CONFIG` 指定项目 watcher 配置；GPU 机当前文件为 `/data/test-results-private/control/consolidated-projects.json`。该文件中的 `enabled` 只控制 main 轮询/更新，不决定 Playwright 全量项目范围，也不会启停共享中间件或其他产品服务。`agent` 不是当前 Tester 的 Playwright 项目，也没有部署适配器，不应启用。`orchestrator` 的隔离部署尚未完成；`node`、`project`、`social`、`warehouse` 也保持 watcher disabled。禁用 watcher 不会从全量 Playwright 运行中排除这些项目；全量范围和当前环境启动步骤见 [测试机架构与环境运行手册](test-machine-architecture.md)。

## 钱包身份授权

Tester 控制台使用 Web3-BS Wallet Identity 登录。后端通过一次性 nonce 验证 Presentation 的 DID 文档签名和登录签名，然后检查受保护的 DID allowlist；没有数据库。授权名单缺失、格式错误或为空时一律拒绝登录。控制台页面本身不含测试状态；状态 API、测试排队 API 均要求有效的 HttpOnly 会话 Cookie。会话有效期为 8 小时，退出或控制服务重启后会话失效，执行操作仍要求同源检查和 CSRF token。

部署时将 `config/authorized-dids.json.template` 复制为 runtime 中 `TESTER_AUTHORIZED_DIDS_FILE` 指定的外部文件，默认 `/data/test-results-private/control/authorized-dids.json`。目录设为 `700`、文件设为 `600`，文件内容格式为：

```json
{
  "dids": ["did:yeying:wid_..."]
}
```

将需要查看用例和执行测试的每个钱包身份 DID 加入数组。新用户首次尝试登录时，页面会显示经签名验证得到的 DID；管理员把该 DID 加入名单后，用户重新登录即可。编辑名单后立即生效，无需重启服务。DID 属公开身份标识，不要把钱包私钥或助记词写入配置。该文件会由 Tester 配置备份流程归档。

## 测试机配置

1. 检查 runtime 指定的项目配置文件中的仓库路径、启用状态和禁用理由。自动更新只允许有已验证适配器的项目。
2. 检查 `scripts/tester-control.service` 的工作目录、Node 路径、运行用户和端口；作为 systemd 服务安装。控制程序默认只监听 `127.0.0.1:18082`。
3. 在 HTTPS 站点的 Nginx `server` 块加入 `scripts/nginx-control-location.conf` 的 `location`，移除该控制路径上旧的 `auth_basic` 配置，然后运行 `nginx -t` 并重载 Nginx。TLS 必须在公网入口启用；应用会通过 Secure Cookie 建立登录会话。公开报告 `/report/` 继续保持只读访问，不能代理到控制接口。
4. `TESTER_CONTROL_ORIGIN` 要与用户打开页面的 HTTPS 域名一致。路径、端口、轮询间隔、报告目录和锁文件可通过 `TESTER_*` 环境变量调整；不要把凭证写在 systemd 单元、命令行或报告中。
5. artifact mode 若要自动跟进产品 `main`，runtime 中应设置 `TESTER_WATCH_UPSTREAM_MAIN=true` 和 `TESTER_ALLOW_PRODUCT_SOURCE_DEPLOY=true`，并只启用已有隔离部署适配器的项目。关闭部署许可时仍可在控制页手动测试当前部署环境。Tester 自身制品升级不由这个 watcher 执行。
6. artifact mode 要运行每日全量任务时，在 runtime 配置中设置 `TESTER_DAILY_RUN_ENABLED` 为 `true`，并指定 `TESTER_DAILY_RUN_TIME`、`TESTER_DAILY_RUN_TIMEZONE`。之后通过 `starter.sh restart` 让新配置生效。删除旧 cron 条目，避免重复触发。

Nginx 将 `/report/control/` 同源代理到仅监听本机的控制服务；Tester 的钱包身份会话保护控制页面中的状态和操作 API。部署后检查匿名访问 `/report/control/api/status` 返回 `401`，登录后可读取状态；未授权 DID 登录返回 `403`，名单为空时不允许任何登录。

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
