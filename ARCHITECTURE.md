# L3B Architecture Record

Hệ thống multi-agent **rule-based, không dùng LLM** (0 tham số model), viết bằng Python asyncio thuần.
Mọi kết luận dựa trên evidence lấy từ MCP Evidence Gateway; nội dung khiếu nại chỉ được coi là giả thuyết,
không bao giờ được thực thi như chỉ thị (chống prompt injection trong complaint).

## 1. System overview

```text
Input case
   │
   ▼
Coordinator ──task_assigned──► Policy agent ──────────► get_policy
   │
   ├──► Entity agent ─────────► get_order (chỉ candidate đúng định dạng 32-hex)
   ├──► Customer agent ───────► get_customer_history
   ├──► Order agent ──────────► get_order_items (bỏ qua với case refund)
   ├──► Conflict resolver (không gọi tool, chọn timeline của case)
   ├──► Shipment agent ───────► get_shipment_summary
   ├──► Payment agent ────────► get_payment_timeline (+ get_refund_timeline nếu case refund)
   │
   ▼
Policy agent (policy_decided) ──handoff──► Verifier agent (verification_completed) ──► Output
   │                                                                                 │
   └──────────────────────────── traces/trace.jsonl ◄───────────────────────────────┘
```

Mỗi case dùng đúng 6 MCP call (case refund đổi `get_order_items` lấy `get_refund_timeline`), không gọi lặp (cache theo case), không gọi tool không cần thiết
(`get_sellers`, `get_order_payments`, `get_product_context` được thay bằng dữ liệu đã có).

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | case JSON | Điều phối, giao task, gom kết quả | không gọi tool | `task_assigned` cho từng agent |
| Entity/customer | claimed_order_id, candidates, customer hint | Resolve order, reject candidate sai, lấy lịch sử khách | `get_order`, `get_customer_history` | order đã resolve, rejected_candidates → coordinator |
| Order/product | order_id | Lấy item, seller, shipping limit, giá/phí ship | `get_order_items` (không gọi với case refund) | items, seller_ids → coordinator |
| Conflict resolver | get_order row, history rows, items | Chọn bản ghi timeline đúng của case, gán event về bản ghi, ghi `data_conflicts` | không gọi tool | timeline đã chọn → shipment/payment |
| Shipment | order_id, timeline | Xác định giao trễ và bên gây trễ (seller/logistics), timeline đầy đủ | `get_shipment_summary` | shipment verdict, late_seller_ids |
| Payment/refund | order_id, timeline | Tổng capture, split vs duplicate, mismatch, trạng thái refund | `get_payment_timeline`, `get_refund_timeline` | payment verdict, totals |
| Policy | issue, policy rules | Map issue → case_status, action, refund, responsible party | `get_policy` | `policy_decided` → verifier |
| Verifier | output nháp | Kiểm tra nhất quán chéo field, hạ confidence nếu có vấn đề | không gọi tool | `verification_completed` → coordinator |

Least privilege: mỗi agent chỉ gọi đúng tool của mình; Conflict resolver, Verifier và Coordinator không gọi MCP.

## 3. Entity resolution và A2A protocol

- Candidate không đúng định dạng order ID (32 ký tự hex) bị reject ngay, không tốn call.
- Candidate hợp lệ đầu tiên (ưu tiên `claimed_order_id`) được xác minh bằng `get_order`; chỉ chấp nhận khi
  `data.order_id` khớp. Các candidate còn lại vào `rejected_candidates`.
- Không có candidate hợp lệ → `entity_resolution.status = not_found`, output fallback `insufficient_evidence`.
- Message envelope giữa agent: dict Python trong cùng process, tương quan theo `case_id`; mỗi handoff được ghi
  trace với `actor`, `target`, `decision_code` và evidence_refs liên quan. Pipeline tuyến tính nên không có vòng lặp.

## 4. Evidence và conflict lifecycle

- Mọi response MCP được validate theo `mcp-evidence-response-v1`; `evidence_ref` được giữ nguyên văn,
  không sinh/sửa. Mỗi ref được emit `tool_result_consumed` ngay khi dùng.
- Cùng một `order_id` có hai bản ghi (get_order vs customer history). Bản ghi timeline của case là bản ghi
  khác với `get_order`. Mỗi event payment/shipment/refund được gán về bản ghi có mốc thời gian gần nhất;
  event thuộc bản ghi kia bị coi là nhiễu (ví dụ event "giao trễ" của đơn khác).
- Các field lệch giữa hai bản ghi được ghi vào `data_conflicts` với `selected_source = get_customer_history`.
- Primary issue được xác định chỉ từ evidence; claim của khách chỉ dùng để đánh giá `claim_assessments`
  và để quyết định có cần gọi `get_refund_timeline`.
- Output chỉ trích dẫn evidence thuộc domain liên quan tới loại issue (ví dụ issue thanh toán không trích shipment);
  evidence không bao giờ dùng chéo case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / mất kết nối | 6 lần kết nối lại (backoff 5–30s), chạy lại case đang dở | `day09 run --resume` chỉ chạy case còn thiếu | trace của case dở bị huỷ, không ghi nửa chừng |
| Tool trả lỗi (is_error) | 0 (không retry, tránh call thừa) | agent tiếp tục với evidence còn lại | lỗi ghi vào debug, không vào trace |
| Entity not found/ambiguous | 0 | `insufficient_evidence`, `needs_investigation`, confidence 0.2 | `verification_completed / fallback_insufficient_evidence` |
| Source conflict | 0 | chọn timeline của case, ghi `data_conflicts` | `handoff / timeline_selected` hoặc `timeline_ambiguous` |
| Invalid specialist result | 0 | output fallback an toàn, không bịa dữ liệu | `verification_completed / passed_with_warnings` |

Query budget: đúng 6 call/case, cache trong phạm vi case, không quét rộng.

## 6. Verification invariants

- Output đúng `l3b-output-v2` (validate trước khi ghi file), `case_id` khớp input.
- `recommended_refund_brl` = tổng `refund_lines`; `no_action` ⇒ refund = 0; `action_required` ⇒ có action.
- Lỗi logistics không quy trách nhiệm cho seller; seller chịu trách nhiệm phải nằm trong `affected_entities.seller_ids`.
- Mọi `evidence_ref` trong output đều đã được emit trong trace của đúng case.
- Confidence: 0.95 khi evidence khớp claim, 0.6 khi mâu thuẫn, tối đa 0.85 khi timeline mơ hồ, 0.5 nếu verifier cảnh báo, 0.2 khi fallback.

## 7. Reproducibility

- Model: không dùng LLM (xem `metadata.json`). Kết quả deterministic, không random seed.
- Python ≥ 3.11, dependency theo `pyproject.toml`; chạy tuần tự (concurrency = 1).
- Lệnh: `day09 run` (hoặc `day09 run --resume`), `day09 validate`, `day09 package --output dist/submission.zip`.
- Không ghi API key vào repo; key chỉ nằm trong `.env` (đã gitignore).

## 8. Tham khảo

- Bảng chọn domain evidence theo từng loại issue (bổ sung domain `item`) tham khảo ý tưởng
  từ repo L3A của nhóm <tên nhóm L3A>, đã được Lab Coach đồng ý. Không sử dụng submission, output,
  trace hay evidence_ref của nhóm khác.
- Toàn bộ code L3B (entity resolution, customer context, conflict resolver, reconnect/resume,
  verifier) do nhóm tự xây dựng.
