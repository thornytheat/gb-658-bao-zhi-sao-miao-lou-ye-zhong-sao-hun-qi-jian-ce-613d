# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含七个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫判定：页面 /rescans，接口 /api/rescans/evaluate
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile
- M7 补扫派工：页面 /dispatch，接口 /api/dispatch/{generate,claim,reassign,receipt,summary,tasks,orders,receipts,sites,verify}

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

### 补扫派工状态机

- 生成：从真实期版页序（同 M2 规范、同 M3 缺页规则）导出缺页任务，任务标识只由 `期|版|页` 决定，重复生成不新增任务、不重置进度。
- 接单：按站点容量排队，满员返回 `CAPACITY_FULL`；携带过期 `expected_version` 返回 `STALE_VERSION`，不落任何写入。
- 转派：原单未超时返回 `NOT_TIMED_OUT`；超时后原单关闭为 `SUPERSEDED` 并保留凭据与执行证据，换发新凭据给新站点；无空闲站点时保持原单在途。
- 回执：当前单凭据有效则完成；已转派原单的迟到回执记 `CONFLICT_PENDING / LATE_RECEIPT`，不计完成、不抵消新单。
- 写入：每次状态变更在单个 `BEGIN IMMEDIATE` 事务内完成，失败整体回滚；相同 `request_id` 重试复用首次结果，不重复占用名额或换发凭据。
- 回读：`/api/dispatch/tasks` 与 `/api/dispatch/summary` 给出缺页任务、有效在途（`active_valid`/`overdue`）与站点容量占用；`/api/dispatch/verify` 核对任务守恒、单任务唯一在途工单与容量上限；凭据只在签发响应中出现一次。

不改 M4 重扫采信与 M5 混期归因：本模块只消费 M2/M3 的期版页序结论，不重算清晰度择优或页眉证据。

