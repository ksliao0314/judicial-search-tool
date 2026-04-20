"""舊制大法官解釋（釋字第 1-813 號）結構解析。

跟一般判決解析完全隔離 — 813 則是固定集合、不會再新增、不會污染 judicial_parser。

設計根據實證勘查（每 100 號抽 10 則、共 90 則樣本）：
- 79 則（釋字 1-79）只有主文、無獨立理由書 → 特殊分支
- 其他 734 則有理由書、用統一 section splitter
- Era D/E（601 號後）有「一、XXX」sub-section header、額外切 reasoning

Section schema（所有 case 共用）：
    {
      "summary": str,       # main_text 原樣
      "issues": str,        # issues 原樣（解釋爭點）
      "sections": [
        {"role": "petitioner_claim",    "title": "聲請意旨",   "text": str},
        {"role": "procedural_ruling",   "title": "受理程序",   "text": str},  # 601 型有
        {"role": "court_reasoning",     "title": "本院見解",   "text": str, "subsections": [...]},
        {"role": "conclusion",          "title": "結論",       "text": str},
        {"role": "signatures",          "title": "大法官署名", "text": str},
      ],
      "era": "A"-"E",       # 年代分層 tag（debug / UI 分類用）
    }

Role 對 AI 精讀 + Reader UI 的意義：
- 律師問「法院見解」→ prompt 送 court_reasoning + conclusion + procedural_ruling、
  excerpt 不得取自 petitioner_claim / signatures
- procedural_ruling：釋字 601 型「壹、受理程序」中法院對聲請案程序要件的論述
  （雖然結構上放在 petitioner 段之前、但語意是本院認定、應併入 reasoning 參考）
- Reader 可折疊 signatures（預設收）、展開 court_reasoning（預設開）
"""
from __future__ import annotations

import re
from typing import Any

# ─── Era 分層（依勘查結果）─────────────────────────
_ERA_BOUNDARIES = (
    (100,  "A"),   # 1-100: 元祖期、多數無理由書
    (300,  "B"),   # 101-300: 短札期
    (600,  "C"),   # 301-600: 成長期
    (800,  "D"),   # 601-800: 爆量期、出現 sub-header
    (813,  "E"),   # 801-813: 過渡期（新憲法訴訟法前夕）
)


def _era_of(cid: int) -> str:
    for upper, era in _ERA_BOUNDARIES:
        if cid <= upper:
            return era
    return "E"  # defensive: cid > 813 理論不會發生


# ─── 文字清理 ─────────────────────────────────
def _clean(text: str) -> str:
    """正規化：\xa0 → space、連續空行壓一、尾端空白。"""
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    # 壓連續 3+ 空行為 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_paragraphs(text: str) -> list[str]:
    """依空行切段、去空白段。"""
    if not text:
        return []
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


# ─── 簽名段（所有 era 尾端）─────────────────────────
# 「大法官會議主席　院　長　XXX」或「大法官會議主席　大法官　XXX」開頭
# 亦有特例：早期少數只寫「大法官　XXX」無主席標記
_SIG_HEAD_RE = re.compile(
    r"(?m)^[\s　]*大法官(?:會議)?(?:主席)?[\s　]"
)


def _split_signatures(paragraphs: list[str]) -> tuple[list[str], str]:
    """從後往前找第一個 match 「大法官會議主席...」的段、該段起皆視為 signatures。

    邊角：
    - 若找不到任何 signature marker（Era A 極少數、或資料異常）→ signatures=""
    - 只認段首的 marker、避免內文引述「大法官會議...」誤判
    """
    if not paragraphs:
        return paragraphs, ""
    for i, p in enumerate(paragraphs):
        if _SIG_HEAD_RE.match(p):
            body = paragraphs[:i]
            sigs = "\n\n".join(paragraphs[i:])
            return body, sigs
    return paragraphs, ""


