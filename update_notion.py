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
P_TITLE, P_PRICE, P_DATE = "지수/종목", "가격/포인트", "기준일"
P_YEAR, P_DAY, P_WEEK = "연간 변동", "일간 변동", "주간 변동"
GROUP_PROP = None          # 그룹 기준 속성이 있으면 ("속성명", "select")
PERCENT_FORMAT = True      # 변동 속성이 '퍼센트' 형식이면 True, 일반 숫자면 False
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
    dict(db="주식시장", name="S&P 500", src=("yf", "^GSPC")),
    # 환율 (USD/X = 1달러당 X)
    dict(db="환율", name="달러인덱스", src=("yf", "DX-Y.NYB")),
    dict(db="환율", name="USD/EUR", src=("yf", "EURUSD=X"), invert=True),
    dict(db="환율", name="USD/JPY", src=("yf", "JPY=X")),
    dict(db="환율", name="USD/KRW", src=("yf", "KRW=X")),
    # 시장 심리 지수
    dict(db="시장 심리 지수", name="Fear & Greed", src=("fng",)),
    dict(db="시장 심리 지수", name="VIX", src=("yf", "^VIX")),
    # M2
    dict(db="M2", name="미국 M2", src=("fred", "WM2NS"), freq="W"),
    dict(db="M2", name="역레포 잔액", src=("fred", "RRPONTSYD")),
    dict(db="M2", name="한국 M2", src=("ecos", "101Y004", "M", ["M2", "평잔", "원계열"]), freq="M"),
    # 원자재
    dict(db="원자재", name="금", src=("yf", "GC=F")),
    dict(db="원자재", name="원유(WTI)", src=("yf", "CL=F")),
    dict(db="원자재", name="은", src=("yf", "SI=F")),
    # 국채금리 (%)
    dict(db="국채금리", name="미국 3년물", src=("tsy", "3 Yr"), kind="diff"),
    dict(db="국채금리", name="미국 10년물", src=("tsy", "10 Yr"), kind="diff"),
    dict(db="국채금리", name="미국 30년물", src=("tsy", "30 Yr"), kind="diff"),
    dict(db="국채금리", name="한국 3년물", src=("ecos", "817Y002", "D", ["국고채", "3년"]), kind="diff"),
    dict(db="국채금리", name="한국 10년물", src=("ecos", "817Y002", "D", ["국고채", "10년"]), kind="diff"),
    # 기준금리 (%)
    dict(db="기준금리", name="미국", src=("fred", "DFEDTARU"), kind="diff"),
    dict(db="기준금리", name="일본", src=("fred", "IRSTCB01JPM156N"), kind="diff", freq="M"),
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
    rows = requests.get(url, timeout=30).json()["StatisticSearch"]["row"]
    idx = [pd.to_datetime(r["TIME"], format="%Y%m%d" if len(r["TIME"]) == 8 else "%Y%m") for r in rows]
    return pd.Series([float(r["DATA_VALUE"]) for r in rows], index=idx)


def s_fng():
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{START.date().isoformat()}"
    data = requests.get(url, headers=UA, timeout=30).json()["fear_and_greed_historical"]["data"]
    s = pd.Series([d["y"] for d in data], index=pd.to_datetime([d["x"] for d in data], unit="ms").normalize())
    return s[~s.index.duplicated(keep="last")]


def load(item):
    kind, *a = item["src"]
    s = {"yf": s_yf, "fred": s_fred, "tsy": s_tsy, "ecos": s_ecos, "fng": s_fng}[kind](*a)
    return 1 / s if item.get("invert") else s


# ── 변동률 계산 ──────────────────────────────────
def compute(item, s):
    s = s[s.index <= AS_OF].dropna().sort_index()
    last_date, last = s.index[-1], float(s.iloc[-1])
    kind, freq = item.get("kind", "pct"), item.get("freq", "D")

    def past(days):
        sub = s[: last_date - timedelta(days=days)]
        return float(sub.iloc[-1]) if len(sub) else None

    def chg(p):
        if p is None or (kind == "pct" and p == 0):
            return None
        v = (last / p - 1) if kind == "pct" else (last - p) / 100   # 금리는 %p
        return round(v if PERCENT_FORMAT else v * 100, 4)

    return {
        "price": round(last, 4),
        "day": chg(float(s.iloc[-2])) if freq == "D" and len(s) > 1 else None,
        "week": chg(past(7)) if freq in ("D", "W") else None,
        "year": chg(past(365)),
    }


# ── 노션 ─────────────────────────────────────────
H = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28",
     "Content-Type": "application/json"}


def upsert(item, d):
    db, name, date = DB_IDS[item["db"]], item["name"], AS_OF.date().isoformat()
    props = {
        P_TITLE: {"title": [{"text": {"content": name}}]},
        P_PRICE: {"number": d["price"]},
        P_DATE: {"date": {"start": date}},
        P_YEAR: {"number": d["year"]},
        P_DAY: {"number": d["day"]},
        P_WEEK: {"number": d["week"]},
    }
    if GROUP_PROP:
        props[GROUP_PROP[0]] = {GROUP_PROP[1]: {"name": name}}
    q = requests.post(
        f"https://api.notion.com/v1/databases/{db}/query", headers=H, timeout=30,
        json={"filter": {"and": [
            {"property": P_TITLE, "title": {"equals": name}},
            {"property": P_DATE, "date": {"equals": date}}]}},
    )
    q.raise_for_status()
    found = q.json()["results"]
    if found:   # 같은 주 재실행 시 중복 생성 방지
        r = requests.patch(f"https://api.notion.com/v1/pages/{found[0]['id']}",
                           headers=H, json={"properties": props}, timeout=30)
    else:
        r = requests.post("https://api.notion.com/v1/pages", headers=H, timeout=30,
                          json={"parent": {"database_id": db}, "properties": props})
    r.raise_for_status()


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
