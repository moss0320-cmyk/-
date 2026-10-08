"""매주 화요일 기준 경제지표를 수집해 노션 DB에 행으로 추가(이미 있으면 갱신).

pip install yfinance requests pandas
환경변수:
  NOTION_TOKEN    노션 통합 토큰
  NOTION_DB_IDS   {"주식시장":"DB_ID","환율":"DB_ID", ...} (JSON 문자열)
  FRED_API_KEY    https://fred.stlouisfed.org/docs/api/api_key.html (무료)
  ECOS_API_KEY    https://ecos.bok.or.kr/api (무료, 한국은행)
  AS_OF           (선택) 기준일 직접 지정, 예: 2026-09-22
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DB_IDS = json.loads(os.environ["NOTION_DB_IDS"])
FRED_KEY = os.environ.get("FRED_API_KEY", "")
ECOS_KEY = os.environ.get("ECOS_API_KEY", "")
UA = {"User-Agent": "Mozilla/5.0"}

# ── 노션 속성 이름 (DB와 다르면 수정) ─────────────────
P_DATE = "기준일"
# DB마다 열 이름이 다르면 앞에서부터 먼저 발견되는 이름을 사용
PROP_NAMES = {
    "price": ["가격/포인트", "금리", "포인트"],
    "year": ["연간 변동"],
    "day": ["일간 변동"],
    "week": ["주간 변동"],
    "last_change": ["변동폭"],   # 기준금리: 직전 금리 변경 폭(%p)
}
GROUP_PROP = None          # 그룹 기준 속성이 있으면 ("속성명", "select")
# (숫자 속성의 '퍼센트/숫자' 형식은 노션 DB에서 자동으로 읽어 맞춥니다)
# ───────────────────────────────────────────────────

# 기준일: 가장 최근 화요일(KST). 수요일 아침에 돌리면 '어제(화)' 마감 기준이 된다.
_today = datetime.now(ZoneInfo("Asia/Seoul")).date()
AS_OF = pd.Timestamp(os.environ.get("AS_OF") or _today - timedelta(days=(_today.weekday() - 1) % 7))

# kind: pct = 등락률(%), diff = 금리 차이(%p)
# freq: D 일별 / W 주별 / M 월별 (낮은 빈도는 일간·주간 변동을 비워둠)
# invert: 역수 (EUR/USD → USD/EUR)
ITEMS = [
    # 주식시장
    dict(db="주식시장", name="KOSPI", src=("yf", "^KS11")),
    dict(db="주식시장", name="Nasdaq", src=("yf", "^IXIC")),
    dict(db="주식시장", name="S&P500", src=("yf", "^GSPC")),
    # 환율 (USD/X = 1달러당 X)
    dict(db="환율", name="달러 인덱스", src=("yf", "DX-Y.NYB")),
    dict(db="환율", name="USD/EUR", src=("yf", "EURUSD=X"), invert=True),
    dict(db="환율", name="USD/JPY", src=("yf", "JPY=X")),
    dict(db="환율", name="USD/KRW", src=("yf", "KRW=X")),
    # 시장 심리 지수
    # Fear & Greed는 CNN이 자동 접근을 막아(418) 제외 → 노션에 수동 입력
    dict(db="시장 심리 지수", name="VIX(뉴욕주식시작 변동성지수)", src=("yf", "^VIX")),
    # M2
    dict(db="M2", name="미국 M2", src=("fred", "M2SL"), freq="M"),
    dict(db="M2", name="역레포 잔액", src=("fred", "RRPONTSYD")),
    dict(db="M2", name="한국 M2", src=("ecos_find", ["M2"], "M", ["M2", "평잔", "원계열"]), freq="M"),
    # 원자재
    dict(db="원자재", name="금 선물", src=("yf", "GC=F")),
    dict(db="원자재", name="원유(WTI 선물)", src=("yf", "CL=F")),
    dict(db="원자재", name="은", src=("yf", "SI=F")),
    # 국채금리 (%)
    dict(db="국채금리", name="미국 3년물", src=("tsy", "3 Yr"), kind="diff"),
    dict(db="국채금리", name="미국 10년물", src=("tsy", "10 Yr"), kind="diff"),
    dict(db="국채금리", name="미국 30년물", src=("tsy", "30 Yr"), kind="diff"),
    dict(db="국채금리", name="한국 3년물", src=("ecos", "817Y002", "D", ["국고채", "3년"]), kind="diff"),
    dict(db="국채금리", name="한국 10년물", src=("ecos", "817Y002", "D", ["국고채", "10년"]), kind="diff"),
    dict(db="국채금리", name="한국 30년물", src=("ecos", "817Y002", "D", ["국고채", "30년"]), kind="diff"),
    # 기준금리 (%)
    dict(db="기준금리", name="미국", src=("fred", "DFEDTARU"), kind="diff"),
    dict(db="기준금리", name="일본", src=("fred_any", ["IRSTCB01JPM156N", "INTDSRJPM193N", "IRSTCI01JPM156N", "IR3TIB01JPM156N"]),
         kind="diff", freq="M"),
    dict(db="기준금리", name="한국", src=("ecos", "722Y001", "D", ["기준금리"]), kind="diff"),
]

START = AS_OF - timedelta(days=800)


# ── 데이터 수집: 각 함수는 날짜 인덱스 pd.Series 반환 ──
def s_yf(ticker):
    s = yf.Ticker(ticker).history(period="3y")["Close"].dropna()
    s.index = s.index.tz_localize(None).normalize()
    return s


def s_fred(series_id):
    r = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params=dict(series_id=series_id, api_key=FRED_KEY, file_type="json",
                    observation_start=START.date().isoformat()),
        timeout=30,
    )
    r.raise_for_status()
    obs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] != "."]
    return pd.Series([v for _, v in obs], index=pd.to_datetime([d for d, _ in obs]))


_tsy = None


def s_tsy(col):
    global _tsy
    if _tsy is None:
        frames = []
        for y in (AS_OF.year - 1, AS_OF.year):
            url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                   f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
                   f"&field_tdr_date_value={y}&page&_format=csv")
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            frames.append(pd.read_csv(io.StringIO(r.text)))
        df = pd.concat(frames)
        df["Date"] = pd.to_datetime(df["Date"])
        _tsy = df.set_index("Date").sort_index()
    return _tsy[col].dropna()


def s_ecos(stat, cycle, keywords):
    base = f"https://ecos.bok.or.kr/api/%s/{ECOS_KEY}/json/kr/1/100000"
    items = requests.get(f"{base % 'StatisticItemList'}/{stat}", timeout=30).json()["StatisticItemList"]["row"]
    hit = [i for i in items if all(k in i["ITEM_NAME"] for k in keywords)]
    if not hit:
        names = [i["ITEM_NAME"] for i in items][:30]
        raise ValueError(f"ECOS 항목 못 찾음 {stat} {keywords}. 후보: {names}")
    item = hit[0]
    print(f"   (ECOS 매칭: {item['ITEM_NAME']} / {item['ITEM_CODE']})")
    fmt = "%Y%m%d" if cycle == "D" else "%Y%m"
    url = f"{base % 'StatisticSearch'}/{stat}/{cycle}/{START.strftime(fmt)}/{AS_OF.strftime(fmt)}/{item['ITEM_CODE']}"
    resp = requests.get(url, timeout=30).json()
    if "StatisticSearch" not in resp:
        raise ValueError(f"ECOS 응답 오류: {resp}")
    rows = resp["StatisticSearch"]["row"]
    idx = [pd.to_datetime(r["TIME"], format="%Y%m%d" if len(r["TIME"]) == 8 else "%Y%m") for r in rows]
    return pd.Series([float(r["DATA_VALUE"]) for r in rows], index=idx)


def s_fred_any(ids):
    """여러 FRED 시리즈 중 최근 데이터가 있는 첫 번째를 사용."""
    for i in ids:
        try:
            s = s_fred(i)
            s = s[s.index <= AS_OF]
            if len(s) and (AS_OF - s.index[-1]).days <= 100:
                print(f"   (FRED 사용: {i})")
                return s
            print(f"   (FRED {i}: 최근 데이터 없음, 다음 시도)")
        except Exception as e:
            print(f"   (FRED {i}: 실패 {e})")
    raise ValueError(f"최근 데이터가 있는 FRED 시리즈 없음: {ids}")


def s_ecos_find(table_kw, cycle, item_kw):
    """ECOS에서 이름으로 통계표를 찾아, 최근 데이터가 있는 첫 표를 사용."""
    base = f"https://ecos.bok.or.kr/api/%s/{ECOS_KEY}/json/kr/1/10000"
    resp = requests.get(f"{base % 'StatisticTableList'}/", timeout=30).json()
    if "StatisticTableList" not in resp:
        raise ValueError(f"ECOS 표 목록 오류: {resp}")
    tables = [t for t in resp["StatisticTableList"]["row"]
              if t.get("CYCLE") == cycle and t.get("SRCH_YN") != "N"
              and all(k in t["STAT_NAME"] for k in table_kw)]
    tried = []
    for t in tables[:15]:
        try:
            s = s_ecos(t["STAT_CODE"], cycle, item_kw)
            if len(s) and (AS_OF - s.index.max()).days <= 100:
                print(f"   (ECOS 표 사용: {t['STAT_CODE']} {t['STAT_NAME']})")
                return s
        except Exception:
            pass
        tried.append(f"{t['STAT_CODE']} {t['STAT_NAME']}")
    raise ValueError(f"ECOS에서 최근 데이터를 찾지 못함. 시도한 표: {tried}")


def s_fng():
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{START.date().isoformat()}"
    hdr = {**UA, "Referer": "https://www.cnn.com/markets/fear-and-greed",
           "Origin": "https://www.cnn.com"}
    r = requests.get(url, headers=hdr, timeout=30)
    if r.status_code != 200:
        raise ValueError(f"CNN 응답 {r.status_code} (서버에서 차단됐을 수 있음)")
    data = r.json()["fear_and_greed_historical"]["data"]
    s = pd.Series([d["y"] for d in data], index=pd.to_datetime([d["x"] for d in data], unit="ms").normalize())
    return s[~s.index.duplicated(keep="last")]


def load(item):
    kind, *a = item["src"]
    s = {"yf": s_yf, "fred": s_fred, "fred_any": s_fred_any, "tsy": s_tsy,
         "ecos": s_ecos, "ecos_find": s_ecos_find, "fng": s_fng}[kind](*a)
    return 1 / s if item.get("invert") else s


# ── 변동률 계산 ──────────────────────────────────
def compute(item, s):
    s = s[s.index <= AS_OF].dropna().sort_index()
    if s.empty:
        raise ValueError("데이터 없음")
    last_date, last = s.index[-1], float(s.iloc[-1])
    kind, freq = item.get("kind", "pct"), item.get("freq", "D")
    if (AS_OF - last_date).days > {"D": 10, "W": 25, "M": 100}[freq]:
        raise ValueError(f"데이터가 오래됨 (최신 {last_date.date()})")

    def past(days):
        sub = s[: last_date - timedelta(days=days)]
        return float(sub.iloc[-1]) if len(sub) else None

    def chg(p):
        if p is None or (kind == "pct" and p == 0):
            return None
        return round((last / p - 1) * 100 if kind == "pct" else last - p, 4)   # %, 금리는 %p

    other = s[s != last]
    return {
        "last_change": round(last - float(other.iloc[-1]), 4) if len(other) else 0.0,
        "price": round(last, 4),
        "day": chg(float(s.iloc[-2])) if freq == "D" and len(s) > 1 else None,
        "week": chg(past(7)) if freq in ("D", "W") else None,
        "year": chg(past(365)),
    }


# ── 노션 ─────────────────────────────────────────
H = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28",
     "Content-Type": "application/json"}


def ok(r):
    if not r.ok:
        raise RuntimeError(f"{r.status_code} {r.text[:300]}")


_schema, _warned = {}, set()


def schema(db):
    if db not in _schema:
        r = requests.get(f"https://api.notion.com/v1/databases/{db}", headers=H, timeout=30)
        ok(r)
        _schema[db] = r.json()["properties"]
    return _schema[db]


def pick(sch, key):
    """이 DB에서 실제로 존재하는 속성 이름 찾기."""
    return next((n for n in PROP_NAMES[key] if n in sch), None)


def build_value(sch_prop, val, kind_label, is_rate_level=False):
    """속성 유형(숫자/텍스트/선택)에 맞춰 값을 변환."""
    t = sch_prop["type"]
    if t == "number":
        if val is not None and (sch_prop["number"].get("format") == "percent") and (kind_label or is_rate_level):
            val = val / 100            # 퍼센트 형식 속성은 0.0455 = 4.55%
        return {"number": val}
    label = None if val is None else (
        f"{val:,.2f}%" if is_rate_level else
        f"{val:,.2f}" if kind_label is None else f"{val:+.2f}{kind_label}")
    if t == "rich_text":
        return {"rich_text": [{"text": {"content": label}}] if label else []}
    if t == "multi_select":
        return {"multi_select": [{"name": label}] if label else []}
    if t == "select":
        return {"select": {"name": label} if label else None}
    return None


def upsert(item, d):
    db, name, date = DB_IDS[item["db"]], item["name"], AS_OF.date().isoformat()
    sch = schema(db)
    title_prop = next(k for k, v in sch.items() if v["type"] == "title")
    if sch.get(P_DATE, {}).get("type") != "date":
        raise ValueError(f"날짜 속성 '{P_DATE}' 없음. 이 DB 속성: { {k: v['type'] for k, v in sch.items()} }")
    props = {
        title_prop: {"title": [{"text": {"content": name}}]},
        P_DATE: {"date": {"start": date}},
    }
    rate = item.get("kind") == "diff"
    unit = "%p" if rate else "%"
    for key in ("price", "year", "day", "week", "last_change"):
        if key == "last_change" and not rate:
            continue
        prop = pick(sch, key)
        if prop is None and key == "last_change":
            continue
        if prop is None:
            if (item["db"], key) not in _warned:
                _warned.add((item["db"], key))
                print(f"   ! [{item['db']}] '{key}'에 해당하는 열 없음 → 건너뜀. 이 DB 속성: "
                      f"{ {k: v['type'] for k, v in sch.items()} }")
            continue
        if key == "price":
            v = build_value(sch[prop], d["price"], "%" if rate else None, is_rate_level=rate)
        else:
            v = build_value(sch[prop], d[key], unit)
        if v is None:
            print(f"   ! [{item['db']}] 열 '{prop}' 유형({sch[prop]['type']})은 지원 안 함 → 건너뜀")
            continue
        props[prop] = v
    if GROUP_PROP and GROUP_PROP[0] in sch:
        props[GROUP_PROP[0]] = {GROUP_PROP[1]: {"name": name}}
    q = requests.post(
        f"https://api.notion.com/v1/databases/{db}/query", headers=H, timeout=30,
        json={"filter": {"and": [
            {"property": title_prop, "title": {"equals": name}},
            {"property": P_DATE, "date": {"equals": date}}]}},
    )
    ok(q)
    found = q.json()["results"]
    if found:   # 같은 주 재실행 시 중복 생성 방지
        r = requests.patch(f"https://api.notion.com/v1/pages/{found[0]['id']}",
                           headers=H, json={"properties": props}, timeout=30)
    else:
        r = requests.post("https://api.notion.com/v1/pages", headers=H, timeout=30,
                          json={"parent": {"database_id": db}, "properties": props})
    ok(r)


if __name__ == "__main__":
    print("기준일:", AS_OF.date())
    failed = []
    for it in ITEMS:
        try:
            upsert(it, compute(it, load(it)))
            print("OK  ", it["db"], it["name"])
        except Exception as e:
            failed.append(it["name"])
            print("FAIL", it["db"], it["name"], "→", e)
    if failed:
        sys.exit(f"실패 항목: {failed}")
