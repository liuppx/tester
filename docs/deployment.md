# Tester 部署手册

> 文档状态：维护中
> 适用环境：采用 Deployer 标准制品升级的 Tester 环境
> 维护对象：Tester 控制服务、测试运行器和控制台前端
> 打包规范：`book/yeying/specification/PACKAGING.md`
> 部署规范：`book/yeying/specification/DEPLOYMENT.md`
> 升级规范：`book/yeying/specification/UPGRADING.md`
> 配置文件说明：[configuration.md](configuration.md)

本手册只说明如何部署已经生成的 Tester 安装包。打包输入、制品命名、manifest、SHA-256 和不可变制品规则以社区打包规范为准；本项目不从源码目录直接作为正式部署对象。

GPU 测试机本次核对的运行状态见[测试环境现状](test-machine-architecture.md)，避免在部署手册维护一份很快过期的服务清单。当前 GPU service 为 `0.1.20`、静态 Web 为 `0.1.19`；两者独立发布，升级/排障时分别核对 manifest 与 source commit。

## 1. 部署对象

Tester 由一个 `service` 制品和一个 `static` 制品组成。service 包含 Playwright 测试运行器、Python 控制服务、报告处理脚本和浏览器运行时；static 包含控制台页面、资源和钱包 SDK。控制服务监听本机回环地址，Nginx 提供静态页面并只代理 API。

制品命名规则：

```text
tester-service-v<major>.<minor>.<patch>-<source-commit-7>.tar.gz
tester-web-v<major>.<minor>.<patch>-<source-commit-7>.tar.gz
```

包内固定入口：`scripts/starter.sh`、`scripts/health-check.sh`、`scripts/test.sh`、`scripts/upgrade.sh`。

## 2. 安装包信息

通过受控发布入口生成制品：

```bash
cd /root/code/deployer/operator/package
./compile_packages.sh --publish
```

发布完成后核对 `release-manifest.json` 中的 `source_commit`、归档名和 SHA-256。制品由标准发布流程上传到配置的制品库；目标机上的 Deployer 升级器下载并验证该不可变制品。不要手工重命名包、覆盖同名不同字节包或从工作区直接复制文件。

### 制品边界

`release-manifest.json` 同时声明 `tester-service` 和 `tester-web`。service 安装到 `/opt/deploy/tester`，static 按标准静态制品目录安装到 `/opt/deploy/static/tester-web/current`。两个制品可以独立升级；页面由 Nginx 提供，API 仍由 Tester control service 提供。

```plantuml
@startuml
title Tester 制品交付边界
actor Maintainer as M
rectangle "社区 main / release tag" as Git
component "compile_packages.sh" as Build
artifact "tester-service-v<version>-<sha>.tar.gz" as Package
artifact "tester-web-v<version>-<sha>.tar.gz" as Static
component "standard upgrade" as Upgrade
node "测试机" as Host
M --> Git : 合并并批准
Git --> Build : 已提交 commit
Build --> Package : 构建 + manifest + SHA-256
Build --> Static : 构建 + manifest + SHA-256
Package --> Upgrade : 制品库
Upgrade --> Host : service 解包、配置、启动、健康检查
Static --> Upgrade : static 制品升级
Upgrade --> Host : static current 链接切换
@enduml
```

### 从旧合包版本或带路径前缀的版本迁移

旧 service 制品曾同时托管页面；新 service 只提供 API。迁移时先在 Deployer 的 `operator/upgrade/static-units.conf` 中启用以下行（最后一列替换成实际 HTTPS 入口），使标准升级器能发现静态单元：

```text
tester tester-web tester-web https://<tester-domain>/
```

