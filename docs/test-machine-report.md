# 测试报告与调度

报告流水线将 Playwright 结果按项目归档，生成脱敏 HTML、JUnit 摘要和可选飞书通知。原始日志与 JUnit 保存在私有目录，不对公网提供。

GPU 现状（2026-11-25）：Tester 控制服务和 runner 健康检查通过，但全量和 Router 报告最近更新时间仍是 2026-11-21/2026-11-17；runtime 未配置每日运行项，默认为关闭。不能把旧报告视为本日测试结果。完整现状见[测试环境现状](test-machine-architecture.md)。

## Requirements

Runner 需要 Linux、Node/npm 依赖、Python 3.10+、GNU `timeout`、`flock`、Xvfb、Bubblewrap (`bwrap`) 和 Chromium 系统库。安装制品模式使用受保护的 `TESTER_ENV_FILE`；本地开发可按 README 配置 `.env.local`。GPU 标准制品安全全量通过 `playwright.safe.config.ts` 排除 Knowledge；本地源码模式包含 Knowledge。shell runner 当前单 worker，直接运行 safe config 时默认两 worker；具体入口和环境模式须随报告记录。

All browser execution goes through `scripts/browser-runtime.sh`, including the
standard `test.sh --suite integration|e2e|all` entrypoints. On Linux it creates a
Bubblewrap mount namespace with an empty, read-only `/sys`, a minimal `/dev`
without NVIDIA/DRM nodes, and an empty `/run/udev`. Masking only
`/sys/bus/pci/devices` and `/sys/class/drm` is insufficient: libudev can still
read `/sys/devices/.../uevent` and block in a host driver. Network access and
repository paths remain available for real product and wallet-extension tests.
These mounts affect only the runner and its children, not the host.

Software-rendering flags and environment settings are supplemental. They do
not guarantee the absence of a Chromium GPU subprocess; the namespace prevents
that subprocess from enumerating host GPUs. The Wallet persistent-context
helper uses the same policy. Missing Bubblewrap/display dependencies or a
failed isolation check stop execution without falling back to host devices.
Listing tests does not launch browsers or require the namespace.

To check browser execution without product services or notifications:

```bash
python3 -m unittest scripts.test_browser_runtime -v
bash scripts/browser-runtime.sh node node_modules/@playwright/test/cli.js test \
  --config=scripts/browser-isolation.config.ts
```

The second check renders and clicks a page, then loads a synthetic MV3 extension
using the real Wallet launch helper. Existing D-state processes are not cleared
by this change; the runner's existing stuck-process preflight still applies.

全量 runner 检查可用内存、磁盘空间和 D-state Chromium 进程。非阻塞 run lock 防止并发，单次执行上限 2.5 小时。preflight 因资源或残留进程跳过时应记录为 skipped，不能当成测试通过。

## Configuration

| Variable                    | Default                               | Purpose                                          |
| --------------------------- | ------------------------------------- | ------------------------------------------------ |
| `TESTER_NODE_BIN`           | `node` on `PATH`                      | Node.js executable                               |
| `TESTER_REPORT_DIR`         | `/data/test-results`                  | Public report directory                          |
| `TESTER_PRIVATE_REPORT_DIR` | `/data/test-results-private`          | Private reports, databases and runtime state     |
| `TESTER_LOCK_FILE`          | `/run/lock/yeying-tester-full.lock`   | Run lock; parent must exist and be writable      |
| `TESTER_STUCK_CHROME_LIMIT` | `0`                                   | Maximum existing D-state Chromium processes      |
| `TESTER_FEISHU_CONFIG`      | `feishu.env` in the private directory | Optional private notification settings           |
| `TESTER_FEISHU_WEBHOOK_URL` | unset                                 | Intended report group's robot webhook            |
| `TESTER_FEISHU_SECRET`      | unset                                 | Optional robot signing secret                    |
| `TESTER_REPORT_URL`         | unset                                 | Public HTTPS report directory, including subpath |
| `TESTER_REPORT_ACCESS_KEY_FILE` | `/data/test-results-private/control/admin-password` | Optional report/control access secret to redact from exports |

Notification settings can be environment variables or entries in the private
file. Environment variables take precedence. Start with the example:

```bash
sudo install -d -m 700 /opt/deploy/tester/config
sudo install -m 600 scripts/feishu.env.example /opt/deploy/tester/config/feishu.env
```