# ─── 聲請段（前置段）─────────────────────────
# 強信號：首段若 match 任一、則切聲請段
_PETITIONER_OPENERS = [
    re.compile(r"^\s*本件(?:因|係|聲請人|聲請(?:案|釋憲|解釋))"),  # 本件因/係/聲請人/聲請案/聲請釋憲…
    re.compile(r"^\s*本件聲請人?(?:聲請)?意旨"),
    # 釋字520 型：「本件行政院為決議停止興建核能…」「本件立法院…」
    # 「本件」+ 機關名（中央 5 院 / 總統府 / 任何 XX 部 / XX 署 / XX 會）
    re.compile(
        r"^\s*本件(?:行政院|立法院|考試院|監察院|司法院|總統府|"
        r"[\u4e00-\u9fff]{2,15}(?:部|署|院|會|局|廳|政府))"
    ),
    # 「聲請人」後接任何中文字：涵蓋「聲請人陳清秀」「聲請人臺灣臺北地方法院」「聲請人慈祐宮」等
    # Reasoning 段幾乎不會以「聲請人」開頭（都是「按/查/憲法/本院」），此 pattern 不會誤判
    re.compile(r"^\s*聲請人[\u4e00-\u9fff]"),
    re.compile(r"^\s*聲請意旨略?稱?"),
    re.compile(r"^\s*(?:原因案件|本件審理).{0,20}?(?:聲請|提請)"),
    re.compile(r"^\s*立法委員.{0,30}?(?:等人?|聲請|認.{1,30}?違憲)"),
    re.compile(r"^\s*緣.{0,30}?(?:聲請人|聲請本院)"),  # 釋字813「緣聲請人慈祐宮…」
    # Era D/E 首段常見 outline 開頭：「壹、受理程序」「一、本件聲請人」
    re.compile(r"^\s*(?:壹|一)、.{0,10}?(?:受理|程序|聲請)"),
]

# 聲請段延續信號：第二段以後若 match 這些、繼續納入聲請段
# 覆蓋：程序 meta、聲請人續述、關係機關 / 各部會機關陳述（603、445 型）+ 419/601 特殊結構
_PETITIONER_CONTINUATIONS = [
    # 次查 prefix（445: 次查聲請人主張略稱）+ 再查 / 又查 / 末查
    re.compile(r"^\s*(?:次查|再查|又查|末查)(?:聲請人|關係機關)"),
    # 聲請人續述（本件聲請人主張略稱 / 聲請人主張 / 聲請人則主張 / 本件聲請意旨）
    re.compile(r"^\s*本件聲請(?:意旨|釋憲|解釋|案)"),  # 601 P2「本件聲請意旨…」
    re.compile(r"^\s*(?:本件)?聲請人(?:[\u4e00-\u9fff]{0,30})?(?:主張|略稱|認|指摘|聲請|聲明)"),
    # 關係機關 / 相關機關 陳述（「關係機關X」「相關機關X則主張」）
    re.compile(r"^\s*(?:關係|相關)機關[\u4e00-\u9fff]{0,20}?(?:[則之]?主張|略稱|略以|陳述|答辯|稱)"),
    # 廣義機關 pattern：任何 2-15 字的中央 / 地方機關（部 / 署 / 院 / 會 / 局）+ 主張 / 答辯
    # 445: 行政院則主張 / 法務部（兼代行政院）主張 / 內政部及內政部警政署之主張 / 交通部提出書狀主張
    re.compile(
        r"^\s*[\u4e00-\u9fff]{2,20}(?:部|署|院|會|局|廳|政府)"
        r"(?:[\u4e00-\u9fff（）\s、及兼代、]{0,40})?"
        r"(?:[之則]?主張|主張略?[稱以]|略稱|略以|陳述|答辯|提出書狀|(?:則)?稱)"
    ),
    # 419 型：enumeration 內的 petitioner 各案（「一、立法委員X等...聲請」「二、立法委員Y等…」）
    # 窄化：「一、」後 0-10 字內必須出現 petitioner/agency keyword，避免誤吃 court reasoning sub-header
    re.compile(
        r"^\s*[一二三四五六七八九十]{1,3}、.{0,10}?"
        r"(?:立法委員|聲請人|本件|行政院|司法院|考試院|監察院|立法院|總統|副總統|"
        r"關係機關|相關機關)"
    ),
    # 419 P6/P11 型：「本件前述第X案聲請人之主張略稱」「前述第四案聲請人及關係機關XX主張」
    re.compile(r"^\s*(?:本件)?前述第.{0,15}?案(?:聲請人|.{0,20}?主張|.{0,20}?關係機關)"),
    re.compile(r"^\s*本院就聲請"),
    re.compile(r"^\s*本院(?:於|依).{1,20}?(?:通知|邀請|舉行)"),  # 本院舉行言詞辯論等程序 meta
]