迁移时删除旧的根路径重定向和控制台代理块，将本仓库 Nginx 片段加入实际 HTTPS server。`/report/` 由 `tester-web` 静态制品提供报告中心页面，具体 `/report/<path>` 报告地址仍由 Nginx 转到钱包会话保护的报告文件 API，移除旧的独立报告 Cookie 门禁。然后进入维护窗口，校验并平滑重载 Nginx，再运行 Deployer 的标准 `upgrade.sh`。这一步切换到尚未安装的 `current` 静态目录后，控制台会短暂不可用，直到升级器部署 `tester-web` 并通过 `/index.html` 探活。升级失败时，按已备份的 Nginx 配置和 service 制品回滚。控制台地址为 `https://tester.tidukongjian.com/`，API 为 `/api/`，静态探活为 `/index.html`，报告入口为 `/report/`。后端 Cookie 路径为 `/`，升级后重新钱包登录。`TESTER_CONTROL_ORIGIN` 仍为 `https://tester.tidukongjian.com`，无需因路径迁移改变。升级完成后核对两个制品版本，并额外验证页面、钱包 SDK、报告中心和具体历史报告；控制服务自身健康检查只验证 service。

## 3. 目标环境准备

目标机器必须符合社区 `book/yeying/specification/MACHINE.md` 的目录、权限和磁盘要求，并准备：

- Linux x86_64、Python 3.10+、Node.js 24.18+。
- Bash、coreutils、`jq`、`flock`、Xvfb、Bubblewrap 以及 Chromium 所需系统库。
- `/opt/deploy/tester` 作为稳定入口；版本目录由标准升级器创建。
- `/data/test-results` 保存公开报告；`/data/test-results-private` 保存私有报告、数据库和运行状态，不存放长期配置文件。
- `runtime.json`、`projects.json` 和 `.env.local` 由项目升级钩子复制到每个服务版本的 `config/`，稳定入口为 `/opt/deploy/tester/config/`。配置目录权限为 `700`，配置文件权限为 `600`。
- `/data/tester` 保存 PID、运行锁和临时数据；`/opt/logs` 保存服务日志。
- 受保护的 runtime、项目配置、测试环境变量和管理员 DID。凭据文件权限为 `600`，私有目录权限为 `700`。
- 产品 E2E 所需的产品服务和公共中间件。Tester 负责排队、部署适配器和运行用例，不负责自动启动所有产品及中间件。

## 4. 配置初始化

Tester 的控制服务配置、项目清单和 E2E 环境文件统一放在 `/opt/deploy/tester/config/`。`/opt/deploy/tester` 是指向当前不可变版本目录的稳定软链接。每次升级由项目 `scripts/upgrade.sh copy-config` 使用 Shell 和 `jq` 将当前配置复制到目标版本，并保留目标版本已存在的文件。首次迁移时，该钩子把旧的 `consolidated-projects.json` 转为标准名 `projects.json`，并把旧 `tester.env` 迁移为 `.env.local`。钩子同时通过 systemd drop-in 将 runtime 入口更新到 `/opt/deploy/tester/config/runtime.json`。配置完成标准迁移后的路径如下：

```bash
/opt/deploy/tester/config/runtime.json
/opt/deploy/tester/config/projects.json
/opt/deploy/tester/config/.env.local
```

确认当前链接、systemd runtime 路径和权限：

```bash
readlink -f /opt/deploy/tester
systemctl show tester-control.service --property=Environment --value
stat -c '%a %n' /opt/deploy/tester/config/runtime.json /opt/deploy/tester/config/projects.json /opt/deploy/tester/config/.env.local
```

`config/runtime.json.template` 提供控制服务配置项，`config/tester.env.template` 提供 Playwright 产品 URL 和测试凭据的环境变量名。两者由各自的运行入口读取，内容不重复。本地开发将 E2E 模板复制为仓库根目录 `.env.local`；GPU 测试部署将它复制为 `/opt/deploy/tester/config/.env.local`。Tester service 使用 `TESTER_ENV_FILE` 显式指向部署文件。控制服务模板复制为 `/opt/deploy/tester/config/runtime.json`。升级钩子负责把这些实际配置复制到新版本。不要把真实身份 DID 或凭据写进公开文档。

运行配置至少确认：

