"""Phase 4 결정론적 평가자 (LLM 미사용). 같은 입력이면 항상 같은 점수가 나온다.

심볼 이름은 "끝 이름"(마지막 '.' 뒤)으로 비교하는 것이 기본이다. 답은 "send" 처럼 짧게,
정답은 "Session.send" 처럼 길게 쓰는 일이 많기 때문이다 (기록 13 관찰 2).
"""

# 끝 이름만으로는 어느 심볼인지 알 수 없는 흔한 이름. 3글자 이하도 흔한 이름으로 본다.
EXPECT_COMMON = {
    "send",
    "run",
    "main",
    "get",
    "post",
    "request",
    "invoke",
    "call",
    "__call__",
    "__init__",
    "print",
    "render",
    "process",
    "handle",
    "execute",
}


def _tail(name: str) -> str:
    """끝 이름. "A.b.c" → "c", "Class.method" → "method"."""
    return name.rsplit(".", 1)[-1]


def _is_common(name: str) -> bool:
    """끝 이름이 흔한 이름인가 (3글자 이하 또는 EXPECT_COMMON)."""
    tail = _tail(name)
    return len(tail) <= 3 or tail in EXPECT_COMMON


def _same_module(a: str, b: str) -> bool:
    """모듈명을 끝 두 조각(짧은 쪽이 한 조각이면 한 조각)으로 비교해 접두어 차이를 흡수한다."""
    pa, pb = a.split("."), b.split(".")
    n = min(len(pa), len(pb), 2)
    return pa[-n:] == pb[-n:]


def entrypoint_recall(predicted: list[dict], gold: list[dict]) -> float:
    """정답 진입점 중 예측에 포함된 비율 (0.0~1.0). gold 가 비면 0.0.

    일치 = 같은 file 이고 심볼 끝 이름이 같음 ("send" == "Session.send").
    각 dict 는 {"file", "symbol"} 키를 쓴다 (ReadingGuide.entrypoints 의 CodeRef 와 같은 이름).
    """
    if not gold:
        return 0.0
    found = {(p["file"], _tail(p.get("symbol") or "")) for p in predicted}
    hits = sum((g["file"], _tail(g.get("symbol") or "")) in found for g in gold)
    return hits / len(gold)


def core_module_precision_at_k(predicted: list[str], gold: list[str], k: int = 5) -> float:
    """예측 상위 k 모듈 중 정답에 있는 비율 (분모는 k). 예측이 k 개보다 적으면 그만큼 손해.

    모듈명은 끝 두 조각 비교로 접두어 차이를 흡수한다 ("src.requests.sessions" ==
    "requests.sessions"). 예측의 중복은 한 번만 센다.
    """
    top = list(dict.fromkeys(predicted))[:k]
    hits = sum(any(_same_module(p, g) for g in gold) for p in top)
    return hits / k


def reached(
    answer_refs: list[tuple[str, str | None]],
    answer_text: str,
    expect: str,
    expect_file: str | None = None,
) -> bool:
    """답이 기대 도달 심볼 expect 까지 갔는가.

    시그니처: answer_refs = 답의 인용 [(file, symbol 또는 None), ...] (인용 순서),
    answer_text = 답의 제목·설명을 이은 글, expect = 기대 심볼("PreparedRequest.prepare_body"),
    expect_file = expect 가 정의된 파일(모르면 None).

    True 조건 (하나라도):
    1. answer_text 에 expect 전체가 들어 있다.
    2. 어떤 인용 symbol 의 끝 이름 == expect 의 끝 이름 ("prepare_body" 도 인정).
       단 끝 이름이 흔한 이름(_is_common)이면 그것만으로는 부족하고, 추가로
       (a) expect_file 이 주어졌고 인용 file 이 같거나,
       (b) 인용 symbol 의 끝 두 조각이 expect 와 같아야 한다 ("Session.send" ↔ "Session.send",
           "HTTPAdapter.send" 기대에 "BaseAdapter.send" 는 불인정).
    """
    if expect in answer_text:
        return True
    tail = _tail(expect)
    for file, symbol in answer_refs:
        if not symbol or _tail(symbol) != tail:
            continue
        if not _is_common(expect):
            return True
        same_file = expect_file is not None and file == expect_file
        qualified = "." in symbol and _same_module(symbol, expect)  # 맨 "send" 는 불인정
        if same_file or qualified:
            return True
    return False


