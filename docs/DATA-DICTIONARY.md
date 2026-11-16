# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。


## 补扫派工

- task_id：`T-<期>-<版>-<页>`，只由业务键决定，重复生成不换身份。
- order_id：`O-<task_id>-<generation>`，同一任务每次接单或转派递增代次。
- credential：接单/转派时签发一次的执行凭据；回执必须携带，回读接口永不返回。
- expected_version：调用方读到的任务版本；与实际不一致返回 STALE_VERSION，不写入。
- order.status：IN_FLIGHT / COMPLETED / SUPERSEDED；SUPERSEDED 原单保留，close_reason=TIMEOUT_REASSIGNED。
- receipt.status：COMPLETED 计入完成；CONFLICT_PENDING 进入冲突待核，reason_code=LATE_RECEIPT，counts_as_completed=false。
- active_valid / overdue：回读时按当前时间判断在途工单是否仍有效、是否已超时待转派。
- request_id：可选幂等键；相同操作与载荷重试复用首次响应，载荷不同返回 IDEMPOTENCY_MISMATCH。
- 容量：站点 capacity 为同时可持有的在途工单上限；占用满时接单/转派返回 CAPACITY_FULL 且不改原单。
