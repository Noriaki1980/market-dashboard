#!/usr/bin/env python3
"""市場指標とポートフォリオ銘柄の日足終値を Yahoo Finance (yfinance) から取得し
data/market.json に書き出す。GitHub Actions から定期実行する想定。

- 取得に失敗した銘柄は、前回の data/market.json の値をそのまま残す
- 全銘柄が失敗した場合のみ終了コード 1
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "portfolio.json"
MARKETS_CONFIG = ROOT / "config" / "markets.json"
OUT = ROOT / "data" / "market.json"

# 市場サマリーに表示する指標の既定値（config/markets.json がないときに使う。順番 = 表示順）
DEFAULT_MARKETS = [
    {"symbol": "^N225",    "name": "日経平均",     "kind": "index", "currency": "JPY", "digits": 2},
    {"symbol": "^DJI",     "name": "NYダウ",       "kind": "index", "currency": "USD", "digits": 2},
    {"symbol": "^GSPC",    "name": "S&P500",       "kind": "index", "currency": "USD", "digits": 2},
    {"symbol": "JPY=X",    "name": "ドル円",       "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "EURJPY=X", "name": "ユーロ円",     "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "GBPJPY=X", "name": "ポンド円",     "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "AUDJPY=X", "name": "豪ドル円",     "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "AUDUSD=X", "name": "豪ドル/米ドル", "kind": "fx",    "currency": "USD", "digits": 5},
]

# 表示しなくても常に取得するもの（ポートフォリオの比較指数・円換算用）
REQUIRED = ["^N225", "^DJI", "^GSPC", "JPY=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X"]

FX_NAMES = {"USD": "ドル", "EUR": "ユーロ", "GBP": "ポンド", "AUD": "豪ドル", "NZD": "NZドル", "CAD": "カナダドル",
            "CHF": "スイスフラン", "CNY": "人民元", "CNH": "人民元", "HKD": "香港ドル", "SGD": "シンガポールドル",
            "ZAR": "南アランド", "MXN": "メキシコペソ", "TRY": "トルコリラ", "KRW": "ウォン", "INR": "ルピー",
            "SEK": "スウェーデンクローナ", "NOK": "ノルウェークローネ", "BRL": "レアル", "JPY": "円"}


def fx_jpy_symbol(cur: str) -> str:
    """その通貨の対円レートの Yahoo シンボル（USD は JPY=X）"""
    return "JPY=X" if cur == "USD" else f"{cur}JPY=X"


def normalize_symbol(raw: str) -> str:
    """通貨ペアの書き方（CHFJPY / EUR/USD / USDJPY=X）を Yahoo 形式に。その他はそのまま"""
    x = re.sub(r"\s+", "", raw).upper() if re.fullmatch(r"[A-Za-z/=_\-\s]+", raw.strip()) else raw.strip()
    pair = re.sub(r"[/\-_]", "", x)
    pair = pair[:-2] if pair.endswith("=X") else pair
    if re.fullmatch(r"[A-Z]{6}", pair) and ("/" in x or x.endswith("=X") or re.fullmatch(r"[A-Z]{6}", x)):
        return pair[3:] + "=X" if pair.startswith("USD") else pair + "=X"   # Yahoo では USD/xxx は xxx=X
    return x


def describe_market(m: dict) -> dict:
    """markets.json の1項目を補完（name / kind / currency / digits）"""
    sym = normalize_symbol(m["symbol"])
    out = {"symbol": sym}
    if sym.endswith("=X"):
        pair = sym[:-2]
        base, quote = ("USD", pair) if len(pair) == 3 else (pair[:3], pair[3:6])
        out.update(kind="fx", currency=quote,
                   name=m.get("name") or (f"{FX_NAMES.get(base, base)}円" if quote == "JPY"
                                          else f"{FX_NAMES.get(base, base)}/{FX_NAMES.get(quote, quote)}"),
                   digits=m.get("digits", 3 if quote == "JPY" else 5))
    else:
        out.update(kind="index" if sym.startswith("^") else "equity",
                   currency=m.get("currency"), name=m.get("name") or sym, digits=m.get("digits"))
    return out


def load_markets() -> list[dict]:
    cfg = load_json(MARKETS_CONFIG, None)
    items = cfg.get("markets") if isinstance(cfg, dict) else None
    if not isinstance(items, list) or not items:
        return DEFAULT_MARKETS
    res, seen = [], set()
    for m in items:
        if isinstance(m, str):
            m = {"symbol": m}
        if not isinstance(m, dict) or not (m.get("symbol") or "").strip() or m["symbol"].strip() in seen:
            continue
        d = describe_market(m)
        seen.add(d["symbol"])
        res.append(d)
    return res or DEFAULT_MARKETS


MARKET_HISTORY_DAYS = 365 * 2 + 30   # 市場チャートは最大2年
RETRIES = 3


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def all_holdings(config):
    """全ポートフォリオの銘柄を (holding, default_base_date) で返す。旧形式（holdings 直下）も可"""
    pfs = config.get("portfolios")
    if not isinstance(pfs, list):
        pfs = [config]
    for pf in pfs:
        for h in pf.get("holdings", []) or []:
            yield h, pf.get("default_base_date")


def guess_currency(symbol: str) -> str:
    if symbol.endswith(".T"):
        return "JPY"
    if symbol.endswith(".L"):
        return "GBP"
    if symbol.endswith(".AX"):
        return "AUD"
    if symbol.endswith((".DE", ".PA", ".AS", ".MI")):
        return "EUR"
    return "USD"


def fetch_one(symbol: str, start: date):
    """(dates, closes, currency, intraday) を返す。失敗時は例外。
    intraday は直近値の時点情報 {"date", "asof", "filled", "live"}（5分足が取れないときは None）。"""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            t = yf.Ticker(symbol)
            df = t.history(start=start.isoformat(), interval="1d", auto_adjust=False, actions=False)
            if df is None or df.empty or "Close" not in df:
                raise RuntimeError("empty response")
            df = df[["Close"]].dropna()
            # 取引所ローカル日付で日付文字列化（同一日の重複は最後を採用）
            rows = {}
            for ts, row in df.iterrows():
                v = float(row["Close"])
                if math.isfinite(v):
                    rows[ts.date().isoformat()] = v
            if not rows:
                raise RuntimeError("no valid rows")
            # 5分足（直近5日）を使って
            #  1) Yahoo の日足で直近日の行が欠けている場合（日本の指数・株で起きる）にその日の最終値で補う
            #  2) 直近値が「何時時点」の値かを記録する（日本時間）
            intraday = None
            try:
                intra = t.history(period="5d", interval="5m", auto_adjust=False, actions=False)
                if intra is not None and not intra.empty and "Close" in intra:
                    last_by_day = {}
                    for ts, row in intra[["Close"]].dropna().iterrows():
                        v = float(row["Close"])
                        if math.isfinite(v):
                            last_by_day[ts.date().isoformat()] = (v, ts)  # 時刻順なので最後が残る
                    added = [d for d in last_by_day
                             if d not in rows and d > min(rows) and date.fromisoformat(d).weekday() < 5]
                    for d in added:
                        rows[d] = last_by_day[d][0]
                    if added:
                        print(f"  [{symbol}] filled from intraday: {', '.join(sorted(added))}")
                    last_d = max(rows)
                    if last_d in last_by_day:
                        # 足の時刻は「開始時刻」。値の時点は 足の開始+5分 と 取得時刻 の早い方
                        now = datetime.now(timezone.utc)
                        bar_start = last_by_day[last_d][1].to_pydatetime()
                        asof = min(bar_start + timedelta(minutes=5), now)
                        intraday = {
                            "date": last_d,                                       # 取引所の日付
                            "asof": asof.astimezone(JST).isoformat(timespec="minutes"),
                            "filled": last_d in added,                            # 5分足で補った値か
                            "live": (now - asof) <= timedelta(minutes=40),        # 取引中（暫定値）か
                        }
                        # 東証の銘柄は、引け後なら日足の値は 15:30 の終値。最後の5分足が早い時刻
                        # （出来高の少ない銘柄など）でも時点は 15:30 とする
                        if (symbol.endswith(".T") or symbol == "^N225") and not intraday["live"]:
                            close_jst = datetime.fromisoformat(last_d + "T15:30:00+09:00")
                            if asof.astimezone(JST) < close_jst <= now.astimezone(JST) - timedelta(minutes=15):
                                intraday["asof"] = close_jst.isoformat(timespec="minutes")
                        print(f"  [{symbol}] last {last_d} as of {intraday['asof']}"
                              + (" (live)" if intraday["live"] else ""))
            except Exception as e:  # noqa: BLE001
                print(f"  [{symbol}] intraday supplement skipped: {e}", file=sys.stderr)
            currency = None
            try:
                currency = t.fast_info.get("currency")
            except Exception:
                pass
            dates = sorted(rows)
            return dates, [rows[d] for d in dates], currency, intraday
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  [{symbol}] attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"{symbol}: {last_err}")


def round_series(values, digits):
    return [round(v, digits) for v in values]


def sig_digits(values):
    """株価向け: 値の大きさに応じて小数桁を決める"""
    m = max(abs(v) for v in values) if values else 1
    if m >= 1000:
        return 1
    if m >= 100:
        return 2
    if m >= 1:
        return 3
    return 5


def main() -> int:
    config = load_json(CONFIG, {"holdings": []})
    previous = load_json(OUT, {}).get("series", {})

    today = date.today()
    market_start = today - timedelta(days=MARKET_HISTORY_DAYS)

    base_dates = []
    for h, default_bd in all_holdings(config):
        bd = h.get("base_date") or default_bd
        if bd:
            try:
                base_dates.append(date.fromisoformat(bd))
            except ValueError:
                print(f"  invalid base_date for {h.get('symbol')}: {bd}", file=sys.stderr)
    pf_start = min(base_dates) - timedelta(days=14) if base_dates else market_start
    # 市場系列もPFの基準日より前まで取る（ベンチマーク比較・円換算のため）
    start = min(market_start, pf_start)

    markets = load_markets()
    targets = []
    for m in markets:
        targets.append({**m, "role": "market"})
    seen = {m["symbol"] for m in markets}
    defaults = {m["symbol"]: m for m in DEFAULT_MARKETS}
    for sym in REQUIRED:
        if sym not in seen:
            seen.add(sym)
            targets.append({**(defaults.get(sym) or describe_market({"symbol": sym})), "role": "aux"})
    for h, _ in all_holdings(config):
        s = (h.get("symbol") or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        targets.append({"symbol": s, "name": h.get("name") or s, "kind": "equity", "role": "holding"})

    series = {}
    ok = 0
    for tgt in targets:
        sym = tgt["symbol"]
        print(f"fetch {sym} from {start}")
        try:
            dates, closes, cur, intraday = fetch_one(sym, start)
            currency = tgt.get("currency") or cur or guess_currency(sym)
            digits = tgt.get("digits") if tgt.get("digits") is not None else sig_digits(closes)
            series[sym] = {
                "name": tgt["name"],
                "kind": tgt["kind"],
                "currency": currency.upper(),
                "dates": dates,
                "close": round_series(closes, digits),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if intraday:
                # 直近値が何時時点（日本時間）か、取引中の暫定値か
                series[sym]["intraday"] = intraday
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  !! {e} -> keep previous data", file=sys.stderr)
            if sym in previous:
                prev = dict(previous[sym])
                prev["stale"] = True
                series[sym] = prev

    # 保有銘柄の通貨（USD/EUR/GBP/AUD 以外も）を円換算できるよう、対円レートを追加で取得
    holding_syms = {t["symbol"] for t in targets if t["role"] == "holding"}
    for cur in sorted({series[k].get("currency") for k in holding_syms if k in series} - {None, "JPY"}):
        fsym = fx_jpy_symbol(cur)
        if fsym in series:
            continue
        print(f"fetch {fsym} (for {cur} holdings)")
        try:
            dates, closes, _, intraday = fetch_one(fsym, start)
            series[fsym] = {"name": f"{FX_NAMES.get(cur, cur)}円", "kind": "fx", "currency": "JPY", "dates": dates,
                            "close": round_series(closes, 4),
                            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if intraday:
                series[fsym]["intraday"] = intraday
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  !! {e}", file=sys.stderr)
            if fsym in previous:
                series[fsym] = {**previous[fsym], "stale": True}

    if ok == 0:
        print("all fetches failed; data not updated", file=sys.stderr)
        return 1

    def strip_ts(s):
        return {k: {kk: vv for kk, vv in v.items() if kk != "fetched_at"} for k, v in s.items()}

    if previous and strip_ts(previous) == strip_ts(series):
        print("no price changes; keep existing file (avoids empty commits)")
        return 0

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (yfinance) 日足終値",
        "markets": [m["symbol"] for m in markets],
        "market_meta": {m["symbol"]: {"digits": m.get("digits"), "name": m["name"]} for m in markets},
        "series": series,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({ok} fetched, {len(series)} series)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