# 強 boundary：法院正式進入見解的明確轉折語、一 match 立即切入 reasoning
# 釋字 419 P17 用「本件斟酌」而非「本院斟酌」、涵蓋兩種。
_COURT_REASONING_HARD_BOUNDARIES = [
    re.compile(r"(?:本院|本件)(?:斟酌|審酌)(?:全(?:辯論)?意旨)?.{0,20}?作成本解釋"),
    re.compile(r"理由(?:詳述)?如(?:下|左)(?:$|[:：])"),  # 「理由如下：」
    re.compile(r"本院爰(?:依|就|為).{0,30}?作成"),
    re.compile(r"爰作成本解釋"),  # 750/802/805 等「爰作成本解釋，理由如下」
]

# 中止信號（次強）：進入 court reasoning 的常見 opener
_COURT_REASONING_OPENERS = [
    re.compile(r"^\s*按[^，。]{5,}"),
    re.compile(r"^\s*查[^，。]{5,}"),
    re.compile(r"^\s*憲法第.{1,8}條"),
    re.compile(r"^\s*(?:本院|本院大法官)(?:業經)?(?:曾|已|釋字|解釋)"),
    re.compile(r"^\s*(?:本(?:件|案)(?:解釋|審理|爭點))"),
]


# 釋字 601 型：petitioner 以「壹、受理程序」開頭、遇下一個 top-level 大寫數字（貳/參…）
# 即為 reasoning boundary。此 stateful pattern 跟「一、二、三」outline 不同層級、不衝突。
_DAZHENG_TRANSITION_RE = re.compile(r"^\s*(?:貳|參|肆|伍|陸)、")
_DAZHENG_OPENER_RE = re.compile(r"^\s*壹、")

# 「本件聲請意旨 / 聲請釋憲 / 聲請解釋」等明確聲請 chunk 起手—
# 用於在 壹、受理程序 mode 內切出 petitioner_claim 與 procedural_ruling 邊界
_PETITIONER_CHUNK_RE = re.compile(
    r"^\s*(?:本件)?聲請(?:人|意旨|釋憲|解釋|案)"
)


def _split_procedural_ruling(petitioner_paras: list[str]) -> tuple[list[str], list[str]]:
    """把 壹、受理程序 mode 切出來的 petitioner section 再細分：
    前段 petitioner_claim（opener + 本件聲請意旨-類 chunk）
    後段 procedural_ruling（法院對程序要件的認定論述）

    邏輯：
    - 首段是 壹、 header（例如「壹、受理程序」）→ 歸 petitioner
    - 找第一個 _PETITIONER_CHUNK_RE match（如「本件聲請意旨」）+ 之前所有段 → petitioner
    - 其後到列表尾 → procedural_ruling（若有）
    - 若找不到切點（整段都不像 procedural）→ 全歸 petitioner

    回傳 (petitioner_paras, procedural_paras)。
    """
    if not petitioner_paras:
        return [], []
    # 第一段必須是 壹、 header、否則本 function 不適用（caller 應已檢查）
    if not _DAZHENG_OPENER_RE.match(petitioner_paras[0]):
        return petitioner_paras, []

    # 從 index 1 起找第一個 petitioner chunk marker
    chunk_end_idx = None  # 指向「本件聲請意旨」段的 idx（含該段仍歸 petitioner）
    for i in range(1, len(petitioner_paras)):
        if _PETITIONER_CHUNK_RE.match(petitioner_paras[i]):
            chunk_end_idx = i
            break

    if chunk_end_idx is None:
        # 無明確 petitioner chunk → 全部歸 petitioner（保守）
        return petitioner_paras, []

    # 切分：[0, chunk_end_idx] = petitioner、 [chunk_end_idx+1, :] = procedural
    petitioner = petitioner_paras[: chunk_end_idx + 1]
    procedural = petitioner_paras[chunk_end_idx + 1 :]
    return petitioner, procedural


