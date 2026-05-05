# dart_api.py
# DART 재무 데이터 + 네이버뉴스 API + 주가/벤치마크 조회
import io
import json
import zipfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
import pandas as pd
import streamlit as st

from db import upsert_financial, insert_news_bulk

# ==========================================
# 인증키 (Streamlit Secrets에서 로드)
# ==========================================
DART_API_KEY        = st.secrets["DART_API_KEY"]
NAVER_CLIENT_ID     = st.secrets["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = st.secrets["NAVER_CLIENT_SECRET"]


# ==========================================
# DART API
# ==========================================
def get_corp_code(corp_name: str) -> str | None:
    """DART 기업 고유번호 조회"""
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    res = requests.get(url, params={"crtfc_key": DART_API_KEY})
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        with z.open("CORPCODE.xml") as f:
            root = ET.parse(f).getroot()
            for node in root.findall("list"):
                if node.find("corp_name").text == corp_name:
                    return node.find("corp_code").text
    return None


def fetch_and_store_financials(corp_name: str, corp_code: str,
                               years: range, items: list[str],
                               fs_div: str) -> dict[str, list]:
    """
    DART에서 연도별 재무 항목을 수집해 DB에 저장하고,
    화면 표시용 dict도 반환.
    반환값: {item_name: [{"year": int, "amount": float, "fs_div": str}, ...]}
    """
    data_by_item: dict[str, list] = {name: [] for name in items}

    for item in items:
        for y in years:
            params = {
                "crtfc_key":  DART_API_KEY,
                "corp_code":  corp_code,
                "bsns_year":  str(y),
                "reprt_code": "11011",
            }
            res = requests.get(
                "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json",
                params=params
            ).json()
            if res.get("status") != "000":
                continue

            df = pd.DataFrame(res["list"])
            target = df[
                df["account_nm"].str.replace(" ", "").str.contains(
                    item.replace(" ", ""), na=False
                )
            ].copy()
            if fs_div != "ALL":
                target = target[target["fs_div"] == fs_div]
            if target.empty:
                continue

            row = target.iloc[0]
            amount = float(str(row["thstrm_amount"]).replace(",", "") or 0)
            div    = row["fs_div"]

            upsert_financial(corp_name, "dart", div, y, item, amount)
            data_by_item[item].append({"year": y, "amount": amount, "fs_div": div})

    return data_by_item


# ==========================================
# 네이버 뉴스 API
# ==========================================
def fetch_and_store_news(corp_name: str, s_date, e_date) -> list[dict]:
    """
    네이버 뉴스를 수집해 DB에 저장하고 리스트로 반환.
    """
    results = []
    for start in range(1, 1001, 100):
        url = (
            "https://openapi.naver.com/v1/search/news.json"
            f"?query={urllib.parse.quote(corp_name)}"
            f"&display=100&start={start}&sort=date"
        )
        req = urllib.request.Request(url)
        req.add_header("X-Naver-Client-Id",     NAVER_CLIENT_ID)
        req.add_header("X-Naver-Client-Secret",  NAVER_CLIENT_SECRET)
        try:
            with urllib.request.urlopen(req) as res:
                data = json.loads(res.read().decode("utf-8"))
                for item in data.get("items", []):
                    p_date = datetime.strptime(
                        item["pubDate"], "%a, %d %b %Y %H:%M:%S +0900"
                    ).date()
                    if s_date <= p_date <= e_date:
                        title = (item["title"]
                                 .replace("<b>", "").replace("</b>", "")
                                 .replace("&quot;", '"'))
                        results.append({
                            "작성일": p_date,
                            "제목":   title,
                            "링크":   item.get("originallink") or item.get("link"),
                        })
                    elif p_date < s_date:
                        insert_news_bulk(corp_name, results)
                        return results
        except Exception:
            break

    insert_news_bulk(corp_name, results)
    return results


# ==========================================
# 주가 조회
# ==========================================
def fetch_stock_info(corp_name: str) -> dict | None:
    """
    KRX 종목 리스트에서 기업명으로 티커를 찾고,
    yfinance로 현재 주가 정보를 조회해 dict로 반환.
    조회 실패 시 None 반환.
    """
    try:
        import FinanceDataReader as fdr
        import yfinance as yf

        df_krx = fdr.StockListing('KRX')
        exact  = df_krx[df_krx['Name'] == corp_name]
        target = exact if not exact.empty else df_krx[df_krx['Name'].str.contains(corp_name, na=False)]

        if target.empty:
            return None

        row    = target.iloc[0]
        symbol = row['Code']
        market = row['Market']
        name   = row['Name']

        # 산업/섹터 정보 추출 (컬럼명 유연하게 처리)
        industry = None
        sector   = None
        for col in df_krx.columns:
            cl = col.strip().lower()
            if cl in ("industry", "industrycode", "업종"):
                industry = str(row[col]) if pd.notna(row[col]) else None
            if cl in ("sector", "sectorcode", "섹터"):
                sector = str(row[col]) if pd.notna(row[col]) else None

        suffix = ".KS" if market == "KOSPI" else ".KQ"
        ticker = f"{symbol}{suffix}"

        stock = yf.Ticker(ticker)
        info  = stock.info

        # yfinance에서도 섹터/산업 보완
        if not sector:
            sector   = info.get("sector")
        if not industry:
            industry = info.get("industry")

        current_price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("regularMarketPreviousClose")  # 장 마감 후 fallback
        )
        if current_price is None:
            return None

        prev_close = info.get("previousClose")
        change     = current_price - prev_close if prev_close else None
        change_pct = (change / prev_close * 100) if (prev_close and change is not None) else None

        return {
            "종목명":   name,
            "종목코드": symbol,
            "시장":     market,
            "섹터":     sector,
            "업종":     industry,
            "현재가":   current_price,
            "전일종가": prev_close,
            "등락":     change,
            "등락률":   change_pct,
            "시가":     info.get("open"),
            "고가":     info.get("dayHigh"),
            "저가":     info.get("dayLow"),
            "거래량":   info.get("volume"),
            "시가총액": info.get("marketCap"),
            "ticker":   ticker,
            "조회시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    except Exception:
        return None


# ==========================================
# 업종 벤치마크
# ==========================================
def fetch_sector_benchmark(ticker: str, industry: str | None) -> dict | None:
    """
    같은 업종 상위 종목들의 평균 영업이익률·PER·PBR을 계산해 반환.
    조회 실패 시 None 반환.
    """
    try:
        import FinanceDataReader as fdr
        import yfinance as yf

        if not industry:
            return None

        df_krx = fdr.StockListing('KRX')

        # 업종 컬럼 찾기
        industry_col = None
        for col in df_krx.columns:
            if col.strip().lower() in ("industry", "industrycode", "업종"):
                industry_col = col
                break
        if industry_col is None:
            return None

        peers = df_krx[df_krx[industry_col].astype(str) == str(industry)]
        if len(peers) < 2:
            return None

        # 상위 10개 종목만 샘플링 (속도 고려)
        sample = peers.head(10)
        oper_margins, pers, pbrs = [], [], []

        for _, r in sample.iterrows():
            sym = r['Code']
            mkt = r['Market']
            sfx = ".KS" if mkt == "KOSPI" else ".KQ"
            try:
                info = yf.Ticker(f"{sym}{sfx}").info
                om  = info.get("operatingMargins")
                per = info.get("trailingPE")
                pbr = info.get("priceToBook")
                if om  is not None: oper_margins.append(om * 100)
                if per is not None: pers.append(per)
                if pbr is not None: pbrs.append(pbr)
            except Exception:
                continue

        if not oper_margins:
            return None

        return {
            "업종명":         industry,
            "샘플수":         len(sample),
            "평균영업이익률": round(sum(oper_margins) / len(oper_margins), 2) if oper_margins else None,
            "평균PER":        round(sum(pers) / len(pers), 2) if pers else None,
            "평균PBR":        round(sum(pbrs) / len(pbrs), 2) if pbrs else None,
        }

    except Exception:
        return None
