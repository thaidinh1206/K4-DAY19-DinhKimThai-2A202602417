"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not name or not known:
        return None
    target = normalize(name)
    if not target:
        return None
    normalized_map = {normalize(k): k for k in known}
    if target in normalized_map:
        return normalized_map[target]
    matches = difflib.get_close_matches(target, list(normalized_map.keys()), n=1, cutoff=0.8)
    if matches:
        return normalized_map[matches[0]]
    return None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

def parse_quantity_amount(raw: str) -> tuple[float | None, float | None]:
    """Parse raw text like 'hơn 9,6kg', 'khoảng 406g' into (lower_g, upper_g)."""
    if not raw:
        return None, None
    s = raw.lower().replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|kilôgam|gam|g)\b", s)
    if not m:
        m_num = re.search(r"(\d+(?:\.\d+)?)", s)
        if m_num:
            return float(m_num.group(1)), None
        return None, None
    val = float(m.group(1))
    unit = m.group(2)
    val_g = val * 1000.0 if "k" in unit else val
    if "hơn" in raw.lower() or "trở lên" in raw.lower() or "từ" in raw.lower():
        return val_g, None
    elif "dưới" in raw.lower() or "gần" in raw.lower():
        return None, val_g
    return val_g, val_g

# ----------------------------------------------------------------------------------------------
# Ontology Extraction Helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' with penalty types, quantity rules, and concepts."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)

        lowered = text.lower()
        penalty_types = []
        if "tử hình" in lowered:
            penalty_types.append("death")
        if "tù chung thân" in lowered or "chung thân" in lowered:
            penalty_types.append("life")
        if "phạt tù" in lowered or "tù từ" in lowered:
            penalty_types.append("fixed_term")

        m_max = re.search(r"đến\s*(\d+)\s*năm", lowered)
        max_years = int(m_max.group(1)) if m_max else None

        clause_substances = find_substances(text)
        quantity_rules = []
        for s in clause_substances:
            if "100 gam trở lên" in text or "100g trở lên" in text:
                quantity_rules.append({
                    "id": f"qr:{article_id}:cl{start.group(1)}:{s}:100g",
                    "substance": s, "lower": 100.0, "upper": None, "unit": "g", "point": "4"
                })
            elif "30 gam đến dưới 100 gam" in text:
                quantity_rules.append({
                    "id": f"qr:{article_id}:cl{start.group(1)}:{s}:30-100g",
                    "substance": s, "lower": 30.0, "upper": 100.0, "unit": "g", "point": "3"
                })
            elif "05 gam đến dưới 30 gam" in text or "5 gam đến dưới 30 gam" in text:
                quantity_rules.append({
                    "id": f"qr:{article_id}:cl{start.group(1)}:{s}:5-30g",
                    "substance": s, "lower": 5.0, "upper": 30.0, "unit": "g", "point": "2"
                })

        concepts = []
        if "tiền chất là" in lowered:
            m_def = re.search(r"[Tt]iền chất là (.+?)(?:\.\s*|$)", text)
            def_text = m_def.group(0).strip() if m_def else text
            concepts.append({
                "id": f"concept:{article_id}:tien_chat",
                "name": "tiền chất",
                "definition_text": def_text,
            })

        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "penalty_types": penalty_types,
            "max_years": max_years,
            "text": text,
            "substances": clause_substances,
            "quantity_rules": quantity_rules,
            "concepts": concepts,
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [
            ("Article", "id"), ("Clause", "id"), ("QuantityRule", "id"), ("LegalConcept", "id"),
            ("Crime", "name"), ("Case", "name"), ("Substance", "name"), ("Person", "name"),
            ("Charge", "id"), ("Quantity", "id"), ("Location", "name")
        ]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) 
              SET a.title = $title, a.law = $law, a.doc_id = $doc_id, a.name = $id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime})
                MERGE (a)-[:DEFINES]->(c)
                MERGE (a)-[:DEFINES_CRIME {doc_id: $doc_id}]->(c)
            )
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, 
                  cl.doc_id = $doc_id, cl.penalty_types = clause.penalty_types, cl.max_years = clause.max_years,
                  cl.name = clause.id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | 
                MERGE (sub:Substance {name: s}) 
                MERGE (cl)-[:MENTIONS]->(sub)
            )
            FOREACH (concept IN clause.concepts |
                MERGE (lc:LegalConcept {id: concept.id})
                  SET lc.name = concept.name, lc.definition_text = concept.definition_text, lc.doc_id = $doc_id
                MERGE (cl)-[:DEFINES_TERM {doc_id: $doc_id}]->(lc)
            )
            FOREACH (rule IN clause.quantity_rules |
                MERGE (qr:QuantityRule {id: rule.id})
                  SET qr.lower = rule.lower, qr.upper = rule.upper, qr.unit = rule.unit,
                      qr.point = rule.point, qr.matching_supported = true, qr.doc_id = $doc_id
                MERGE (cl)-[:HAS_QUANTITY_RULE]->(qr)
                MERGE (srule:Substance {name: rule.substance})
                MERGE (qr)-[:APPLIES_TO]->(srule)
            )
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        case_name = case.get("name") or doc.metadata.get("title", doc.id)
        processed_substances = []
        for i, s in enumerate(case.get("substances", [])):
            if not s.get("name"):
                continue
            amt = s.get("amount", "")
            lower_g, upper_g = parse_quantity_amount(amt)
            processed_substances.append({
                "name": s["name"],
                "amount": amt,
                "lower_g": lower_g,
                "upper_g": upper_g,
                "qty_id": f"{doc.id}:case:qty:{i}:{s['name']}",
            })

        processed_people = []
        for i, p in enumerate(case.get("people", [])):
            if not p.get("name"):
                continue
            sentence_raw = p.get("sentence", "")
            sentence_type = ""
            sentence_months = None
            if "tử hình" in sentence_raw.lower():
                sentence_type = "death"
            elif "chung thân" in sentence_raw.lower():
                sentence_type = "life"
            elif re.search(r"\d+\s*(?:năm|tháng)", sentence_raw.lower()):
                sentence_type = "fixed_term"
                m_yrs = re.search(r"(\d+)\s*năm", sentence_raw.lower())
                m_mos = re.search(r"(\d+)\s*tháng", sentence_raw.lower())
                total_m = (int(m_yrs.group(1)) * 12 if m_yrs else 0) + (int(m_mos.group(1)) if m_mos else 0)
                if total_m > 0:
                    sentence_months = total_m

            processed_people.append({
                "name": p["name"],
                "aliases": p.get("aliases") or [],
                "role": p.get("role", "bị cáo"),
                "charge": p.get("charge", ""),
                "sentence": sentence_raw,
                "sentence_type": sentence_type,
                "sentence_months": sentence_months,
                "charge_id": f"{doc.id}:charge:{i}:{p['name']}",
            })

        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN {doc_id: $doc_id}]->(l))
            FOREACH (crime IN $charges | 
                MERGE (c:Crime {name: crime}) 
                MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | 
                MERGE (sub:Substance {name: s.name}) 
                MERGE (k)-[r:INVOLVES]->(sub) SET r.amount = s.amount
                MERGE (qty:Quantity {id: s.qty_id})
                  SET qty.raw_amount = s.amount, qty.lower = s.lower_g, qty.upper = s.upper_g, qty.unit = 'g', qty.doc_id = $doc_id
                MERGE (k)-[:HAS_QUANTITY]->(qty)
                MERGE (qty)-[:OF_SUBSTANCE]->(sub)
            )
            FOREACH (p IN $people | 
                MERGE (person:Person {name: p.name})
                  SET person.aliases = coalesce(p.aliases, []), person.doc_id = $doc_id
                MERGE (person)-[r:INVOLVED_IN]->(k) 
                  SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence
                MERGE (person)-[:PARTICIPATED_IN {role: p.role, doc_id: $doc_id}]->(k)
                FOREACH (chg IN CASE WHEN p.charge = '' THEN [] ELSE [p.charge] END |
                    MERGE (charge_node:Charge {id: p.charge_id})
                      SET charge_node.sentence_type = p.sentence_type, charge_node.sentence_months = p.sentence_months,
                          charge_node.sentence_raw = p.sentence, charge_node.doc_id = $doc_id, charge_node.stage = 'first_instance'
                    MERGE (person)-[:HAS_CHARGE]->(charge_node)
                    MERGE (charge_node)-[:IN_CASE]->(k)
                    MERGE (cnode:Crime {name: chg})
                    MERGE (charge_node)-[:OF_CRIME]->(cnode)
                    FOREACH (s IN $substances |
                        MERGE (qlink:Quantity {id: s.qty_id})
                        MERGE (charge_node)-[:ATTRIBUTED_QUANTITY {doc_id: $doc_id}]->(qlink)
                    )
                )
            )
            """,
            name=case_name,
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=processed_people,
            substances=processed_substances,
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + multi-hop traversal with quantitative rules & maximum penalties."""
        facts: list[str] = []
        seen: set[str] = set()

        def add_fact(f: str) -> None:
            f = f.strip()
            if f and f not in seen:
                seen.add(f)
                facts.append(f)

        # 1. LegalConcept definitions (Q1: tiền chất là gì?)
        concepts = self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)-[:DEFINES_TERM]->(lc:LegalConcept)
            WHERE toLower($q) CONTAINS toLower(lc.name)
            RETURN a.id AS article_id, cl.number AS clause_num, lc.name AS name, lc.definition_text AS def_text
            """,
            q=question,
        )
        for row in concepts:
            add_fact(f"[{row['article_id']} khoản {row['clause_num']}] Định nghĩa '{row['name']}': {row['def_text']}")

        # 2. Identify target persons mentioned in question
        target_persons = self.run(
            """
            MATCH (p:Person)
            WHERE (p.name IS :: STRING AND size(p.name) >= 3 AND toLower($q) CONTAINS toLower(p.name))
               OR any(a IN coalesce(p.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN DISTINCT p.name AS name, elementId(p) AS id
            """,
            q=question,
        )
        target_p_ids = [p["id"] for p in target_persons]

        # 3. Target cases: from target persons, or from doc_ids
        if target_p_ids:
            case_rows = self.run(
                """
                MATCH (p:Person)-[:INVOLVED_IN|PARTICIPATED_IN]->(k:Case)
                WHERE elementId(p) IN $p_ids
                RETURN DISTINCT elementId(k) AS id, k.name AS name, k.summary AS summary, k.doc_id AS doc_id
                """,
                p_ids=target_p_ids,
            )
        else:
            case_rows = self.run(
                """
                MATCH (k:Case)
                WHERE k.doc_id IN $doc_ids
                RETURN DISTINCT elementId(k) AS id, k.name AS name, k.summary AS summary, k.doc_id AS doc_id
                """,
                doc_ids=doc_ids,
            )
        case_ids = [row["id"] for row in case_rows]

        # Add Case summaries
        for row in case_rows:
            if row.get("summary"):
                add_fact(f"Vụ việc '{row['name']}': {row['summary']}")

        # 4. People and Charges in these target cases (or specific target persons)
        charges_data = self.run(
            """
            MATCH (p:Person)-[:HAS_CHARGE]->(h:Charge)-[:OF_CRIME]->(c:Crime)
            MATCH (p)-[:INVOLVED_IN|PARTICIPATED_IN]->(k:Case)
            WHERE (elementId(p) IN $p_ids) OR (size($p_ids) = 0 AND elementId(k) IN $case_ids)
            RETURN DISTINCT p.name AS person, c.name AS crime, h.sentence_raw AS sentence,
                            h.sentence_type AS sentence_type, k.name AS case_name
            """,
            p_ids=target_p_ids,
            case_ids=case_ids,
        )
        target_crimes = set()
        for row in charges_data:
            target_crimes.add(row["crime"])
            st = f" bị tuyên: {row['sentence']}" if row["sentence"] else ""
            add_fact(f"Bị cáo '{row['person']}' trong vụ '{row['case_name']}'{st} về tội {row['crime']}.")

        # If no target person, also collect charges from case charged_with
        if not target_crimes and case_ids:
            for r in self.run("MATCH (k:Case)-[:CHARGED_WITH]->(c:Crime) WHERE elementId(k) IN $case_ids RETURN DISTINCT c.name AS crime", case_ids=case_ids):
                target_crimes.add(r["crime"])

        # 5. Articles and Clauses for target crimes:
        # Always include: Clause 1 (khung cơ bản) and Clause with maximum penalty (life/death or highest imprisonment)
        if target_crimes:
            clause_rows = self.run(
                """
                MATCH (c:Crime)<-[:DEFINES|DEFINES_CRIME]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE c.name IN $crimes
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text,
                                cl.penalty AS penalty, cl.penalty_types AS penalty_types, cl.max_years AS max_years
                ORDER BY a.id, cl.number
                """,
                crimes=list(target_crimes),
            )
            by_article = {}
            for row in clause_rows:
                by_article.setdefault(row["article_id"], []).append(row)

            for art_id, clauses in by_article.items():
                cl1 = next((c for c in clauses if c["number"] == 1), None)
                if cl1:
                    pen = f" (Khung hình phạt: {cl1['penalty']})" if cl1.get('penalty') else ""
                    add_fact(f"[{cl1['article_id']} - {cl1['title']}] khoản 1 (khung hình phạt cơ bản): {cl1['text']}{pen}")

                extreme_clauses = [c for c in clauses if any(pt in ("death", "life") for pt in (c.get("penalty_types") or []))]
                if extreme_clauses:
                    death_cl = next((c for c in extreme_clauses if "death" in (c.get("penalty_types") or [])), extreme_clauses[-1])
                    pen = f" (Mức hình phạt: {death_cl['penalty']})" if death_cl.get('penalty') else ""
                    add_fact(f"[{death_cl['article_id']} - {death_cl['title']}] khoản {death_cl['number']} (khung hình phạt cao nhất): {death_cl['text']}{pen}")
                else:
                    term_clauses = [c for c in clauses if c.get("max_years")]
                    if term_clauses:
                        max_c = max(term_clauses, key=lambda c: c["max_years"])
                        pen = f" (Mức hình phạt: {max_c['penalty']})" if max_c.get('penalty') else ""
                        add_fact(f"[{max_c['article_id']} - {max_c['title']}] khoản {max_c['number']} (khung hình phạt cao nhất): {max_c['text']}{pen}")

        # 6. Quantitative matching for case substances vs law rules (Q5)
        matched_rules = self.run(
            """
            MATCH (k:Case)-[:HAS_QUANTITY]->(q:Quantity)-[:OF_SUBSTANCE]->(sub:Substance)
            MATCH (r:QuantityRule)-[:APPLIES_TO]->(sub)
            MATCH (cl:Clause)-[:HAS_QUANTITY_RULE]->(r)
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl)
            MATCH (a)-[:DEFINES|DEFINES_CRIME]->(c:Crime)
            WHERE elementId(k) IN $case_ids
              AND (c.name IN $crimes OR size($crimes) = 0)
              AND q.lower IS NOT NULL AND r.lower IS NOT NULL
              AND q.lower >= r.lower
            RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text,
                            q.raw_amount AS amount, r.lower AS min_g, sub.name AS substance, cl.penalty AS penalty
            ORDER BY cl.number DESC
            """,
            case_ids=case_ids,
            crimes=list(target_crimes),
        )
        for row in matched_rules:
            pen = f" Khung hình phạt: {row['penalty']}." if row.get("penalty") else ""
            add_fact(
                f"[Định lượng áp dụng: {row['substance']} {row['amount']} >= {row['min_g']}g] "
                f"[{row['article_id']} - {row['title']}] khoản {row['number']}: {row['text']}.{pen}"
            )

        # 7. Aggregation for substances (Q6: Những vụ việc nào liên quan đến MDMA...)
        mentioned_subs = find_substances(question)
        if mentioned_subs and any(w in question.lower() for w in ("vụ việc", "những vụ", "liên quan")):
            agg_cases = self.run(
                """
                MATCH (k:Case)-[:HAS_QUANTITY]->(q:Quantity)-[:OF_SUBSTANCE]->(sub:Substance)
                WHERE sub.name IN $substances
                RETURN DISTINCT k.name AS case_name, k.summary AS summary, q.raw_amount AS amount, sub.name AS substance
                """,
                substances=mentioned_subs,
            )
            for row in agg_cases:
                add_fact(f"Vụ việc liên quan {row['substance']}: '{row['case_name']}' (tang vật: {row['amount']}). Tóm tắt: {row['summary']}")

        # 8. Direct article mention in question ("Điều 251", "Điều 255"...)
        article_nums = re.findall(r"[Đđ]iều (\d+)", question)
        if article_nums:
            direct_clauses = self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE any(num IN $article_nums WHERE a.id CONTAINS num)
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text, cl.penalty AS penalty
                ORDER BY a.id, cl.number
                """,
                article_nums=article_nums,
            )
            for row in direct_clauses:
                pen = f" (Hình phạt: {row['penalty']})" if row.get("penalty") else ""
                add_fact(f"[{row['article_id']} - {row['title']}] khoản {row['number']}: {row['text']}{pen}")

        # 9. Fallback if few structured facts found
        if len(facts) < 5:
            _, fallback_facts = self.seed_facts(question, doc_ids, limit=max_facts - len(facts))
            for f in fallback_facts:
                add_fact(f)

        return facts[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.suggested_constraints()
    articles = [parse_law_article(d) for d in law_docs]
    for a in articles:
        graph.add_law_article(a)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for d in news_docs:
        for case in extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes):
            graph.add_news_case(case, d)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(
            c.get("metadata", {}).get("doc_id")
            for c in chunks
            if c.get("metadata", {}).get("doc_id")
        ))
        facts = self.graph.context(question, doc_ids)
        facts_str = "\n".join(f"- {f}" for f in facts) if facts else "Không có dữ kiện từ đồ thị."
        chunks_str = "\n\n".join(f"[{i + 1}] {c.get('content', '')}" for i, c in enumerate(chunks)) if chunks else "Không có đoạn văn bản."
        prompt = GRAPH_PROMPT.format(facts=facts_str, chunks=chunks_str, question=question)
        return self.llm_fn(prompt)
