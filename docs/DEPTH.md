# 深度登记 扫描期版质检台

- 规模：中型
- 核心规则：期号、版次、页码构成业务键，图像相似只生成候选，页眉证据决定混期。
- 使用者与工作流：质检员导入扫描清单，核对期版与页序，裁决重扫和混期，归档证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 清单采信 | 导入扫描清单并定位坏行 | POST /api/scans/import | `app/modules/m1_intake/parser.py` `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py` |
| M2 期版契约 | 规范期号、版次和页码 | POST /api/issues/normalize | `app/modules/m2_issue/rules.py` `app/modules/m2_issue/catalog.py` | `tests/test_m2_issue.py` |
| M3 页码序列 | 查缺页、重页与越界页 | POST /api/pages/check | `app/modules/m3_pages/sequence.py` `app/modules/m3_pages/reconcile.py` | `tests/test_m3_pages.py` |
| M4 重扫判定 | 比较扫描候选并保留清晰版本 | POST /api/rescans/evaluate | `app/modules/m4_rescans/similarity.py` `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py` |
| M5 混期归因 | 用页眉和页码证据裁决混期 | POST /api/mixed/evaluate | `app/modules/m5_mixed/evidence.py` `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py` |
| M6 批次对账 | 检查四账并导出证据 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 补扫派工 | 生成缺页补扫、按站点容量接单、超时转派、回执与回读 | POST /api/dispatch/generate · /claim · /reassign · /receipt；GET /api/dispatch/tasks · /summary · /orders · /receipts · /verify | `app/modules/m7_dispatch/service.py` `app/modules/m7_dispatch/repo.py` | `tests/test_m7_dispatch.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 首列带 BOM | 期号字段取不到 | 读取表头时移除 BOM | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_strip_bom_header` |
| TRAP-05 | 行列数不齐 | 错位行进入页序 | 隔离并保留物理行号 | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_column_count_goes_quarantine` |
| TRAP-07 | 页眉为空 | 空串被当作一致 | 返回未知并转人工复核 | `app/modules/m5_mixed/evidence.py` | `tests/test_m5_mixed.py::test_missing_header_is_unknown` |
| TRAP-08 | 前导零页码 | 转整数后无法复核原编号 | 规范为三位字符串 | `app/modules/m2_issue/rules.py` | `tests/test_m2_issue.py::test_page_zero_pads_without_numeric_roundtrip` |
| TRAP-09 | 大摘要或相似哈希 | 数字往返或只凭相似度误判 | 摘要保形，相似仅生成候选 | `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py::test_hash_candidate_without_header_needs_review` |
| TRAP-14 | 同版多扫描值冲突 | 后写覆盖清晰版本 | 版本、清晰度、时间稳定择优 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_duplicate_page_detected` |
| TRAP-16 | 清单尾行截断 | 半行被当成扫描件 | 完整尾行检查并隔离 | `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py::test_truncated_tail_detected` |
| TRAP-19 | 候选顺序改变 | 先到者决定保留项 | 显式排序键保持顺序无关 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_selection_order_independent` |
| TRAP-22 | 同内容多别名 | 合并顺序改变代表扫描 | 清晰度后以时间和文件名择一 | `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py::test_selection_is_deterministic_on_tie` |
| TRAP-26 | 陈旧重扫晚提交 | 旧版本覆盖新版 | 版本优先于输入顺序 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_higher_version_wins_not_later_input` |
| TRAP-27 | 站点失联后死等整期 | 任务悬空或直接撤单丢执行证据 | 超时转派：原单关闭为 SUPERSEDED 保留，换发新凭据 | `app/modules/m7_dispatch/repo.py` | `tests/test_m7_dispatch.py::test_reassign_keeps_original_order_and_issues_new_credential` |
| TRAP-28 | 旧站迟到回执 | 迟到回执覆盖或抵消新单 | 记 CONFLICT_PENDING，不完成、不影响在途新单 | `app/modules/m7_dispatch/repo.py` | `tests/test_m7_dispatch.py::test_late_receipt_enters_conflict_and_does_not_affect_new_order` |
| TRAP-29 | 并发接单与过期版本 | 后到请求覆盖已接单状态 | 版本校验 + BEGIN IMMEDIATE，过期返回 STALE_VERSION | `app/modules/m7_dispatch/repo.py` | `tests/test_m7_dispatch.py::test_claim_rejects_stale_version` |
| TRAP-30 | 派工写入失败后重试 | 名额重复占用或凭据换发 | 事务回滚；request_id 幂等复用首次结果 | `app/modules/m7_dispatch/repo.py` | `tests/test_m7_dispatch.py::test_write_failure_rolls_back_and_retry_does_not_double_book` |

## 对账规则

- 应有页数 = 唯一采纳页 + 缺页数。
- 扫描件数 = 唯一采纳 + 被替代重扫 + 混期 + 隔离。
- 同一输入与规则版本重跑，运行标识和结果行数不变。
- 补扫任务：缺页任务数 = 期望页序 − 已采纳唯一页；有效在途 = 未超时在途工单；容量占用 = 各站点在途工单数。

## 已知不覆盖

- 不读取图像二进制，感知哈希与 OCR 页眉由上游扫描软件提供。

