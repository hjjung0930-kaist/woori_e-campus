
#!/usr/bin/env python3
"""Evidence-grounded ESG rule scoring engine.

Input:  Rule Book xlsx + a folder of official-company PDFs.
Output: An xlsx containing evidence candidates, final decisions and score summaries.

The program never treats the Rule Book's provisional score as a final score.
Without --review-mode llm it produces only REVIEW_REQUIRED / MISSING_EVIDENCE
statuses. With --review-mode llm, only the selected PDF excerpts are sent to
the model for a final 1/0/N/A/미확인 decision.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader


SHEET = "채점_증거기록_양식"
RULE_SHEET = "통합_RuleBook_255"
FINAL_STATUSES = {"1", "0", "N/A", "미확인"}
COVERAGE_THRESHOLD = 0.80
STOPWORDS = {
    "기준", "공식", "공시", "확인", "관련", "정보", "보고서", "성과", "기업",
    "해당", "및", "또는", "대한", "통해", "있는", "수치", "단위", "연도", "범위",
    "2025", "2024", "2026", "the", "and", "for", "with", "from", "report",
}


def load_dotenv_file() -> None:
    """Load OPENAI_API_KEY from a local .env file without another package.

    The environment variable, when already set, always wins.  This keeps the
    key out of source code and lets a downloaded ZIP run with only a .env file.
    """
    dotenv = Path(__file__).with_name(".env")
    if not dotenv.exists():
        return
    for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass
class Page:
    document: str
    page: int
    text: str
    source_year: int | None = None
    document_type: str = "other"


class VisibleHtmlText(HTMLParser):
    """Extract visible text while skipping embedded CSS/JavaScript."""
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def tokens(text: str) -> list[str]:
    raw = re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9·/\-]{1,}", clean(text).lower())
    return [x for x in raw if x not in STOPWORDS and len(x) > 1]


def infer_reporting_year(text: str) -> int | None:
    """Infer the main performance/fiscal year only from clear cover-page phrases."""
    patterns = [
        r"(20\d{2})년도\s*회계연도",
        r"사업연도\s*(20\d{2})년\s*0?1월\s*0?1일.*?(?:12월\s*31일|12월31일)",
        r"(20\d{2})년\s*0?1월\s*0?1일\s*(?:부터|~).*?(?:12월\s*31일|12월31일)",
        r"(20\d{2})-0?1-0?1.*?(?:20\d{2})-12-31",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return int(match.group(1))
    return None


def infer_document_type(filename: str) -> str:
    normalized = unicodedata.normalize("NFC", filename)
    name = normalized.lower()
    if "지배구조" in normalized or "governance" in name:
        return "governance"
    if "사업보고서" in normalized or "annual" in name:
        return "business"
    if "지속가능" in normalized or "sustainability" in name or "esg" in name:
        return "sustainability"
    return "other"


def read_html(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            source = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            source = ""
    parser = VisibleHtmlText()
    parser.feed(source)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def extract_pages(pdf_dir: Path) -> tuple[list[Page], dict[str, int | None]]:
    pages: list[Page] = []
    files = sorted([*pdf_dir.rglob("*.pdf"), *pdf_dir.rglob("*.html"), *pdf_dir.rglob("*.htm")])
    if not files:
        raise FileNotFoundError(f"PDF 또는 HTML 공시문서를 찾을 수 없습니다: {pdf_dir}")
    years: dict[str, int | None] = {}
    for path in files:
        try:
            document_type = infer_document_type(path.name)
            if path.suffix.lower() in {".html", ".htm"}:
                text = read_html(path)
                source_year = infer_reporting_year(text[:30000])
                years[path.name] = source_year
                # DART/KIND HTML은 하나의 긴 문서여서 통째로 검색하면 모든 규칙의
                # 상위 후보가 되는 문제가 있다. 문단 길이 기준으로 나눠 가상 페이지처럼 취급한다.
                chunk_size = 5000
                for chunk_no, start in enumerate(range(0, len(text), chunk_size), start=1):
                    chunk = text[start:start + chunk_size]
                    if chunk:
                        pages.append(Page(path.name, chunk_no, chunk, source_year, document_type))
            else:
                reader = PdfReader(str(path))
                raw_pages = [(page.extract_text() or "") for page in reader.pages]
                source_year = infer_reporting_year(" ".join(raw_pages[:8]))
                years[path.name] = source_year
                for page_no, text in enumerate(raw_pages, start=1):
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        pages.append(Page(path.name, page_no, text, source_year, document_type))
        except Exception as exc:
            print(f"[경고] 문서 읽기 실패: {path.name} ({exc})")
    if not pages:
        raise RuntimeError("문서에서 추출된 텍스트가 없습니다. 스캔 PDF라면 OCR을 먼저 적용하세요.")
    return pages, years



# 검색어 확장 사전: 점수 규칙은 바꾸지 않고 후보 페이지 검색만 보강한다.
SYNONYM_GROUPS = [
    ("내부신고", "제보", "신문고", "사이버감사", "whistleblowing", "신고채널", "신고 채널"),
    ("신고자", "제보자", "신고인", "제보인", "익명", "비밀보장", "보복금지"),
    ("윤리", "준법", "컴플라이언스", "compliance", "행동강령", "윤리강령"),
    ("부패", "반부패", "뇌물", "금품", "청탁", "anti-corruption"),
    ("교육", "이수", "참여율", "수료", "교육시간"),
    ("제재", "징계", "위반", "조치", "적발"),
    ("협력사", "공급망", "공급업체", "supplier", "실사"),
    ("온실가스", "배출량", "scope 1", "scope 2", "스코프", "ghg"),
]


def expand_query(query: list[str], max_terms: int = 50) -> list[str]:
    result = list(dict.fromkeys(query))
    lower = " ".join(result).lower()
    for group in SYNONYM_GROUPS:
        if any(word in lower for word in group):
            result.extend(word for word in group if word not in result)
    return list(dict.fromkeys(result))[:max_terms]


def _normalized_quote(value: str) -> str:
    """Only remove PDF formatting differences; never forgive changed words/numbers."""
    value = unicodedata.normalize("NFKC", clean(value)).casefold()
    value = re.sub(r"[\u00ad\u200b\u200c\u200d]", "", value)
    return re.sub(r"\s+", "", value)


def validate_quote(answer: dict[str, Any], candidates: list[tuple[Page, float]]) -> dict[str, Any]:
    """긍정·부정 판정에 사용하는 인용문의 존재/출처를 기계적으로 점검한다."""
    if answer.get("final_status") not in {"1", "0", "N/A"}:
        return answer
    quote = clean(answer.get("quote"))
    document = clean(answer.get("document"))
    try:
        page_no = int(answer.get("page"))
    except (TypeError, ValueError):
        page_no = -1
    chosen = [p for p, _ in candidates if p.document == document and p.page == page_no]
    if not quote or not chosen or not any(_normalized_quote(quote) in _normalized_quote(p.text) for p in chosen):
        answer["reason"] = "인용문·문서·페이지 원문 대조 실패: 사람 검토 필요. " + clean(answer.get("reason"))
        answer["final_status"] = "미확인"
        answer["confidence"] = "low"
    else:
        answer["검증상태"] = "원문인용확인_의미판정별도검토"
        if answer.get("final_status") == "0":
            answer["검증상태"] = "0점_원문인용확인_미충족판정검토필요"
    return answer

def make_query(row: pd.Series) -> list[str]:
    return list(dict.fromkeys(tokens(" ".join([
        clean(row.get("세부질문")), clean(row.get("원문 근거/수치")),
        clean(row.get("검색한 문서·키워드")), clean(row.get("1점 인정조건")),
    ]))))


def retrieve(pages: list[Page], query: list[str], top_k: int, target_year: int,
             esg_area: str, wide: bool = False) -> list[tuple[Page, float]]:
    """서로 다른 검색어의 일치 수를 우선하고 한 문서의 후보 독점을 줄인다."""
    scored = []
    for p in pages:
        if p.source_year is not None and p.source_year != target_year:
            continue
        # G만 보지 말고 모든 유형의 공시에서 후보를 검색한다.
        lower = p.text.lower()
        matched = [term for term in query if term in lower]
        if not matched:
            continue
        distinct = len(matched)
        frequency = sum(min(2, lower.count(term)) for term in matched)
        score = float(distinct * 5 + frequency * 0.5)
        if p.document_type == 'sustainability':
            score += 2
        if p.document_type == 'governance' and esg_area == 'G':
            score += 2
        # 복수 근거 용어가 좁은 문맥에 함께 나타나면 우대한다.
        if distinct > 1:
            positions = sorted(lower.find(term) for term in matched)
            if positions[-1] - positions[0] <= 1500:
                score += 3
        scored.append((p, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    if not wide:
        return scored[:top_k]
    # 2차 검색에서는 문서별 후보 다양성을 확보한다.
    selected, doc_counts = [], Counter()
    for p, score in scored:
        if doc_counts[p.document] >= max(2, top_k // 2):
            continue
        selected.append((p, score))
        doc_counts[p.document] += 1
        if len(selected) >= top_k:
            break
    return selected


def compact_excerpt(text: str, query: list[str], limit: int = 3200) -> str:
    """가장 많은 검색어가 공존하는 구간을 선택한다. 원문 문자열은 변형하지 않는다."""
    if len(text) <= limit:
        return text.strip()
    lower = text.lower()
    starts = [lower.find(term) for term in query if lower.find(term) >= 0]
    if not starts:
        return text[:limit].strip()
    best_start, best_score = 0, -1
    for pos in starts:
        start = max(0, min(pos - 250, len(text) - limit))
        window = lower[start:start + limit]
        score = sum(1 for term in query if term in window)
        if score > best_score:
            best_start, best_score = start, score
    return text[best_start:best_start + limit].strip()


def llm_decision(rule: str, condition: str, target_year: int, candidates: list[tuple[Page, float]], model: str, query: list[str] | None = None) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("LLM 검증에는 openai 패키지가 필요합니다: pip install openai") from exc
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("LLM 검증에는 OPENAI_API_KEY 환경변수가 필요합니다.")

    evidence = "\n\n".join(
        f"[문서: {p.document} | PDF 페이지: {p.page}]\n{compact_excerpt(p.text, query or tokens(rule + ' ' + condition))}"
        for p, _ in candidates
    )
    prompt = f"""너는 ESG 공시 검증자다. 주어진 PDF 발췌문만 근거로 판정하라.
