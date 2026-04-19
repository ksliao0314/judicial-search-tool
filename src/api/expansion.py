"""Keyword 展開 preview API + 同義詞字典回饋 endpoint + 設定頁 CRUD。

律師送搜尋前，前端可呼叫 /api/expand-preview 取得展開清單（citation 變體 +
synonym 變體），顯示在 UI 讓律師勾刪確認。律師對某 variant 按「✓/×」後，
前端呼叫 /api/synonym-feedback 累積事務所資產的 accept/reject signal。
"""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.db import database as db
from src.pipeline import synonym_expander
from src.pipeline.citation_normalizer import (
    Citation, parse_keyword, top_search_variants, generate_variants,
    ensure_law_known,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["expansion"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ExpandRequest(BaseModel):
    keyword: str = Field(min_length=1, description="律師原始輸入（可含空格分多 keyword）")


class ExpandedKeyword(BaseModel):
    original: str
    type: str  # 'citation' | 'synonym'
    citation: dict | None = None
    search_variants: list[str]
    filter_variants: list[str]
    variant_metadata: list[dict] = Field(default_factory=list)
    from_cache: bool = False


class ExpandResponse(BaseModel):
    keywords: list[ExpandedKeyword]


class FeedbackRequest(BaseModel):
    canonical: str
    variant: str
    accepted: bool


class ShouldPreviewRequest(BaseModel):
    keyword: str = Field(min_length=1)


class ShouldPreviewResponse(BaseModel):
    needs_preview: bool
    reason: str  # debug / UX 顯示用


async def _keyword_needs_preview(raw_kw: str) -> tuple[bool, str]:
    """純本地判斷，零 LLM call。

    需要 preview 的情境：
      - keyword 是條號（prase_keyword 成功）：會展開多種格式變體，律師該看
      - keyword 的法名已在字典（law_abbreviations.json）：會展開全稱/簡稱
      - keyword 本身已在 synonym_dictionary 有 confirmed / candidate variants
    其他情境（純字串字典 miss）→ 不 preview 直接搜原字。
    """
    from src.pipeline.citation_normalizer import parse_keyword, is_law_in_dict

    kw = raw_kw.strip()
    if not kw:
        return False, "empty"

    # 條號
    c = parse_keyword(kw)
    if c is not None:
        if c.law is not None:
            return True, f"條號（法名：{c.law}）"
        return True, "條號（無法名但會展開格式變體）"

    # 法名已在字典（非條號但是純法名輸入）— parse_keyword 可能返回 None，但
    # is_law_in_dict 能確認此詞為已知法名的全稱 / 簡稱
    if is_law_in_dict(kw):
        return True, "已知法名"

    # 字典中有確認的 variants
    existing = await db.get_synonyms(kw, min_tier="confirmed")
    if existing and any(r["variant"] != kw for r in existing):
        return True, f"字典有 {len(existing)} 個 confirmed 同義詞"

    return False, "純字串無展開 → 直接搜"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/expand-preview", response_model=ExpandResponse)
async def expand_preview(
    body: ExpandRequest,
    x_api_key: str | None = Header(default=None),
) -> ExpandResponse:
    """對律師 keyword 做展開預覽（UI 審閱用，尚未真正建立任務）。"""
    raw_keywords = [kw.strip() for kw in body.keyword.split() if kw.strip()]
    results: list[ExpandedKeyword] = []

    for kw in raw_keywords:
        # 法名 LLM fallback：若輸入的法名不在字典（如「衛廣法」），先 call Claude
        # 展開並寫回 JSON，使後續 parse_keyword / generate_variants 能拿到完整變體
        await ensure_law_known(kw, api_key=x_api_key)
        citation = parse_keyword(kw)
        if citation is not None:
            sv = top_search_variants(citation, limit=5)
            fv = generate_variants(citation, with_law_prefix=True)
            if citation.law is not None:
                fv = fv + generate_variants(citation, with_law_prefix=False)
            results.append(ExpandedKeyword(
                original=kw,
                type="citation",
                citation={
                    "law": citation.law,
                    "article": citation.article,
                    "sub": citation.sub,
                    "paragraph": citation.paragraph,
                    "item": citation.item,
                    "subitem": citation.subitem,
                },
                search_variants=sv,
                filter_variants=fv,
                from_cache=False,
            ))
        else:
            # Synonym path
            try:
                expansion = await synonym_expander.expand(
                    kw, api_key=x_api_key, use_cache=True, verify_corpus=True,
                )
                variants_list = [v["variant"] for v in expansion["variants"]]
                results.append(ExpandedKeyword(
                    original=kw,
                    type="synonym",
                    citation=None,
                    search_variants=variants_list,
                    filter_variants=variants_list,
                    variant_metadata=expansion["variants"],
                    from_cache=expansion["from_cache"],
                ))
            except Exception as exc:
                logger.warning("synonym 預覽 %s 失敗：%s", kw, exc)
                results.append(ExpandedKeyword(
                    original=kw, type="synonym", citation=None,
                    search_variants=[kw], filter_variants=[kw],
                    variant_metadata=[], from_cache=False,
                ))

    return ExpandResponse(keywords=results)


@router.post("/should-preview", response_model=ShouldPreviewResponse)
async def should_preview(body: ShouldPreviewRequest) -> ShouldPreviewResponse:
    """判斷 UI 律師輸入的 keyword(s) 是否需要顯示搜尋計畫 preview。

    多個 keyword 空格分隔時：**任一**需要 preview 就 True。
    """
    raw_keywords = [k.strip() for k in body.keyword.split() if k.strip()]
    if not raw_keywords:
        return ShouldPreviewResponse(needs_preview=False, reason="empty")

    reasons = []
    for kw in raw_keywords:
        needs, reason = await _keyword_needs_preview(kw)
        if needs:
            reasons.append(f"{kw}：{reason}")
    if reasons:
        return ShouldPreviewResponse(needs_preview=True, reason="；".join(reasons))
    return ShouldPreviewResponse(needs_preview=False, reason="全部 keyword 都是純字串無展開")


@router.post("/synonym-feedback", status_code=200)
async def synonym_feedback(body: FeedbackRequest) -> dict:
    """律師按 UI 的 ✓/× 時，記錄 accept/reject signal 至字典。"""
    await db.record_synonym_feedback(body.canonical, body.variant, body.accepted)
    return {"ok": True}


@router.get("/synonyms/{canonical}")
async def get_synonyms(canonical: str) -> list[dict]:
    """查看某 canonical 已累積的字典條目（debug / 律師檢視字典用）。"""
    return await db.get_synonyms(canonical)


class AddSynonymRequest(BaseModel):
    canonical: str
    variant: str


@router.post("/synonyms/add", status_code=201)
async def add_synonym(body: AddSynonymRequest) -> dict:
    """律師手動新增同義詞，直接進入 confirmed 詞庫。"""
    await db.upsert_synonyms(
        canonical=body.canonical,
        variants=[body.variant],
        source="user_added",
    )
    # 直接升級為 confirmed
    await db.record_synonym_feedback(body.canonical, body.variant, accepted=True)
    await db.record_synonym_feedback(body.canonical, body.variant, accepted=True)
    await db.record_synonym_feedback(body.canonical, body.variant, accepted=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 設定頁：同義詞庫管理
# ---------------------------------------------------------------------------

class DeleteSynonymRequest(BaseModel):
    canonical: str
    variant: str


@router.get("/settings/synonyms")
async def list_all_synonyms() -> list[dict]:
    """列出全部同義詞字典條目（設定頁用）。"""
    return await db.get_all_synonyms()


@router.delete("/settings/synonyms")
async def delete_synonym(body: DeleteSynonymRequest) -> dict:
    """硬刪除字典中特定 canonical/variant 組合。"""
    deleted = await db.delete_synonym_variant(body.canonical, body.variant)
    if not deleted:
        raise HTTPException(status_code=404, detail="條目不存在")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 設定頁：法條簡稱管理
# ---------------------------------------------------------------------------

_LAW_ABBREV_PATH  = Path(__file__).parent.parent / "data" / "law_abbreviations.json"
_MCP_CACHE_DB     = Path(__file__).parent.parent.parent / "mcp-taiwan-legal-db" / "data" / "cache" / "legal_mcp.db"


def _load_law_abbrev() -> dict:
    if not _LAW_ABBREV_PATH.exists():
        return {}
    return json.loads(_LAW_ABBREV_PATH.read_text(encoding="utf-8"))


def _save_law_abbrev(data: dict) -> None:
    _LAW_ABBREV_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@router.get("/settings/law-abbreviations")
async def list_law_abbreviations() -> dict:
    """回傳法條簡稱字典（全稱 → [簡稱, ...]）。"""
    return _load_law_abbrev()


class DeleteLawRequest(BaseModel):
    full_name: str
    abbreviation: str | None = None  # None = 刪整條；非 None = 只刪特定簡稱


# ---------------------------------------------------------------------------
# 設定頁：快取管理
# ---------------------------------------------------------------------------

@router.get("/settings/cache-stats")
async def get_cache_stats() -> dict:
    """回傳 MCP 快取 DB 的大小與各表筆數。"""
    if not _MCP_CACHE_DB.exists():
        return {"db_size_bytes": 0, "tables": {}}

    import aiosqlite
    from datetime import datetime, timezone

    db_size = _MCP_CACHE_DB.stat().st_size
    now = datetime.now(timezone.utc).isoformat()

    tables: dict[str, dict] = {}
    async with aiosqlite.connect(_MCP_CACHE_DB) as db:
        for table in ("judgment_cache", "search_cache", "regulation_cache"):
            cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
            total = (await cur.fetchone())[0]
            cur = await db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE expires_at < ?", (now,)
            )
            expired = (await cur.fetchone())[0]
            tables[table] = {"total": total, "expired": expired}

    return {"db_size_bytes": db_size, "tables": tables}


@router.delete("/settings/cache")
async def clear_cache(expired_only: bool = True) -> dict:
    """清除 MCP 快取。

    expired_only=true（預設）：只刪過期條目，不影響仍有效的快取。
    expired_only=false：清空全部三張表（下次搜尋全部重新抓）。
    """
    if not _MCP_CACHE_DB.exists():
        return {"deleted": {}, "db_size_before": 0, "db_size_after": 0}

    import aiosqlite
    from datetime import datetime, timezone

    size_before = _MCP_CACHE_DB.stat().st_size
    now = datetime.now(timezone.utc).isoformat()
    deleted: dict[str, int] = {}

    async with aiosqlite.connect(_MCP_CACHE_DB) as db:
        for table in ("judgment_cache", "search_cache", "regulation_cache"):
            if expired_only:
                cur = await db.execute(
                    f"DELETE FROM {table} WHERE expires_at < ?", (now,)
                )
            else:
                cur = await db.execute(f"DELETE FROM {table}")
            deleted[table] = cur.rowcount
        await db.commit()

    # VACUUM 必須在 transaction 外執行（獨立連線 + isolation_level=None）
    try:
        async with aiosqlite.connect(_MCP_CACHE_DB, isolation_level=None) as db:
            await db.execute("VACUUM")
    except Exception:
        pass  # VACUUM 失敗不影響清除結果，空間下次再釋放

    size_after = _MCP_CACHE_DB.stat().st_size
    return {
        "deleted": deleted,
        "db_size_before": size_before,
        "db_size_after": size_after,
    }


@router.delete("/settings/law-abbreviations")
async def delete_law_abbreviation(body: DeleteLawRequest) -> dict:
    """刪除法條簡稱字典條目。

    abbreviation=None → 刪整條法名（含所有簡稱）
    abbreviation=指定值 → 只刪該簡稱，全稱條目保留
    """
    data = _load_law_abbrev()
    if body.full_name not in data:
        raise HTTPException(status_code=404, detail="法名不存在")

    if body.abbreviation is None:
        del data[body.full_name]
    else:
        abbrevs = data[body.full_name]
        if body.abbreviation not in abbrevs:
            raise HTTPException(status_code=404, detail="簡稱不存在")
        abbrevs.remove(body.abbreviation)
        if not abbrevs:          # 簡稱清空 → 順帶刪整條
            del data[body.full_name]
        else:
            data[body.full_name] = abbrevs

    _save_law_abbrev(data)
    return {"ok": True}