def _split_petitioner_claims(paragraphs: list[str]) -> tuple[list[str], list[str]]:
    """從頭開始認聲請段。

    規則：
    1. 首段不符 petitioner opener → 整段歸 reasoning
    2. 首段符合 → 從第二段起掃：
       - 遇 hard boundary（「理由如下：」「本院斟酌...作成本解釋」）→ 該段含開始即歸 reasoning
       - 若 petitioner 以「壹、…」起、遇「貳、…」→ hard boundary（601 型）
       - 遇 reasoning opener → 該段起歸 reasoning
       - 符合 continuation（關係機關陳述、本件聲請人主張續、enumeration、程序 meta）
         → 繼續納入 petitioner
       - 其他 → 保守只收首段為 petitioner、其餘歸 reasoning

    回傳 (petitioner_paras, remaining_paras)。
    """
    if not paragraphs:
        return [], paragraphs

    if not any(p.search(paragraphs[0]) for p in _PETITIONER_OPENERS):
        return [], paragraphs

    # Stateful flag：petitioner 是否以「壹、」起、用於識別 601 型 貳/參 boundary
    petitioner_starts_dazheng = bool(_DAZHENG_OPENER_RE.match(paragraphs[0]))

    petitioner = [paragraphs[0]]

    # Era D/E「壹、受理程序」mode（釋字 601 型）：壹 section 可能含多段 substantive
    # 討論（不符合 continuation pattern），全視為 petitioner until 貳/參/肆 transition
    # 或明確 hard boundary。這跟一般 mode 的 continuation-based 判斷分開處理、
    # 避免「法官就其依法受理之案件…」這類中段段落被誤判為 reasoning。
    if petitioner_starts_dazheng:
        for idx, para in enumerate(paragraphs[1:], start=1):
            if any(p.search(para) for p in _COURT_REASONING_HARD_BOUNDARIES):
                petitioner.append(para)
                return petitioner, paragraphs[idx + 1:]
            if _DAZHENG_TRANSITION_RE.match(para):
                return petitioner, paragraphs[idx:]
            petitioner.append(para)
        return petitioner, []

    # 一般 mode：用 continuation pattern 判斷
    for idx, para in enumerate(paragraphs[1:], start=1):
        # Hard boundary：該段含「理由如下」等、該段仍屬 petitioner（meta 宣告）、
        # 後續才是 reasoning
        if any(p.search(para) for p in _COURT_REASONING_HARD_BOUNDARIES):
            petitioner.append(para)
            return petitioner, paragraphs[idx + 1:]

        # Reasoning opener → 該段起歸 reasoning
        if any(p.search(para) for p in _COURT_REASONING_OPENERS):
            return petitioner, paragraphs[idx:]

        # Petitioner 延續（關係機關陳述 / 程序 meta / enumeration）→ 繼續納入
        if any(p.search(para) for p in _PETITIONER_CONTINUATIONS):
            petitioner.append(para)
            continue

        # Petitioner opener 再次出現 → 延續（少見）
        if any(p.search(para) for p in _PETITIONER_OPENERS):
            petitioner.append(para)
            continue

        # 保守：只收首段
        return petitioner, paragraphs[idx:]

    return petitioner, []


# ─── 結論段（後置段，位於 court_reasoning 之後 / signatures 之前）─────
_CONCLUSION_OPENERS = [
    re.compile(r"^\s*(據上論結|綜上(?:所述)?|依上說明|依上所述)"),
    re.compile(r"^\s*(應|自應|從而|是|故).{0,10}?(?:認|不能|不得|合乎|違反|無違)"),  # Era A/B 常見短結論
]


def _split_conclusion(paragraphs: list[str]) -> tuple[list[str], list[str]]:
    """從尾往前找第一個 conclusion opener 的段、之後都歸 conclusion。

    只認尾端 ≤ 2 段為結論候選（避免把中間段落誤認）。
    Edge case：找不到明顯 conclusion opener → conclusion 空、整段歸 reasoning。
    """
    if not paragraphs:
        return paragraphs, []

    # 只掃尾 2 段
    tail_limit = max(0, len(paragraphs) - 2)
    for i in range(len(paragraphs) - 1, tail_limit - 1, -1):
        if any(p.search(paragraphs[i]) for p in _CONCLUSION_OPENERS):
            # 該段起所有後續段（含該段）歸 conclusion
            return paragraphs[:i], paragraphs[i:]

    return paragraphs, []


# ─── Court reasoning sub-section header（Era D/E 特化）────
# 「一、系爭規定一及二無違憲法第23條法律保留原則」這種 numbered sub-header
_SUBHEADER_RE = re.compile(
    r"^[\s　]*(?P<num>[一二三四五六七八九十]{1,3})、(?P<title>[^。\n]{5,80})$"
)