- `TESTER_CONTROL_CONFIG` 指向项目 watcher 配置。
- `TESTER_PRIVATE_REPORT_DIR`、`TESTER_REPORT_DIR`、`TESTER_PID_FILE` 和 `TESTER_LOCK_FILE` 指向持久目录。
- `TESTER_CONTROL_ORIGIN` 与用户访问控制台的 HTTPS origin 完全一致。
- `TESTER_ADMIN_DID` 是已经完成 Node 身份注册、邮箱验证和钱包绑定的管理员 DID。
- `TESTER_IDENTITY_ISSUER_URL` 指向受信任的身份发行方。
- `TESTER_ENV_FILE` 指向 `/opt/deploy/tester/config/.env.local`；其中的 API key、钱包私钥和 Webhook secret 不写入报告。

GPU 测试机当前仍在迁移前状态，systemd runtime 和 E2E 配置位于 `/data/test-results-private/control/`。下次部署包含新 `copy-config` 钩子的 Tester service 制品时，项目升级阶段会将配置迁入当前和目标版本目录，更新 runtime 中的稳定配置路径，并保留旧源文件用于回滚。新版本健康检查与配置备份通过后，再清理旧配置副本。不要把 `/data/test-results-private` 中的报告、SQLite 数据库或运行状态移动到部署目录。

Tester 授权数据库是服务私有 SQLite 文件，启动时自动执行 `scripts/migrations/` 中未执行的迁移。它不需要另行启动 PostgreSQL；路径由 `TESTER_AUTH_DATABASE` 指定，否则为 `${TESTER_PRIVATE_REPORT_DIR}/control/tester-auth.sqlite3`。GPU 当前 service 使用 `/data/test-results-private/control/tester-auth.sqlite3`。以 systemd 实际环境和数据库文件为准，不要按仓库默认值猜测线上路径。

## 5. 部署和启动

标准升级器执行下载、校验、解包、保留配置、停止旧版本、启动新版本、健康检查和稳定链接切换。升级成功后通过稳定入口执行：

```bash
cd /opt/deploy/tester
scripts/starter.sh start
scripts/health-check.sh --level readiness --format json
scripts/health-check.sh --level all --format json
scripts/test.sh --suite smoke --format json
```

如果使用 systemd 管理控制服务，单元只调用安装包内的 `scripts/starter.sh`；不要把产品服务塞进 Tester 的 systemd 单元。

Nginx 必须包含项目提供的 [`scripts/nginx-control-location.conf`](../scripts/nginx-control-location.conf)：站点根路径 `/` 直接提供控制台，静态页面从 `/opt/deploy/static/tester-web/current` 提供，只有 `/api/` 代理到 `127.0.0.1:18082`。钱包登录请求不受报告 Cookie 或 Basic Auth 额外拦截，`client_max_body_size` 至少为 `1m`。修改后执行：

```bash
nginx -t
# 使用实际运行中的 Nginx master 做平滑重载
nginx -s reload
```

```plantuml
@startuml
title Tester 请求与登录时序
actor User
participant Browser
participant Nginx
participant "Tester control" as Control
participant "Web3-BS wallet" as Wallet
participant "Node Identity" as Identity
database "Tester SQLite" as DB
User -> Browser : 打开 /
Browser -> Nginx : GET /
Nginx --> Browser : 200 控制台 HTML
Browser -> Nginx : GET /assets/ 和 /web3-bs.js
Nginx -> Control : 仅代理 API
Browser -> Control : 创建登录 session
Browser -> Wallet : 请求钱包身份签名
Wallet -> Identity : 获取/证明身份凭证
Identity --> Wallet : 有效 Presentation
Wallet --> Browser : 签名结果
Browser -> Control : 提交 Presentation
Control -> Identity : 验证发行方和凭证
Control -> DB : 查询授权状态
DB --> Control : admin / active / pending
Control --> Browser : Secure session cookie 或待审批
@enduml
```

## 6. 验证方式

公网入口至少验证：

```bash
curl -I https://<tester-domain>/
curl -i https://<tester-domain>/api/auth/status
curl -I https://<tester-domain>/web3-bs.js
```

预期结果：根路径直接返回 `200` 控制台页面；匿名认证状态 `401`；Wallet SDK 返回 `200`。登录后再确认控制台能读取 `/api/status`、`/api/cases` 和 `/api/architecture`，管理员能看到成员授权入口。

