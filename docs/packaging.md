# Tester 发布包

Tester 作为一个 `service` 单元交付：包内 Playwright 运行器、报告处理脚本与 Python 控制服务共同版本化。生成的 HTML、JUnit、截图、运行日志和凭据属于外置数据。代码接入与本地验收不表示服务器已迁移，也不会自动安装 systemd 服务单元、改 Nginx 或启用发布。

## 构建接口

构建要求 Linux x86_64、Python 3.10+、Node.js >=24.18 和 npm：

```bash
scripts/package.sh --ref "$RELEASE_COMMIT" --source-commit "$MAIN_COMMIT" --version 0.1.2 --output-dir "$EMPTY_OUTPUT"
```

从指定已提交版本提取临时源码，使用 npm 安装依赖且不生成 package-lock.json，下载对应 Chromium，输出版本化 tar.gz 和 release-manifest.json。仓库按项目约定不保留依赖锁文件，因此依赖版本按 package.json 的 semver 范围解析。包中带 node_modules 与浏览器，不需要运行机的开发缓存；Node、Python、Chromium 系统库，以及原测试执行脚本使用的 Bash/coreutils、flock、Xvfb 和 bubblewrap 由部署环境准备。构建不会 sudo、fetch、切换调用者分支、打 tag、推送、上传或部署。

package-api.json 声明四个统一参数。构建输入的 tag commit 与 main source commit 分开记录；归档名与清单身份采用 main source commit。输出目录必须为空，不覆盖已有不同字节的制品。构建默认将 Playwright 浏览器缓存到打包用户的 `~/.cache/yeying-tester/playwright`，后续版本复用相同的浏览器 revision；npm 默认使用 `https://registry.npmmirror.com`，Playwright 浏览器默认使用 `https://npmmirror.com/mirrors/playwright`，两者都可用 `NPM_CONFIG_REGISTRY` 和 `PLAYWRIGHT_DOWNLOAD_HOST` 覆盖。可用 `TESTER_BUILD_BROWSER_CACHE` 指定其他持久化缓存目录。安全配置统一使用完整 Chromium channel，因此打包命令使用 `--no-shell`，不下载单独的 headless shell；安装器只把当前版本需要的 Chromium 和 ffmpeg 目录复制入包，避免把旧 revision 一起打入制品。运行时仍不依赖打包机缓存。

独立的 scripts/tag.sh 在获批发布事件中调用，支持 --bump patch|minor|major、--unit-version tester-service=版本。它在临时 worktree 更新 package.json 与 artifact-versions.conf，仅推送 tag，不改 main。package.sh 不调用它。正式发布通过 Deployer 的 compile_packages.sh --publish；开发验收不需要推送任何真实 tag。

## 构建结果核对

验证发布链路时，以 PR 合并后的 main 提交为起点，核对构建日志与 `release-manifest.json` 中的 `source_commit`，并确认对应版本归档已生成、SHA-256 与清单一致。创建 tag 或出现“开始打包”日志不代表构建成功；是否自动触发，以发布节点的实际日志和制品为准。

## 目录与运行