def _split_reasoning_subsections(paragraphs: list[str]) -> list[dict]:
    """若 reasoning 包含「一、XXX」格式 sub-header、切成多個 subsection。
    否則整段打包成單一 subsection。

    Sub-section 格式：{"title": str, "text": str}
    """
    if not paragraphs:
        return []

    # 掃描：找出所有 sub-header index
    subheader_idx = [i for i, p in enumerate(paragraphs) if _SUBHEADER_RE.match(p)]

    if not subheader_idx:
        # 沒 sub-header → 整段 reasoning 為單一 subsection（title 空）
        return [{"title": "", "text": "\n\n".join(paragraphs)}]

    subsections = []

    # 第一個 sub-header 之前的段是「總論」、title 空
    if subheader_idx[0] > 0:
        subsections.append({
            "title": "",
            "text": "\n\n".join(paragraphs[:subheader_idx[0]]),
        })

    for i, hdr_i in enumerate(subheader_idx):
        next_hdr = subheader_idx[i + 1] if i + 1 < len(subheader_idx) else len(paragraphs)
        m = _SUBHEADER_RE.match(paragraphs[hdr_i])
        title = m.group("title").strip() if m else ""
        body = "\n\n".join(paragraphs[hdr_i + 1:next_hdr])
        subsections.append({"title": title, "text": body})

    return subsections


# ─── Public API ─────────────────────────────────
def parse_interpretation(
    cid: int,
    main_text: str | None,
    reasoning: str | None,
    issues: str | None = None,
) -> dict[str, Any]:
    """Top-level parser — cid-dispatched。

    Args:
        cid: 釋字號（1-813）
        main_text: 解釋文原文
        reasoning: 理由書原文（可能為空，Era A 79 則皆無）
        issues: 解釋爭點（可選、直接原樣放 result["issues"]）

    Returns:
        統一 schema dict（見 module docstring）
    """
    era = _era_of(cid)
    summary = _clean(main_text or "")
    reasoning_text = _clean(reasoning or "")

    result: dict[str, Any] = {
        "summary": summary,
        "issues": _clean(issues or ""),
        "sections": [],
        "era": era,
    }

    # Era A 分支：無理由書
    if not reasoning_text:
        # 整個 summary 視為 court_reasoning（Era A 的主文已融合見解）
        # 不重複列入 summary；section 只列 signatures（若主文尾端有）
        # 主文通常無署名，故此分支 sections 通常為空。
        return result

    paragraphs = _split_paragraphs(reasoning_text)

    # Stage 1：剝尾段署名
    body, signatures = _split_signatures(paragraphs)

    # Stage 2：從頭認聲請段
    petitioner_paras, rest = _split_petitioner_claims(body)

    # Stage 3：從尾端認結論段
    reasoning_paras, conclusion_paras = _split_conclusion(rest)

    # Stage 4：Era D/E 切 court_reasoning sub-section
    subsections = _split_reasoning_subsections(reasoning_paras) if era in ("D", "E") else [
        {"title": "", "text": "\n\n".join(reasoning_paras)}
    ]

    # 後處理：601 型 壹、受理程序 mode 把 petitioner 再切出 procedural_ruling
    # （法院對程序要件的認定論述、語意屬本院認定、應與 court_reasoning 一起被 LLM 參考）
    petitioner_paras, procedural_paras = _split_procedural_ruling(petitioner_paras)

    # 組 sections（空段不輸出）
    if petitioner_paras:
        result["sections"].append({
            "role": "petitioner_claim",
            "title": "聲請意旨",
            "text": "\n\n".join(petitioner_paras),
        })

    if procedural_paras:
        result["sections"].append({
            "role": "procedural_ruling",
            "title": "受理程序",
            "text": "\n\n".join(procedural_paras),
        })

    if reasoning_paras:
        section: dict[str, Any] = {
            "role": "court_reasoning",
            "title": "本院見解",
            "text": "\n\n".join(reasoning_paras),
        }
        # 只有多 subsection 或有 title 的 subsection 時才加 subsections field
        if len(subsections) > 1 or (subsections and subsections[0]["title"]):
            section["subsections"] = subsections
        result["sections"].append(section)

    if conclusion_paras:
        result["sections"].append({
            "role": "conclusion",
            "title": "結論",
            "text": "\n\n".join(conclusion_paras),
        })

    if signatures:
        result["sections"].append({
            "role": "signatures",
            "title": "大法官署名",
            "text": signatures,
        })

    return result
