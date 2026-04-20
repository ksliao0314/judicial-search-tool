"""excerpt_anomaly_log.detect_anomaly_kinds 單元測試。

主要驗證 prompt 規則的 Claude 輸出是否被正確偵測為異常。不測試 async I/O。
"""
from src.utils.excerpt_anomaly_log import detect_anomaly_kinds


def test_party_claim_prefix_detected():
    # 原告主張開頭
    assert 'party_claim_prefix' in detect_anomaly_kinds(
        excerpt='原告主張略以：(一)按文資法第12條',
        score=9, main_text=None,
    )
    # 被告答辯開頭
    assert 'party_claim_prefix' in detect_anomaly_kinds(
        excerpt='被告答辯略稱：本件系爭建物',
        score=8, main_text=None,
    )
    # 含 [理由] label prefix
    assert 'party_claim_prefix' in detect_anomaly_kinds(
        excerpt='[理由] 原告起訴主張:(1)...',
        score=7, main_text=None,
    )
    # 上訴人抗辯
    assert 'party_claim_prefix' in detect_anomaly_kinds(
        excerpt='上訴人主張系爭房屋',
        score=5, main_text=None,
    )


def test_party_claim_all_procedural_variants():
    """所有程序當事人變體都該被偵測（原告、被告之外的類型）。"""
    cases = [
        '被上訴人答辯略以：被告違反程序',
        '抗告人主張原裁定違誤',
        '聲請人略以：系爭處分應予停止執行',
        '相對人答辯如下',
        '參加人主張本案與其利害有關',
        '再審原告主張原判決適用法規顯有錯誤',
        '再審被告答辯稱',
        '自訴人指稱被告涉嫌',
        '告訴人陳稱其遭詐騙',
        '反訴原告主張被告違約',
        '選定當事人主張',
    ]
    for ex in cases:
        kinds = detect_anomaly_kinds(excerpt=ex, score=7, main_text=None)
        assert 'party_claim_prefix' in kinds, f'失誤：{ex!r} 沒被偵測'


def test_party_claim_prefix_not_triggered_on_court_text():
    # 法院判斷段落開頭、不該觸發
    assert 'party_claim_prefix' not in detect_anomaly_kinds(
        excerpt='本院認為原告之主張尚屬可採',  # 雖然含「原告」「主張」、開頭是「本院」
        score=8, main_text=None,
    )
    # 法院複述當事人（text 會先過 party claim regex 只 match 開頭）
    assert 'party_claim_prefix' not in detect_anomaly_kinds(
        excerpt='查原告雖主張...惟本院認',
        score=7, main_text=None,
    )


def test_main_text_leak_detected():
    mt = "訴願決定及原處分均撤銷。訴訟費用由被告負擔。"
    assert 'main_text_leak' in detect_anomaly_kinds(
        excerpt='訴願決定及原處分均撤銷。',
        score=9, main_text=mt,
    )
    # 帶 label prefix 也要能偵測
    assert 'main_text_leak' in detect_anomaly_kinds(
        excerpt='[主文] 訴願決定及原處分均撤銷。',
        score=9, main_text=mt,
    )


def test_main_text_leak_ignores_short_text():
    # 短於 10 字、可能只是偶然重複短語、不算 leak
    assert 'main_text_leak' not in detect_anomaly_kinds(
        excerpt='撤銷',
        score=9, main_text='原處分撤銷。',
    )


def test_empty_but_scored_detected():
    assert 'empty_but_scored' in detect_anomaly_kinds(
        excerpt='', score=5, main_text=None,
    )
    assert 'empty_but_scored' in detect_anomaly_kinds(
        excerpt='   ', score=7, main_text=None,  # 只含空白
    )


def test_empty_ok_when_score_zero():
    # score=0 合理無 excerpt、不算異常
    assert 'empty_but_scored' not in detect_anomaly_kinds(
        excerpt='', score=0, main_text=None,
    )
    assert 'empty_but_scored' not in detect_anomaly_kinds(
        excerpt='', score=None, main_text=None,
    )


def test_healthy_excerpt_triggers_nothing():
    # 正常法院判斷段落、不該觸發任何 anomaly
    kinds = detect_anomaly_kinds(
        excerpt='本院認為被告所執行之處分有違比例原則，理由如下...',
        score=8,
        main_text='訴願決定及原處分均撤銷。',
    )
    assert kinds == []


def test_multiple_kinds_flagged():
    # excerpt 同時開頭主張 + 子字串是 main_text（極端 edge）
    mt = '原告起訴主張略以：系爭處分應予撤銷。'
    kinds = detect_anomaly_kinds(
        excerpt='原告起訴主張略以：系爭處分應予撤銷。',
        score=9, main_text=mt,
    )
    assert 'party_claim_prefix' in kinds
    assert 'main_text_leak' in kinds
