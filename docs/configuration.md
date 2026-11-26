# Tester 配置说明

Tester 有控制服务配置、Playwright 测试环境、测试项目清单和源码模式依赖图。它们的读取入口和生命周期不同，模板各维护一份，运行值保存在忽略于 Git 的配置文件中。

## 配置文件

| 仓库文件 | 用途 | 本地/部署中的实际文件 |
| --- | --- | --- |
| `config/runtime.json.template` | Tester 控制服务监听、管理员 DID、报告路径、调度和其他 `TESTER_*` 参数；也记录项目清单与 E2E 环境文件的位置 | 本地 `config/runtime.json`；部署 `/opt/deploy/tester/config/runtime.json` |
| `config/tester.env.template` | Playwright 用的产品 URL、测试账号和可选 API/钱包凭据。由 `shared/env.ts` 通过 dotenv 读取 | 本地仓库根目录 `.env.local`；GPU 测试部署 `/opt/deploy/tester/config/.env.local` |
| `config/projects.json.template` | 控制服务的上游项目 watcher 清单，例如仓库、分支、是否允许更新 | 部署 `/opt/deploy/tester/config/projects.json` |
| `config/local-test-dependencies.json` | 声明每个 E2E 项目在本地源码模式下要准备哪些仓库、产品服务及共享中间件；由 `local_test_readiness.py` 和 `local-source-services.py` 读取 | 直接随代码版本管理，不复制为运行时私有配置 |

`runtime.json` 中的 `TESTER_ENV_FILE` 是路径引用，不复制或定义 Playwright 环境变量。比如 GPU 测试部署配置为 `/opt/deploy/tester/config/.env.local`。控制服务启动时读取 `runtime.json`，Playwright 运行时再读取该路径指向的 `.env.local`。

本地开发从唯一模板初始化环境变量：

```bash
cp config/tester.env.template .env.local
```

GPU 测试部署使用相同模板初始化部署目录中的 `.env.local`。实际密钥只写入未跟踪的运行文件，并将权限设为 `600`。`runtime.json` 和 `projects.json` 分别从对应模板初始化，也应使用 `600` 权限。

## 为什么依赖清单直接入库

`local-test-dependencies.json` 描述项目间的代码和服务关系，不包含环境地址、账号或密钥。它决定本地源码测试应启动的依赖闭包，因此需要随测试代码接受 review，并在依赖关系变化时同步更新。它不是需要每台机器填写的环境模板。

## 配置备份归属

Tester 的 `scripts/config_backup.sh` 创建本地受限归档，包含运行配置、引用的环境文件、项目清单、授权 SQLite 数据库和部署服务器配置。Deployer 在 Tester 升级成功后调用该脚本，归档暂存在 `/opt/backup`，本地一致性快照写入 `/data/backups/tester/config`。

是否上传到远端由 Deployer 管理，不在 Tester 配置模板中重复声明。Deployer 上传任务读取 `/data/tester/backup.conf` 的 `BACKUP_CONF_FLAG`、任务区的模块清单和 WebDAV 配置。GPU 上已生成多个本地归档，但 `/data/tester/backup.conf` 当前关闭上传；Deployer 任务区没有实际模块清单或 WebDAV `.env`，也未发现启用的定时器。因此当前有升级触发的本地备份，没有定期远端上传。

## GPU 当前迁移状态

GPU 目前仍由旧 systemd runtime `/data/test-results-private/control/runtime-artifact.json` 指向旧目录中的 `tester.env` 和 `consolidated-projects.json`。Tester 新升级钩子会将 E2E 环境文件迁移并命名为 `.env.local`，将项目清单命名为 `projects.json`，并把 systemd runtime 入口切换到 `/opt/deploy/tester/config/runtime.json`。迁移会在部署包含该钩子的 Tester service 版本时执行。报告、授权数据库和运行状态保留在 `/data`。

敏感运行值、DID、密钥和 WebDAV 凭据不写入本文、仓库模板或测试报告。