Fill in the real webhook and report URL in that private file. Never commit
credentials or the filled file. An unset webhook disables notification.
Notification failure preserves the report and the test runner's exit code.

## Daily schedule

The packaged Tester control service owns the daily schedule. Set
`TESTER_DAILY_RUN_ENABLED` to `true`, choose `TESTER_DAILY_RUN_TIME` and
`TESTER_DAILY_RUN_TIMEZONE` in the protected runtime configuration, then restart
Tester through `scripts/starter.sh`. The standard schedule is **03:00** in
**Asia/Shanghai**. The schedule enqueues the full run through Tester’s shared
queue and lock, so manual, project-triggered and daily runs cannot overlap.
Remove any old `/etc/cron.d/yeying-tester-full` entry after enabling this
schedule. In source mode, the full runner still checks out the requested Tester
main commit into its managed worktree. In artifact mode, it runs the published
Tester package directly. See [test-control.md](test-control.md) for the source
worktree and protected manual controls.

GPU 当前每日计划未启用（`TESTER_DAILY_RUN_ENABLED` 未设置，默认关闭）。需要启用时先确认全量依赖闭包和人工全量报告通过，再配置受保护 runtime、通过 starter 重启并检查服务日志；不要同时保留旧 cron。

Manual source-mode run with the latest Tester main:

```bash
bash scripts/test-tester-main.sh
```

退出码 `0` 可能表示套件通过或 preflight skip；必须检查 `last-attempt.txt` 和报告/JUnit。退出码 `1` 可表示报告已发布但用例失败。超时、陈旧/无效结果和零执行用例不会覆盖上次报告。检查报告时间戳，避免把历史结果误读为当前结果。

## Published files and links

Store published report pages under `TESTER_REPORT_DIR`. The Tester control service
indexes each published `summary.json` in its SQLite `tester_reports` catalog and
serves report pages and downloads through wallet-authenticated `/api/reports`
endpoints. Nginx serves the report-center page at `/report/` and rewrites nested
`/report/<path>` requests to the authenticated file API. Do not expose the
repository `reports/` directory, JUnit XML or the private directory directly.

```text
index.html                 Latest overview grouped by product
full.html                  Latest redacted Playwright report with Chinese test titles
full.en.html               Same run with English test titles
latest.json                Start/end times, counts and archived report paths
last-attempt.txt           Most recent attempt status
runs/<run>/index.html      Archived overview
runs/<run>/full.html       Archived report with Chinese test titles
runs/<run>/full.en.html    Archived report with English test titles
runs/<run>/logs/<project>.log      Sanitized, project-only case log
runs/<run>/logs/<project>.en.log   Same cases with English field labels
runs/<run>/errors/<project>.csv   Failed cases only; absent when none failed
runs/<run>/summary.json    Archived summary
```

The overview offers project-specific LOG downloads and a failed-case CSV when
applicable. Its log-language selector changes the LOG download links between
Chinese and English labels. Test names in these LOG files come from the JUnit
result of the run and are not translated. The two Playwright detail pages use
the same test results, IDs, durations, errors, and attachments; only test titles,
group paths, and report controls change language. Switching detail pages keeps
the current URL query and fragment. Single-project reports use the same layout
under `projects/<project>/runs/<run>/`.

Serve these downloads behind the same report access rule as their overview.
Older archived runs that predate this feature retain their original files and
may not have an English page or per-project downloads.

The publisher redacts configured sensitive values and credential shapes from
HTML and its embedded ZIP. The Playwright configuration omits the complete
environment object from report metadata. Public HTML titles are escaped.
The pipeline does not publish raw trace or screenshot attachment directories.
Failed or incomplete publication does not replace the previous overview.

Skipped cases remain skips. Their displayed reason categories are review hints,
not proof that the environment or product is correct; unknown reasons stay visible.

A completed run's Feishu card shows its start and finish time in Beijing time,
with one row per product and linked pass/fail/skip counts. Links open that run's
archived `full.html` using Playwright `p:<project>` and `s:<status>` filters.
They keep pointing to the same run after a later report is published.
Summary and JUnit counts must agree before a card is generated.

## Validate the report pipeline

These checks use synthetic fixtures without product tests or group messages:

```bash
python3 -m unittest discover -s scripts/tests -v
bash -n scripts/run-full-on-test-machine.sh
```