从 config/*.template 创建 runtime.json、projects.json 和部署环境 .env.local，私有文件权限设为 600。runtime.json 中的值是部署环境配置，命令行显式 --config 与现有进程环境变量可覆盖。测试账号和飞书凭据只存受保护配置，不放入发布包。

模板使用：
- 稳定服务入口 /opt/deploy/tester，实际解包到独立版本目录。
- 报告 /data/test-results；私有状态 /data/tester/private；临时结果 /data/tester/runtime。
- PID /data/tester/control.pid；操作日志 /opt/logs/tester-control.log。
- 日志持续轮转：默认每份 1 MiB、保留 3 份备份；TESTER_LOG_MAX_BYTES、TESTER_LOG_BACKUPS 可调整。
- API 控制服务和每日全量测试任务由同一个 Tester 控制服务管理；默认关闭每日任务。生产启用时设置 `TESTER_DAILY_RUN_ENABLED=true`，并使用 `TESTER_DAILY_RUN_TIME` 和 `TESTER_DAILY_RUN_TIMEZONE` 配置时间。不要同时保留旧的 `/etc/cron.d/yeying-tester-full`，避免重复运行。
- 升级后的 `scripts/config_backup.sh` 会把 runtime、项目、环境、通知、Tester systemd unit 及其 drop-in 配置备份到 `TESTER_CONFIG_BACKUP_DIR`；报告、日志和测试结果保存在各自的数据目录，不纳入配置备份。

```bash
scripts/starter.sh start
scripts/health-check.sh --level all --format json
scripts/test.sh --suite smoke --format json
scripts/starter.sh stop
```

start 检查监听端口及本次启动进程身份；stop 确认 PID 属于本版本的进程组后再发送信号，等待退出，必要时强制结束。restart 只有停止成功才启动。健康响应包含版本与源码 commit，避免把旧进程当作新版本成功。

当每日测试正在运行时，`starter.sh stop` 会检查 Tester 的运行锁并拒绝停止；Deployer 会在后续检查周期重试升级，不会切断正在执行的测试。

artifact mode 固定执行包内版本，不拉取 Tester main 或部署产品源码。原源码模式保留。项目模板默认关闭源码部署轮询；模板中的源码路径不代表自动获得部署权限。

## 健康检查和测试

health-check.sh 支持 liveness、readiness、dependency、all，及 --timeout、--retries、--interval、--format、--quiet、--config、--base-url。仅健康检查可显式重试。

test.sh 无参数执行 unit，支持：
- unit：离线脚本测试。
- integration：使用包内 Chromium 完成本地页面断言，不访问产品。
- smoke：本服务健康/版本、状态读取和未授权排队请求拒绝。
- e2e：调用已有跨产品 Playwright 测试；必须提供明确的 TESTER_ENV_FILE，生产环境拒绝执行。
- all：依次执行上述适用套件，不能把未执行或全跳过当作成功。

integration、e2e 和报告 runner 统一通过 `scripts/browser-runtime.sh` 启动浏览器。
Linux 必须提供 Bubblewrap，未设置 DISPLAY 时还必须提供 Xvfb。隔离空间内的整个
`/sys` 为空且只读，`/dev` 不暴露 NVIDIA/DRM 设备，`/run/udev` 为空；只隐藏
PCI/DRM 索引不能阻断 `/sys/devices/.../uevent` 探测。Wallet 自建的 persistent
context 同样使用软件渲染参数。隔离失败时拒绝启动，不回退到宿主 GPU。
这不会修复或回收主机上已存在的内核 D 状态进程，也不会重启任何服务。
GPU 主机升级时，应检查 host-overlay 不会把旧的 `run-full-on-test-machine.sh`、
浏览器启动 helper 或 safe config 覆盖回来；以部署目录实际文件和上述隔离验证为准。

统一参数：--suite、--timeout、--format text|json|junit、--output、可重复 --case、--fail-fast、--keep-data、--quiet、--help。另有 --environment、--config、--project、--base-url、--list-cases。用例 ID 不存在返回 2；失败返回 1、框架异常 3、超时 4、清理失败 5。测试失败不自动重试。--keep-data 仅供非生产本地 fixture 调试；跨产品 E2E 保持既有清理策略，拒绝该选项。

```bash
scripts/test.sh --suite smoke --list-cases
scripts/test.sh --suite smoke --case SMOKE-TESTER-002 --format json
scripts/test.sh --suite e2e --project router --environment test --timeout 600 --format junit --output /data/tester/runtime/router.xml
```

--case 的产品用例优先使用源码已有 ID，否则使用由项目、相对文件路径和标题计算的稳定 ID；执行前枚举和核对选择结果。跳过独立计数，全部跳过返回非零。JSON 标准输出仅包含报告，诊断进入 stderr。--timeout 是整次测试预算；超时和中断会停止本次子进程组并尝试清理临时数据。

需要已有网页报告、脱敏发布与飞书流程时，使用 scripts/run-tests.sh [--project router] --config 路径；它读取本版本配置后调用既有运行脚本。该操作会执行真实产品用例及配置的通知，不能当作无副作用健康检查。

## 升级与回退

scripts/upgrade.sh 提供 prepare、copy-config、post-copy、finalize 四阶段；仅复制缺失配置，不覆盖目标已配置值或外置结果。通用升级器通过 `starter.sh` 停止旧 Tester、启动新版本；每日任务随服务一起停止和启动。健康检查通过后切换稳定链接；配置由 `config_backup.sh` 备份。Deployer 负责轮询制品、下载和调用项目升级钩子，不另行管理 Tester 的日常测试时间表。

GPU 测试机历史上的 `host-overlay` 是独立于 release archive 的主机适配层，不是 Nginx overlay，也不是普通配置文件。`overlay.json` 为每个附加文件记录基础文件 SHA-256、目标 SHA-256 和可选 patch；升级时校验这些值，再把文件应用到已解包的 release，并写入 `host-adaptations.json`。因此最终部署目录可能包含 manifest 没有列出的 GPU 主机改动，且 overlay 变更会产生不同的部署目录后缀。GPU 机当前仍有 48 条此类条目，覆盖产品 E2E、Tester 控制脚本和 runner 适配，所以运行目录中的代码可能与发布包源码不同。它可以用于短期的单机测试兼容；共享测试修复应并回 Tester 源码，服务启停、配置备份和定时任务应由 Tester 包内脚本管理。上游源码已经包含某项修复后，应移除相应 overlay 条目，防止旧 patch 再应用。

首次接入前需确认浏览器系统库、Xvfb/bubblewrap、受保护报告路由、数据目录权限、账号与调度配置。现有 GPU 测试环境不会因这次开发自动迁移。TypeScript 全量检查仍有与原 main 相同的既有错误；接入审计必须保留这一项，不能声称所有仓库检查已通过。

停止等待使用规范变量 STOP_TIMEOUT_SECONDS（默认 30）与 FORCE_STOP_TIMEOUT_SECONDS（默认 5）；兼容旧的 TESTER_STOP_TIMEOUT 和 TESTER_FORCE_STOP_TIMEOUT，规范变量优先。两者也可写入 runtime.json，必须为非负整数字符串。
