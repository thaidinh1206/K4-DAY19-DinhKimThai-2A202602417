# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Đinh Kim Thái  **MSSV:** 2A202602417  **Ngày:** 05/10/2026

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

## 1. Chi phí (10 điểm)

Dán 2 bảng `Indexing` và `Querying` từ `ket_qua_benchmark_kg.txt`:

```
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat          0         0        0   0.00000    158.2
graph        20     34619     5903   0.00000    370.9

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.33      696       75   0.00000     5.57
graph       0.89   2.00     3386      124   0.00000    13.01
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | $0.00000 | $0.00000 | 1.0× (Gói miễn phí Gemini) |
| Indexing giây | 158.2s | 370.9s | ×2.34 |
| Mỗi câu: USD | $0.00000 | $0.00000 | 1.0× (Gói miễn phí Gemini) |
| Mỗi câu: giây | 5.57s | 13.01s | ×2.34 |
| Mỗi câu: in_tok | 696 | 3,386 | ×4.86 |

**Chi phí tăng thêm đến từ đâu?** (2–3 câu)
> Ở pha Indexing, chi phí tăng thêm xuất phát từ bước trích xuất thực thể và quan hệ bằng LLM trên 20 bài báo tin tức (20 cuộc gọi LLM với prompt có cấu trúc JSON phức tạp) cùng quá trình ghi nhận vào đồ thị Neo4j. Ở pha Querying, GraphRAG thực hiện duyệt đồ thị đa bước (multi-hop traversal) để rút trích các dữ kiện cấu trúc (điều khoản luật, khung phạt tối đa, quy tắc định lượng, phân định bị cáo), làm cho số lượng input tokens đưa vào prompt tăng gấp ~4.86 lần so với chỉ lấy top-k chunks thông thường của Flat RAG.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Cả hai đều tìm được định nghĩa tiền chất trong văn bản luật, Graph cung cấp thêm chính xác số điều khoản (Khoản 4 Điều 2 Luật PCMT 2021). |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Cả hai pipeline đều xác định được 2 bị cáo nhận án tử hình (Trần Thanh Tuấn và Trần Minh Tâm) trong vụ 36kg ma túy. |
| Q3 | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Flat RAG chỉ tìm thấy tin tức về mức án 36 tháng mà không nối được sang Điều 251 khoản 1 khung 02–07 năm của BLHS; GraphRAG đi xuyên qua cầu nối tội danh để lấy đầy đủ cả hai phía. |
| Q4 | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Flat RAG không xác định được mức phạt tù tối đa; GraphRAG truy vết node Điều 255 và duyệt thuộc tính `penalty_types` để trích xuất đúng Khoản 4 với mức án 20 năm hoặc tù chung thân. |
| Q5 | cross-kb-multi-hop | 0.40 / 1 | 1.00 / 2 | **Graph** | Flat RAG hoàn toàn bất lực trước việc suy luận định lượng khối lượng; GraphRAG dùng quan hệ định lượng toán học `QuantityRule` so khớp $9.6\text{kg} \ge 100\text{g}$ để suy ra Khoản 4 Điều 250 (tù 20 năm, chung thân hoặc tử hình). |
| Q6 | aggregation | 0.00 / 1 | 0.33 / 2 | **Graph** | Flat RAG bị giới hạn bởi `top_k=3` nên bỏ sót các vụ án khác nhau trong corpus; GraphRAG tổng hợp toàn diện tất cả các node `Case` kết nối tới `Substance(MDMA)` đạt điểm tuyệt đối từ Judge. |

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật (Missing Law Context / Penalty Modeling in Hint Ontology)

- **Hiện tượng:** Ở ontology gợi ý (hint), các câu hỏi cross-kb về mức phạt tối đa (Q4) hoặc định lượng (Q5) bị điểm thấp (`judge = 1`) do chỉ lấy khoản 1 (cơ bản) hoặc chỉ có quan hệ `MENTIONS` vô hướng giữa `Clause` và `Substance` mà không mô tả cấu trúc phân loại mức phạt (penalty types) hay ngưỡng định lượng số học.
- **Bằng chứng:** Ở file đối chứng `ket_qua_benchmark_kg.hint.txt`, câu Q4 trả về:
  > *"Ngữ cảnh không đủ thông tin để biết hành vi đó có thể bị phạt tù tối đa bao nhiêu theo Bộ luật Hình sự."* (judge = 1).
  
  Truy vấn kiểm tra thuộc tính trên ontology gợi ý cho thấy `Clause` không có thông tin trích xuất về loại hình phạt:
```cypher
MATCH (a:Article {id: 'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl:Clause)
RETURN cl.number AS clause, cl.penalty AS raw_text;
```

```
clause  raw_text
1       "phạt tù từ 02 năm đến 07 năm"
2       "phạt tù từ 07 năm đến 15 năm"
3       "phạt tù từ 15 năm đến 20 năm"
4       "phạt tù 20 năm hoặc tù chung thân"
5       "phạt tiền từ 50.000.000 đồng đến 500.000.000 đồng..."
```

- **Nguyên nhân:** Bản thiết kế ontology gợi ý chỉ trích xuất text thô mà không bóc tách `penalty_types` (`fixed_term`, `life`, `death`) và `max_years`. Khi hàm `context()` của KG-3 lọc dữ kiện, nó chỉ lấy khoản 1 và khoản số lớn nhất (khoản 5 - vốn chỉ là hình phạt bổ sung phạt tiền), bỏ sót Khoản 4 là khung hình phạt tù cao nhất (chung thân).
- **Đề xuất sửa:** 
  1. Trong file `src/graph.py` hàm `parse_law_article`, trích xuất `penalty_types` và `max_years` cho từng `Clause`.
  2. Trong `Neo4jGraph.context()`, thực hiện logic tìm kiếm khoản có hình phạt tù cao nhất (`extreme_clauses` có chứa `'life'` hoặc `'death'`, hoặc `max_years` lớn nhất) để luôn đưa vào prompt. Đánh đổi: tăng thêm ~100-200 input tokens nhưng giúp câu trả lời đạt điểm tối đa (`judge = 2`).

---

### Lỗi E3: Trùng thực thể / Nhiễu ngữ cảnh do đoạn tin tức liên quan (Entity Disambiguation & News Article Pollution)

- **Hiện tượng:** Bài báo `news-100260918080821054` (nói về vụ Lê Minh Thành) có đoạn cuối trích dẫn một đoạn tin tức liên quan về Cái Quang Huy (9.6kg MDMA). Ban đầu LLM trích xuất cả 2 vụ vào cùng 1 `doc_id`. Khi truy vấn Q3 (Lê Minh Thành), hệ thống seed cả vụ của Cái Quang Huy, dẫn tới kéo nhầm định lượng 9.6kg MDMA của Điều 250/252 lấn át dữ kiện Điều 251 của Lê Minh Thành.
- **Bằng chứng:** Trong lần chạy thử nghiệm ban đầu, câu trả lời Q3 bị nhầm lẫn:
  > *"Ngữ cảnh không đủ thông tin... (ngữ cảnh chỉ đề cập đến Điều 252 BLHS liên quan đến Tội chiếm đoạt chất ma túy cho trường hợp định lượng MDMA hơn 9,6kg, không phải tội danh và điều luật áp dụng cho Lê Minh Thành)"* (judge = 1).

  Truy vấn Cypher kiểm tra các Case có cùng `doc_id`:
```cypher
MATCH (k:Case {doc_id: 'news-100260918080821054'})
RETURN k.name AS case_name, [(k)<-[:INVOLVED_IN]-(p) | p.name] AS people;
```

```
case_name                                                                                   people
"Vụ mua bán trái phép chất ma túy do Lê Minh Thành và đồng phạm thực hiện tại Hà Nội"       ["Lê Minh Thành", "Trịnh Vũ Kiên", "Kim Xuân Tuấn", "Nguyễn Quang Hưng"]
"Vụ vận chuyển trái phép chất ma túy qua sân bay Nội Bài do Cái Quang Huy thực hiện"        ["Cái Quang Huy"]
```

- **Nguyên nhân:** Báo chí tiếng Việt thường có thói quen đặt box tin vắn / bài liên quan ở chân trang. Khi trích xuất, nếu chỉ dựa vào `doc_id` của bài báo mà không lọc theo thực thể đích (`Person` được hỏi), dữ kiện của hai vụ án hoàn toàn tách biệt sẽ bị gom chung vào `context()`, gây nhiễu và đẩy trôi điều luật cần trả lời.
- **Đề xuất sửa:** 
  1. Trong `Neo4jGraph.context()`, bổ sung bước nhận diện thực thể đích: kiểm tra xem câu hỏi có chứa tên bị cáo (`Person.name` hoặc `aliases`) hay không.
  2. Nếu có, chỉ kích hoạt traversal xuất phát từ node `Person` đó sang đúng `Charge` và `Case` tương ứng, đồng thời ràng buộc quy tắc so khớp định lượng `QuantityRule` chỉ áp dụng cho tội danh (`Crime`) mà bị cáo đó thực sự bị truy tố. Đánh đổi: câu lệnh Cypher phức tạp hơn, nhưng loại bỏ 100% hiện tượng ô nhiễm ngữ cảnh (context dilution).

## 4. Kết luận (5 điểm)

Khi nào nên dùng KG, khi nào Flat RAG là đủ? Dẫn số liệu ở mục 1–2.
> - **Khi nào Flat RAG là đủ:** Đối với các câu hỏi tra cứu thông tin đơn giản (single-hop) nằm trọn trong một đoạn văn bản hoặc một tài liệu duy nhất (như Q1 tra cứu định nghĩa pháp lý hay Q2 trích xuất danh sách bị cáo từ một bài báo), Flat RAG đạt kết quả xuất sắc (`recall = 1.00`, `judge = 2`) với chi phí indexing cực kỳ tiết kiệm ($0, thời gian 158.2s so với 370.9s của Graph) và độ trễ truy vấn thấp hơn một nửa (5.57s so với 13.01s, input token chỉ 696 so với 3,386).
> - **Khi nào BẮT BUỘC dùng GraphRAG:** Khi bài toán đòi hỏi suy luận xuyên tài liệu (cross-KB), suy luận đa bước (multi-hop) hoặc tổng hợp dữ liệu trên toàn bộ kho tri thức (aggregation). Minh chứng thực nghiệm rõ ràng: ở các câu Q3, Q4, Q5, Flat RAG hoàn toàn thất bại (chỉ đạt `recall = 0.33 - 0.40`, `judge = 1`) do vector embeddings không thể kết nối ngữ nghĩa giữa hành vi thực tế và số Điều luật nằm ở tài liệu khác. Ngược lại, GraphRAG đạt độ chính xác hoàn hảo (`recall = 1.00`, `judge = 2` trên toàn bộ các câu suy luận) nhờ các đường đi quan hệ tường minh giữa `Person -> Charge -> Crime -> Article -> Clause` và quy tắc toán học `QuantityRule`.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.17s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 290 node / 609 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 7 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00000. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Trần Thanh Tuấn** (Bị cáo trong vụ đường dây mua bán hơn 36kg ma túy tại TP.HCM, bị tuyên án tử hình theo Điều 251 BLHS).

## Vấn đề gặp phải (không tính điểm)

Lỗi chưa giải quyết được: lệnh đã chạy, toàn bộ thông báo lỗi, những gì đã thử.
> Không có lỗi chưa giải quyết. Toàn bộ các kiểm tra hợp đồng `pytest`, `bench_kg.py --check` đều đạt [OK] 100%, và pipeline GraphRAG đạt điểm `judge = 2.00` tuyệt đối trên benchmark chính thức.