E2E 环境按项目依赖图和测试机架构手册逐项确认。Tester 自身 `health-check.sh --level all` 只证明 Tester runner 和控制服务可用，不代表所有产品服务、中间件或跨项目依赖已启动。

## 7. 备份、迁移和回滚

Deployer 在每次服务升级成功后调用 Tester 的 `scripts/config_backup.sh`，生成权限受限的本地配置归档，并调用自己的 `operator/backup-config/upload_config_backup.sh` 尝试上传。Tester 负责创建本地归档；是否远端上传由 Deployer 在 `/data/tester/backup.conf` 的 `BACKUP_CONF_FLAG` 决定。不要在 Tester 仓库维护第二份上传开关模板。

需要在升级外手动创建本地备份时执行：

```bash
/opt/deploy/tester/scripts/config_backup.sh
```

本地备份包含 runtime、项目配置、环境变量、Tester SQLite 授权数据库、systemd unit/drop-in 和 Nginx 配置；不包含测试结果、运行日志和公开报告。归档暂存在 `/opt/backup`，备份清单和一致性快照存于配置的本地备份目录。

远端上传由 Deployer 任务区负责，需要部署节点配置 `operator/backup-config/backup.conf` 模块列表、WebDAV `.env`、`/data/tester/backup.conf` 上传策略及受控调度。GPU 当前只在每次 Tester 升级后生成了本地归档；上传策略为关闭状态，Deployer 任务区尚无实际 `backup.conf`、`.env` 或定时器，因此归档没有远端上传。

Tester 当前 migration 是本地 SQLite 授权表迁移，由控制服务启动时执行。迁移失败时服务必须保持失败，不得继续接收登录或测试操作；先保留旧版本和备份，再按升级规范安全重试或执行经评审的 forward-fix。健康检查通过仅证明 Tester 自身检查项通过，不能证明产品 E2E 依赖齐备。

回滚使用 Deployer 的标准升级流程选回上一个完整制品版本；保留旧版本目录和相应配置快照。不要手工替换稳定链接绕过 starter 和健康检查。执行入口为部署环境中 Deployer 的 `operator/upgrade/upgrade.sh`：

```bash
cd <deployer-checkout>/operator/upgrade
./upgrade.sh
```

升级器健康检查失败时保持旧稳定链接；人工回滚后再次执行 readiness、all 和 smoke 检查，并确认控制台登录、报告读取和运行锁均恢复。

## 8. 常见问题

### 根路径显示 Welcome to nginx

HTTPS server 没有指向 Tester 静态目录，命中了默认站点。将 `scripts/nginx-control-location.conf` 的根路径 location 合并到实际监听 443 的 Nginx 配置，执行 `nginx -t` 并平滑重载真正的 master。

### `/` 返回 403

检查是否仍有报告 Cookie 门禁或 Basic Auth 规则覆盖控制 location。钱包登录由 Tester 控制服务负责，控制 location 不应复用报告访问 Cookie。

### 页面打开但脚本 404

确认 `tester-web` static 制品已升级且 Nginx 指向其 `current` 目录；确认 API 代理到同版本 control service。不要从旧 checkout 补拷贝前端文件。

### 登录后提示等待授权

管理员 DID 尚未批准该身份。管理员先完成钱包登录，再在“成员授权”中批准；确认 Node issuer、邮箱验证和钱包绑定状态有效。

### smoke 或 E2E 大量连接失败

先查看项目依赖图和测试机架构手册，逐个检查产品服务与公共中间件 readiness。Tester 不会因为自身启动成功而自动启动缺失依赖。

## 9. 相关文档

- [测试控制台和钱包授权](test-control.md)
- [测试机架构与服务启动](test-machine-architecture.md)
- [项目与 E2E 依赖图](project-dependency-map.md)
- [测试报告和调度](test-machine-report.md)
- [全部文档入口](README.md)
- 社区打包规范：`book/yeying/specification/PACKAGING.md`
- 社区部署规范：`book/yeying/specification/DEPLOYMENT.md`
- 社区升级规范：`book/yeying/specification/UPGRADING.md`
