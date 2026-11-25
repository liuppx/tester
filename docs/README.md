# Tester 文档入口

本文档目录按维护任务组织。运行状态以 [测试环境现状](test-machine-architecture.md) 为准；产品用例目录记录覆盖范围，`products/<project>/tests/` 才是自动化实现。

| 要做什么 | 阅读文档 |
| --- | --- |
| 了解当前 Tester、GPU 环境和依赖是否就绪 | [测试环境现状与架构](test-machine-architecture.md) |
| 打包、部署、升级、验证或回滚 Tester | [部署手册](deployment.md) |
| 使用社区 main 源码启动产品并执行 E2E | [本地源码测试模式](local-source-mode.md) |
| 理解钱包授权、控制页面、队列和源码 watcher | [控制服务与授权](test-control.md) |
| 查看报告、计划任务和失败的判读规则 | [报告与调度](test-machine-report.md) |
| 了解产品、中间件和跨产品依赖 | [依赖关系](project-dependency-map.md) |
| 查看产品用例范围与维护约定 | [测试用例目录](test-cases/README.md) |
| 了解 Tester Web 产品目标与已确认交互 | [Web 产品目标](web-product-brief.md) |

## 维护规则

- 主机配置、运行状态、端口、服务版本等易变信息只维护在 `test-machine-architecture.md`，更新时记录核对日期和验证命令。
- 可重复的部署或测试步骤维护在对应操作手册；环境快照不要复制到多个文档。
- 依赖图表达逻辑依赖；当前是否运行以架构手册的现场检查为准。
- 文档不得包含公网 IP、凭据、私钥、Webhook、用户邮箱或未经脱敏的服务日志。
- 代码、配置或服务关系改变后，运行对应测试并在同一变更中更新文档。