평가연도는 {target_year}년이다. 다른 연도의 성과·목표를 {target_year}년 실적으로 오인하지 마라.

세부질문: {rule}
1점 인정조건: {condition}

판정 원칙:
- 1: 발췌문이 모든 인정조건을 직접 충족한다.
- 0: 발췌문이 반대 사실 또는 명백한 미충족을 보여 준다.
- N/A: 기업·업종에 적용할 수 없는 항목임이 발췌문에 명시됐다.
- 미확인: 발췌문만으로 확인 불가.
문서나 페이지를 지어내지 말고, 인용문은 반드시 발췌문에 실제 있는 문구를 40단어 이내로 사용하라.
반드시 JSON만 반환하라:
{{"final_status":"1|0|N/A|미확인","reason":"짧은 판단 근거","document":"문서명 또는 빈값","page":정수 또는 null,"quote":"직접 인용문 또는 빈값","confidence":"high|medium|low"}}

발췌문:
{evidence}"""
    client = OpenAI()
    # 일부 최신 모델은 temperature 값을 기본값(1) 외에는 허용하지 않는다.
    # 판정 프롬프트는 구조화된 JSON·근거 제약으로 일관성을 확보한다.
    request_kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    if not model.startswith("gpt-6-"):
        request_kwargs["temperature"] = 0
    response = client.chat.completions.create(
        **request_kwargs,
    )
    raw = response.choices[0].message.content or "{}"
    result = json.loads(raw)
    if result.get("final_status") not in FINAL_STATUSES:
        result["final_status"] = "미확인"
    return validate_quote(result, candidates)


def load_records(rulebook: Path, company: str, target_year: int) -> pd.DataFrame:
    evidence = pd.read_excel(rulebook, sheet_name=SHEET)
    rules = pd.read_excel(rulebook, sheet_name=RULE_SHEET)
    rule_cols = ["규칙ID", "세부질문", "1점 인정조건", "ESG영역", "분류"]
    evidence = evidence[evidence["기업"].astype(str).str.strip().eq(company)].copy()
    if evidence.empty:
        available = ", ".join(sorted(pd.read_excel(rulebook, sheet_name=SHEET)["기업"].dropna().astype(str).unique()))
        raise ValueError(f"'{company}' 기업을 찾지 못했습니다. 사용 가능 기업: {available}")
    # (규칙ID, 세부질문) 복합키를 사용해 업종별 반복 Rule ID 충돌을 피한다.
    evidence = evidence.merge(rules[rule_cols], on=["규칙ID", "세부질문"], how="left", suffixes=("", "_rule"))
    evidence["평가연도"] = target_year
    return evidence


def score_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for group_name, part in [("전체", df), *[(x, d) for x, d in df.groupby("ESG영역", dropna=False)]]:
        statuses = Counter(part["최종판정"].fillna("미확인"))
        ones = statuses.get("1", 0)
        zeros = statuses.get("0", 0)
        nas = statuses.get("N/A", 0)
        decided = ones + zeros + nas
        coverage = decided / len(part) if len(part) else 0
        scoring_denominator = ones + zeros
        score_rate = ones / scoring_denominator if scoring_denominator else None
        score_status = "산출 가능" if coverage >= COVERAGE_THRESHOLD else "산출 보류(근거확인률 부족)"
        rows.append({
            "구분": group_name, "대상 규칙 수": len(part), "1점": ones, "0점": zeros,
            "N/A": nas, "미확인": statuses.get("미확인", 0),
            "검토필요": statuses.get("검토필요", 0), "점수 산정 분모": scoring_denominator,
            "판정완료 항목": decided,
            "근거확인률": round(coverage, 4),
            "참고 1점률(판정완료 기준)": round(score_rate, 4) if score_rate is not None else None,
            "점수 상태": score_status,
        })
    return pd.DataFrame(rows)


def pending_rule_keys(previous: pd.DataFrame) -> set[tuple[str, str]]:
    pending = previous[previous["최종판정"].fillna("미확인").isin(["미확인", "검토필요"])]
    return {(clean(row["규칙ID"]), clean(row["세부질문"])) for _, row in pending.iterrows()}


def run(args: argparse.Namespace) -> Path:
    rulebook = Path(args.rulebook).expanduser().resolve()
    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    records = load_records(rulebook, args.company, args.target_year)
    previous: pd.DataFrame | None = None
    recheck_count = len(records)
    if args.refine_unconfirmed:
        previous = pd.read_excel(Path(args.refine_unconfirmed).expanduser().resolve(), sheet_name="판정결과")
        keys = pending_rule_keys(previous)
        records = records[records.apply(lambda row: (clean(row["규칙ID"]), clean(row["세부질문"])) in keys, axis=1)].copy()
        recheck_count = len(records)
        if records.empty:
            raise ValueError("기존 결과에 재검증할 미확인/검토필요 항목이 없습니다.")
    pages, document_years = extract_pages(pdf_dir)
    output_rows: list[dict[str, Any]] = []

    for sequence, (_, row) in enumerate(records.iterrows(), start=1):
        print(f"[{sequence}/{len(records)}] {clean(row['규칙ID'])} 검증 중")
        query = expand_query(make_query(row)) if args.enhanced_search else make_query(row)
        top_k = max(args.top_k, 5) if args.refine_unconfirmed else args.top_k
        candidates = retrieve(pages, query, top_k, args.target_year, clean(row.get("ESG영역")))
        search_round = 1
        base = row.to_dict()
        # 파일럿 기간을 2025년 성과로 확정할 경우에도 원본 Rule Book을 덮어쓰지 않는다.
        # 실행에 사용한 기준연도만 결과 파일에 별도로 남긴다.
        execution_condition = re.sub(r"20\d{2}년 기준", f"{args.target_year}년 기준", clean(row.get("1점 인정조건")))
        base["실행 1점 인정조건"] = execution_condition
        base["기존후보판정"] = clean(row.get("판정(1/0/N/A/미확인)"))
        base["검색어"] = "; ".join(query[:25])
        base["후보근거"] = "\n\n".join(
            f"{p.document} p.{p.page} (검색점수 {score})\n{compact_excerpt(p.text, query)}"
            for p, score in candidates
        )
        base["후보문서·페이지"] = "; ".join(f"{p.document} p.{p.page}" for p, _ in candidates)
        base["평가연도 불일치로 제외된 문서"] = "; ".join(
            f"{name} ({year} 성과)" for name, year in document_years.items()
            if year is not None and year != args.target_year
        )
        base["재검증여부"] = "Y" if args.refine_unconfirmed else "N"
        # 후보 부재는 LLM 호출 전에 검색 범위를 넓힌다.
        if not candidates and args.enhanced_search:
            candidates = retrieve(pages, query, max(8, top_k * 2), args.target_year,
                                  clean(row.get("ESG영역")), wide=True)
            search_round = 2
        if not candidates:
            base.update({"최종판정": "미확인", "검증사유": "공식 PDF에서 관련 후보 근거를 찾지 못함", "최종인용문": "", "신뢰도": "low"})
        elif args.review_mode == "llm":
            answer = llm_decision(clean(row.get("세부질문")), execution_condition,
                                  args.target_year, candidates, args.model, query)
            if args.enhanced_search and answer.get("final_status") == "미확인":
                wide_candidates = retrieve(pages, query, max(8, top_k * 2), args.target_year,
                                           clean(row.get("ESG영역")), wide=True)
                old_keys = {(p.document, p.page) for p, _ in candidates}
                if any((p.document, p.page) not in old_keys for p, _ in wide_candidates):
                    answer2 = llm_decision(clean(row.get("세부질문")), execution_condition,
                                           args.target_year, wide_candidates, args.model, query)
                    search_round = 2
                    candidates = wide_candidates
                    # Never discard a better verdict merely because a second search ran.
                    if answer2.get("final_status") != "미확인":
                        answer = answer2
                    else:
                        answer["2차검색사유"] = clean(answer2.get("reason"))
            base.update({
                "최종판정": answer.get("final_status", "미확인"), "검증사유": answer.get("reason", ""),
                "최종문서": answer.get("document", ""), "최종페이지": answer.get("page", ""),
                "최종인용문": answer.get("quote", ""), "신뢰도": answer.get("confidence", "low"),
                "검증상태": answer.get("검증상태", "미확인_검토필요"),
                "2차검색사유": answer.get("2차검색사유", ""),
            })
        else:
            base.update({"최종판정": "검토필요", "검증사유": "후보 근거 추출 완료. --review-mode llm 또는 사람 검토 필요", "최종인용문": "", "신뢰도": "unreviewed"})
        base["검색차수"] = search_round
        # 2차 검색 시 실제 최종 검색 후보로 로그를 갱신한다.
        base["후보문서·페이지"] = "; ".join(f"{p.document} p.{p.page}" for p, _ in candidates)
        base["후보근거"] = "\n\n".join(
            f"{p.document} p.{p.page} (검색점수 {score})\n{compact_excerpt(p.text, query)}"
            for p, score in candidates
        )
        output_rows.append(base)

    result = pd.DataFrame(output_rows)
    if previous is not None:
        refreshed_keys = {(clean(row["규칙ID"]), clean(row["세부질문"])) for _, row in result.iterrows()}
        preserved = previous[~previous.apply(lambda row: (clean(row["규칙ID"]), clean(row["세부질문"])) in refreshed_keys, axis=1)].copy()
        if "재검증여부" not in preserved.columns:
            preserved["재검증여부"] = "N"
        result = pd.concat([preserved, result], ignore_index=True, sort=False)
        order = {(clean(row["규칙ID"]), clean(row["세부질문"])): idx for idx, row in load_records(rulebook, args.company, args.target_year).iterrows()}
        result["_order"] = result.apply(lambda row: order.get((clean(row["규칙ID"]), clean(row["세부질문"])), 999999), axis=1)
        result = result.sort_values("_order").drop(columns="_order").reset_index(drop=True)
    summary = score_summary(result)
    review = result[result["최종판정"].isin(["검토필요", "미확인"])].copy()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="판정결과", index=False)
        summary.to_excel(writer, sheet_name="점수요약", index=False)
        review.to_excel(writer, sheet_name="검토대상", index=False)
        pd.DataFrame([{
            "기업": args.company, "평가연도": args.target_year, "검증방식": args.review_mode,
            "주의": "기존 Rule Book의 잠정 판정은 최종 점수가 아님. 최종판정 열만 점수 산정에 사용.",
            "점수 해석": "근거확인률이 80% 미만이면 참고 1점률은 최종 ESG 점수로 사용하지 않음.",
            "재검증 규칙 수": recheck_count,
            "입력 문서 수": len({p.document for p in pages}), "추출 페이지 수": len(pages),
            "문서별 감지 성과연도": "; ".join(f"{name}: {year or '미감지'}" for name, year in document_years.items()),
        }]).to_excel(writer, sheet_name="실행정보", index=False)
    print(f"완료: {output}")
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ESG Rule Book 증거검증·점수산출 모델 v2.1 (v1 검색 복원)")
    parser.add_argument("--rulebook", required=True, help="최신 Rule Book xlsx 경로")
    parser.add_argument("--pdf-dir", required=True, help="해당 기업의 공식 PDF를 넣은 폴더")
    parser.add_argument("--company", required=True, help="Rule Book의 기업명과 정확히 동일하게 입력")
    parser.add_argument("--target-year", type=int, default=2025, help="평가 성과연도 (기본 2025)")
    parser.add_argument("--output", default="esg_result.xlsx", help="결과 xlsx 경로")
    parser.add_argument("--top-k", type=int, default=3, help="규칙별 LLM/사람 검토에 보낼 후보 페이지 수")
    parser.add_argument("--review-mode", choices=["manual", "llm"], default="manual", help="manual은 후보만 추출, llm은 최종 판정")
    parser.add_argument("--model", default="gpt-4.1-mini", help="LLM 검증에 쓸 OpenAI 모델")
    parser.add_argument("--enhanced-search", action="store_true", help="유의어 확장·후보 다양화·미확인 2차 검색")
    parser.add_argument("--refine-unconfirmed", help="기존 결과 xlsx의 미확인·검토필요 항목만 재검증")
    return parser.parse_args()


if __name__ == "__main__":
    load_dotenv_file()
    run(parse_args())
