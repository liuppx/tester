# 端到端测试用例目录

本目录是 Tester 控制台“用例脑图”的内容来源，每个产品一份 Markdown。文档描述产品应有的端到端行为和运行前置条件；`products/<project>/tests/**/*.spec.ts` 是 Playwright 自动化实现。逻辑用例与 Playwright test 数量不是一一对应关系。

## 产品目录

| 产品 | 用例文档 |
| --- | --- |
| Book | [book.md](book.md) |
| Chat | [chat.md](chat.md) |
| Knowledge | [knowledge.md](knowledge.md) |
| Marketplace | [marketplace.md](marketplace.md) |
| Node | [node.md](node.md) |
| Orchestrator | [orchestrator.md](orchestrator.md) |
| Project | [project.md](project.md) |
| Router | [router.md](router.md) |
| Social | [social.md](social.md) |
| Wallet | [wallet.md](wallet.md) |
| Warehouse | [warehouse.md](warehouse.md) |

不要在本 README 或产品文档手工维护“当前通过率”。实时 pass/fail/skip 来自某次报告，应与运行模式、Tester 版本、产品 commit 和报告链接一起查看。用例文档中的“已实现”表示存在自动化映射，不代表最近一次运行通过，也不代表当前 GPU 环境具备所需依赖。

## 脑图解析格式

控制服务从产品 Markdown 读取二级标题作为功能模块，读取以下格式的三级标题作为用例节点：

```markdown
## 一、钱包登录

### RT-API-001 有效钱包可以登录

- 优先级:P0
- 类型:API
- 状态:✅ 已实现 — products/router/tests/auth.spec.ts
- 前置条件:Node 与 Router API 可达，测试钱包身份有效。
- 步骤:
  1. 创建登录 challenge。
  2. 使用对应钱包签名并提交。
- 预期结果:签发的会话身份与钱包地址匹配。
```

解析器要求 case ID 匹配大写字母/数字/连字符并以三位数字结尾，例如 `RT-API-001`。标题可读；字段标签固定为“优先级、类型、状态、前置条件、步骤、预期结果”。步骤使用 Markdown 有序列表。模块前未归属的用例会被归为“未分类”。

支持的用例状态口径：

- `✅ 已实现 — <spec 路径>`：至少一个自动化 spec 覆盖该逻辑行为。
- `⬜ 待实现`：尚无自动化实现。
- `🐞 已实现但被真实产品缺陷阻塞`：保留原断言，使用 `test.fixme` 等方式标记，不能伪装通过。
- 环境条件跳过要写清楚具体条件。跳过不等于通过；某次运行的实际跳过原因以 JUnit/报告为准。

## 更新与验证

新增或调整用例时，按这个顺序维护：

1. 确认行为来自当前产品代码、API 文档或可复现的业务规则；确认它值得作为 E2E 验收。
2. 找到或新增产品模块、唯一 ID 和具体前置条件；不要把密钥、账户密码或真实用户身份写入文档。
3. 编写/调整对应 Playwright spec，并让失败信息能指出产品行为或环境前置条件。
4. 本地运行 `python3 -m unittest scripts.test_tester_control -v`，确认脑图解析器读取新模块和 case 字段。
5. 在目标运行模式下跑对应项目，保留准确的 pass/fail/skip 和 source commit；更新 case 状态及对应实现路径。
6. 新增服务或跨产品调用时，同步维护 `config/local-test-dependencies.json`、`docs/project-dependency-map.json` 和[依赖关系文档](../project-dependency-map.md)。

运行环境和服务当前状态见[测试环境现状](../test-machine-architecture.md)，产品依赖见[依赖关系](../project-dependency-map.md)。这两者不能由用例目录中的历史说明替代。
