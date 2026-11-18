#!/usr/bin/env python3
"""市場指標とポートフォリオ銘柄の日足終値を Yahoo Finance (yfinance) から取得し
data/market.json に書き出す。GitHub Actions から定期実行する想定。

- 取得に失敗した銘柄は、前回の data/market.json の値をそのまま残す
- 全銘柄が失敗した場合のみ終了コード 1
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "portfolio.json"
OUT = ROOT / "data" / "market.json"

# 市場サマリーに表示する指標（順番 = 表示順）
MARKETS = [
    {"symbol": "^N225",    "name": "日経平均",     "kind": "index", "currency": "JPY", "digits": 2},
    {"symbol": "^DJI",     "name": "NYダウ",       "kind": "index", "currency": "USD", "digits": 2},
    {"symbol": "^GSPC",    "name": "S&P500",       "kind": "index", "currency": "USD", "digits": 2},
    {"symbol": "JPY=X",    "name": "ドル円",       "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "EURJPY=X", "name": "ユーロ円",     "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "GBPJPY=X", "name": "ポンド円",     "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "AUDJPY=X", "name": "豪ドル円",     "kind": "fx",    "currency": "JPY", "digits": 3},
    {"symbol": "AUDUSD=X", "name": "豪ドル/米ドル", "kind": "fx",    "currency": "USD", "digits": 5},
]

MARKET_HISTORY_DAYS = 365 * 2 + 30   # 市場チャートは最大2年
RETRIES = 3


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


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
    for h in config.get("holdings", []):
        bd = h.get("base_date") or config.get("default_base_date")
        if bd:
            try:
                base_dates.append(date.fromisoformat(bd))
            except ValueError:
                print(f"  invalid base_date for {h.get('symbol')}: {bd}", file=sys.stderr)
    pf_start = min(base_dates) - timedelta(days=14) if base_dates else market_start
    # 市場系列もPFの基準日より前まで取る（ベンチマーク比較・円換算のため）
    start = min(market_start, pf_start)

    targets = []
    for m in MARKETS:
        targets.append({**m, "role": "market"})
    seen = {m["symbol"] for m in MARKETS}
    for h in config.get("holdings", []):
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
        "markets": [m["symbol"] for m in MARKETS],
        "market_meta": {m["symbol"]: {"digits": m["digits"]} for m in MARKETS},
        "series": series,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({ok}/{len(targets)} fetched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
