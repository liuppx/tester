# Tester 配置说明

Tester 的运行配置模板、E2E 环境模板和源码测试依赖图统一放在仓库根目录。私有运行值使用同目录中的实际配置文件，模板各维护一份，避免重复定义。

## 配置文件

| 仓库文件 | 用途 | 本地/部署中的实际文件 |
| --- | --- | --- |
| `runtime.json.template` | 控制服务监听、管理员 DID、报告路径、调度等 `TESTER_*` 参数，并引用项目清单和 E2E 环境文件 | 本地 `runtime.json`；部署 `/opt/deploy/tester/runtime.json` |
| `tester.env.template` | Playwright 产品 URL、测试账号及可选 API/钱包凭据，由 `shared/env.ts` 通过 dotenv 读取 | 本地 `.env.local`；部署 `/opt/deploy/tester/.env.local` |
| `projects.json.template` | 控制服务 watcher 的项目、仓库、分支和启用状态 | 本地 `projects.json`；部署 `/opt/deploy/tester/projects.json` |
| `local-test-dependencies.json` | 本地源码测试的项目、代码仓库、服务和共享中间件依赖图，由 readiness 和源码服务管理脚本读取 | 直接随代码版本管理，不复制为机器私有配置 |

`TESTER_ENV_FILE` 只是 `runtime.json` 中指向 `.env.local` 的路径，不会重复定义 Playwright 环境变量。两类配置分别由控制服务和 Playwright 读取。

## 初始化

本地 E2E 环境：

```bash
cp tester.env.template .env.local
```

需要本地运行控制服务时，再初始化控制服务配置和 watcher 清单：

```bash
cp runtime.json.template runtime.json
cp projects.json.template projects.json
```

GPU 部署配置由升级钩子保存在稳定入口 `/opt/deploy/tester` 根目录。实际密钥只写入未跟踪的 `.env.local`，配置文件权限设为 `600`，配置目录权限设为 `700`。`runtime.json` 和 `projects.json` 按对应模板初始化。

## 依赖图为何入库

`local-test-dependencies.json` 描述项目间代码和服务依赖，不包含环境地址、账号或密钥。它决定本地源码测试需准备的依赖闭包，应随代码接受 review，并在项目依赖变化时同步更新。它不是环境变量模板。

## 配置备份归属

Tester 的 `scripts/config_backup.sh` 创建本地受限归档，包含运行配置、引用的环境文件、项目清单、授权 SQLite 数据库和部署服务器配置。Deployer 在 Tester 升级成功后调用该脚本，归档暂存在 `/opt/backup`，本地一致性快照写入 `/data/backups/tester/config`。

是否上传到远端由 Deployer 管理。Deployer 上传任务读取 `/data/tester/backup.conf` 的 `BACKUP_CONF_FLAG`、任务区的模块清单和 WebDAV 配置。GPU 上已生成多个本地归档，但 `/data/tester/backup.conf` 当前关闭上传，Deployer 任务区没有实际模块清单或 WebDAV `.env`，也未发现启用的定时器。因此当前有升级触发的本地备份，没有定期远端上传。

## GPU 旧配置迁移

GPU 当前 systemd runtime `/data/test-results-private/control/runtime-artifact.json` 仍引用旧文件 `tester.env` 和 `consolidated-projects.json`。Tester 新升级钩子会将其迁移为 `/opt/deploy/tester/.env.local` 和 `/opt/deploy/tester/projects.json`，并将 runtime 配置迁移到 `/opt/deploy/tester/runtime.json`。迁移会在部署包含该钩子的 Tester service 版本时执行。报告、授权数据库和运行状态继续保留在 `/data`。代码包内的配置模板及依赖图直接位于仓库根目录。

敏感运行值、DID、密钥和 WebDAV 凭据不写入本文、仓库模板或测试报告。