def _lcs(a: list[str], b: list[str]) -> int:
    """최장 공통 부분수열(순서는 지키되 연속일 필요 없음) 길이. 동적 계획법 O(len(a)·len(b))."""
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def call_chain_accuracy(predicted_chain: list[str], expect_chain: list[str]) -> float:
    """호출 체인 순서 일치도 (0.0~1.0) = LCS(끝 이름 정규화) 길이 / len(expect_chain).

    예: 예측 [get, request, send] vs 기대 [get, request, Session.request, Session.send]
    → 끝 이름 [get, request, request, send] 와의 LCS 3 → 3/4. 예측이나 기대가 비면 0.0.
    """
    if not predicted_chain or not expect_chain:
        return 0.0
    pred = [_tail(s) for s in predicted_chain]
    gold = [_tail(s) for s in expect_chain]
    return _lcs(pred, gold) / len(gold)


def predicted_chain_from_refs(refs: list[tuple[str, str | None]]) -> list[str]:
    """인용 순서대로 symbol 을 (끝 이름 정규화 없이) 나열하고 연속 중복을 뺀다.

    symbol 이 없는 인용은 건너뛴다.
    """
    chain: list[str] = []
    for _file, symbol in refs:
        if symbol and (not chain or chain[-1] != symbol):
            chain.append(symbol)
    return chain


def issue_module_precision_at_k(
    predicted_modules: list[str], gold_modules: list[str], k: int = 3
) -> float:
    """Issue Matcher: 예측 상위 k 모듈 중 정답 모듈 비율. 분모는 min(k, 정답 수).

    이슈 정답 모듈은 대개 1~2개라 core_module_precision_at_k 처럼 분모를 k 로 두면
    정답이 1개인 이슈의 만점이 1/k 가 된다. 그래서 분모를 정답 수에 맞춘다
    (정답 1개를 상위 3개 안에 맞히면 1.0). 비교 규칙(끝 두 조각, 중복 한 번)은 같다.
    """
    if not gold_modules or k <= 0:
        return 0.0
    denom = min(k, len(gold_modules))
    hits = core_module_precision_at_k(predicted_modules, gold_modules, k) * k
    return min(1.0, round(hits) / denom)


def issue_module_hit_at_k(
    predicted_modules: list[str], gold_modules: list[str], k: int = 3
) -> bool:
    """Issue Matcher: 상위 k 예측 모듈 중 정답이 하나라도 있는가 (가장 느슨한 지표)."""
    return issue_module_precision_at_k(predicted_modules, gold_modules, k) > 0


def issue_symbol_hit(predicted_symbols: list[str], gold_symbols: list[str]) -> bool:
    """Issue Matcher: 예측 심볼 중 하나라도 정답 심볼과 끝 이름이 같은가.

    "Session.request" 정답에 "request" 예측도 맞은 것으로 친다(_tail 비교).
    """
    gold_tails = {_tail(g) for g in gold_symbols}
    return any(_tail(p) in gold_tails for p in predicted_symbols)


def impact_recall(predicted_symbols: list[str], gold_symbols: list[str]) -> float:
    """PR Coach: 정답 영향 심볼 중 예측에 들어 있는 비율 (0.0~1.0). 분모는 정답 수.

    끝 이름으로 비교한다("Session.send" 정답에 "send" 예측도 찾은 것으로 친다).
    정답의 중복은 한 번만 센다. gold 가 비면 0.0.
    """
    gold_tails = {_tail(g) for g in gold_symbols}
    if not gold_tails:
        return 0.0
    pred_tails = {_tail(p) for p in predicted_symbols}
    return len(gold_tails & pred_tails) / len(gold_tails)


def test_file_hit(predicted_files: list[str], gold_files: list[str]) -> bool:
    """PR Coach: 예측 테스트 파일 중 하나라도 정답과 파일명(경로 끝)이 같은가.

    "tests/test_models.py" 정답에 "test_models.py" 나 "repo/tests/test_models.py" 예측도 맞은
    것으로 친다. 디렉터리 차이(체크아웃 위치·접두어)를 흡수하려는 느슨한 비교다.
    """
    gold_names = {g.replace("\\", "/").rsplit("/", 1)[-1] for g in gold_files}
    return any(p.replace("\\", "/").rsplit("/", 1)[-1] in gold_names for p in predicted_files)
