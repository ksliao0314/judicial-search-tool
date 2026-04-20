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


class TestChargeImpositionLeak:
    """刑事判決「論罪/罪數/量刑」誤選偵測。"""

    def test_charge_application_detected(self):
        """典型論罪語句：「核被告X所為，均係犯刑法第Y條」"""
        kinds = detect_anomaly_kinds(
            excerpt='核被告C○○（如附表一共9罪、附表二共5罪）所為，均係犯刑法第339條第1項詐取財罪',
            score=7, main_text=None,
        )
        assert 'charge_imposition_leak' in kinds

    def test_applicable_law_detected(self):
        """「應依刑法第X條論處」"""
        kinds = detect_anomaly_kinds(
            excerpt='是被告所為，應依刑法第339條第1項論處。',
            score=6, main_text=None,
        )
        assert 'charge_imposition_leak' in kinds

    def test_one_crime_rule_detected(self):
        """罪數認定：「應以一罪論」「數罪併罰」「從一重論處」"""
        for ex in [
            '應以一罪論',
            '被告所犯數罪，應依刑法第51條數罪併罰',
            '應從一重論處詐欺取財罪',
            '為想像競合犯',
        ]:
            kinds = detect_anomaly_kinds(excerpt=ex, score=5, main_text=None)
            assert 'charge_imposition_leak' in kinds, f'失誤：{ex!r} 沒被偵測'

    def test_sentencing_detected(self):
        """量刑：「爰以行為人之責任」「爰審酌被告」「量處有期徒刑」"""
        for ex in [
            '爰以行為人之責任為基礎，審酌被告之犯罪動機',
            '爰審酌被告犯罪後坦承犯行',
            '爰依刑法第57條各款所列情狀',
            '量處有期徒刑一年',
        ]:
            kinds = detect_anomaly_kinds(excerpt=ex, score=7, main_text=None)
            assert 'charge_imposition_leak' in kinds, f'失誤：{ex!r} 沒被偵測'

    def test_element_reasoning_not_flagged(self):
        """構成要件認定段不該被誤判（即使含「犯罪」「被告」字樣）。"""
        ok_cases = [
            '本院認為被告具有為自己不法所有之意圖，主觀要件成立',
            '查被告施用詐術，使被害人陷於錯誤而交付財物，客觀要件該當',
            '本院審酌全案事證，被告辯解不足採信',  # 「審酌」但不是「爰審酌」
        ]
        for ex in ok_cases:
            kinds = detect_anomaly_kinds(excerpt=ex, score=8, main_text=None)
            assert 'charge_imposition_leak' not in kinds, f'誤判：{ex!r}'

    def test_empty_excerpt_no_flag(self):
        assert 'charge_imposition_leak' not in detect_anomaly_kinds(
            excerpt='', score=0, main_text=None,
        )
