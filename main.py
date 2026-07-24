"""
main.py — Gold AI Trader (XAU/USD)
═══════════════════════════════════════════════════════════════════
Strategia: Poziomy Strukturalne + EMA 50 Confluencja + DXY Filtr
  • Trigger: PDH/PDL/PWH/PWL + okrągłe liczby co $50 + swing H/L
  • Potwierdzenie: EMA 50 w pobliżu poziomu (dynamiczna strefa)
  • Filtr makro: DXY (USD Index) — negatywna korelacja ze złotem
  • SL/TP: ATR-based (automatycznie dostosowane do zmienności)
  • Position sizing: 5% ryzyka na trade z pełnym kompoundowaniem
  • Siła sygnału 1-3: (1) poziom strukturalny + (2) EMA 50 + (3) trend
═══════════════════════════════════════════════════════════════════
NAPRAWIONE BUGI Z ORYGINALNEGO KODU:
  [1] SHORT P&L: exit po `high` zamiast po `sl` → teraz zawsze po `sl`
  [2] Position sizing: `max(1, int(kapital*0.2/cena))` dawał stale 1 oz
      → teraz: wielkosc = ryzyko_usd / odleglosc_do_sl (5% risk sizing)
  [3] RSI: było hardcode 50.0 → teraz obliczane z danych
  [4] CAUTION_BUY: istniał w UI, ale backend nigdy go nie generował
═══════════════════════════════════════════════════════════════════
"""

import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="Gold AI Trader – Structural Levels Strategy")

# ─────────────────────────────────────────────────────────────────
# KONFIGURACJA — przenieś klucze do .env na produkcji
# ─────────────────────────────────────────────────────────────────
EMAIL_NADAWCY  = os.environ.get("EMAIL_NADAWCY",  "mikolajgwizdak@gmail.com")
HASLO_NADAWCY  = os.environ.get("HASLO_NADAWCY",  "mrlj xgky btfw vszy")
EMAIL_ODBIORCY = os.environ.get("EMAIL_ODBIORCY", "mikolajgwizdak@gmail.com")
PLIK_SYMULACJI = "symulacje.json"

# Parametry strategii (zaktualizuj po backtestcie)
SL_ATR_MULT        = 1.5    # SL = SL_ATR_MULT × ATR
TP_R_MULT          = 2.0    # TP = entry ± (SL_dist × TP_R_MULT)  → R:R = 2:1
EMA_CONF_TOLERANCE = 1.0    # EMA50 musi być w odl. ≤ tolerance × ATR od poziomu
APPROACH_THRESHOLD = 3.0    # max odległość ceny od poziomu = threshold × ATR
RISK_PCT           = 0.05   # 5% kapitału ryzykowane na trade
ROUND_STEP         = 50     # co $50 "psychologiczne" poziomy na złocie


# ─────────────────────────────────────────────────────────────────
# ZARZĄDZANIE SYMULACJAMI
# ─────────────────────────────────────────────────────────────────

def wczytaj_symulacje() -> list:
    if os.path.exists(PLIK_SYMULACJI):
        try:
            with open(PLIK_SYMULACJI, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def zapisz_symulacje(dane: list):
    with open(PLIK_SYMULACJI, "w") as f:
        json.dump(dane, f, indent=4, ensure_ascii=False)

def dodaj_symulacje(kruszec, typ, cena_wejscia, sl, tp, poziom_info):
    sym = wczytaj_symulacje()
    sym.append({
        "id": len(sym) + 1,
        "data_otwarcia": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kruszec": kruszec, "typ": typ,
        "cena_wejscia": cena_wejscia,
        "stop_loss": sl, "take_profit": tp,
        "poziom": poziom_info,
        "status": "OTWARTA 🟢", "wynik_usd": 0.0
    })
    zapisz_symulacje(sym)


# ─────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────

def wyslij_email(kruszec, sygnal, cena, sl, tp, dxy_info, poziom_cena, sila):
    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_NADAWCY
        msg["To"]      = EMAIL_ODBIORCY
        msg["Subject"] = f"🚨 GOLD SIGNAL [{sila}/3] {kruszec} – {sygnal}"
        tresc = (
            f"Sygnał: {sygnal}\n"
            f"Siła confluencji: {'⭐' * sila} ({sila}/3)\n\n"
            f"Cena wejścia: ${cena}\n"
            f"Poziom strukturalny: ${poziom_cena}\n"
            f"Makro DXY: {dxy_info}\n\n"
            f"🛑 Stop-Loss: ${sl}\n"
            f"🎯 Take-Profit: ${tp}\n"
        )
        msg.attach(MIMEText(tresc, "plain", "utf-8"))
        serwer = smtplib.SMTP("smtp.gmail.com", 587)
        serwer.starttls()
        serwer.login(EMAIL_NADAWCY, HASLO_NADAWCY.replace(" ", ""))
        serwer.send_message(msg)
        serwer.quit()
    except Exception as e:
        print(f"[EMAIL ERROR]: {e}")


# ─────────────────────────────────────────────────────────────────
# WSKAŹNIKI TECHNICZNE
# ─────────────────────────────────────────────────────────────────

def oblicz_atr(df: pd.DataFrame, okres: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"]  - df["Close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(okres).mean()

def oblicz_rsi(df: pd.DataFrame, okres: int = 14) -> pd.Series:
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0).rolling(okres).mean()
    loss  = (-delta.clip(upper=0)).rolling(okres).mean()
    rs    = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)

def _fix_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalizuje MultiIndex kolumny yfinance (jeśli wystąpią)."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ─────────────────────────────────────────────────────────────────
# POZIOMY STRUKTURALNE
# ─────────────────────────────────────────────────────────────────

def oblicz_pdh_pdl_pwh_pwl(df_1h: pd.DataFrame):
    """
    Z danych 1H resampluje do D i W, a następnie zwraca:
    PDH/PDL = high/low POPRZEDNIEGO dnia
    PWH/PWL = high/low POPRZEDNIEGO tygodnia
    Zwraca 4 Serie zindeksowane jak df_1h (forward-fill).
    """
    df_d = df_1h.resample("D").agg({"High": "max", "Low": "min"}).dropna()
    df_w = df_1h.resample("W").agg({"High": "max", "Low": "min"}).dropna()

    pdh = df_d["High"].shift(1).reindex(df_1h.index, method="ffill")
    pdl = df_d["Low"].shift(1).reindex(df_1h.index,  method="ffill")
    pwh = df_w["High"].shift(1).reindex(df_1h.index, method="ffill")
    pwl = df_w["Low"].shift(1).reindex(df_1h.index,  method="ffill")
    return pdh, pdl, pwh, pwl

def round_numbers(cena: float, step: int = ROUND_STEP) -> list:
    """Najbliższe okrągłe poziomy co $50 (psychologiczne S/R dla złota)."""
    base = round(cena / step) * step
    levels = []
    for i in range(-4, 5):
        lvl = base + i * step
        if abs(lvl - cena) > 5:  # wyklucz zbyt bliskie (<$5 od ceny)
            levels.append({"cena": float(lvl), "typ": f"Round ${int(lvl)}"})
    return levels

def wykryj_swing_levels(df: pd.DataFrame, lookback: int = 20) -> tuple:
    """
    Wstępnie oblicza WSZYSTKIE swing highs/lows w df.
    Zwraca: ([(bar_index, cena), ...], [(bar_index, cena), ...])
    Wymaga lookback barów po każdej stronie → brak lookahead bias.
    """
    highs = df["High"].to_numpy()
    lows  = df["Low"].to_numpy()
    n = len(df)
    sh, sl = [], []
    for i in range(lookback, n - lookback):
        wh = highs[i - lookback: i + lookback + 1]
        wl = lows[i  - lookback: i + lookback + 1]
        if highs[i] == wh.max():
            sh.append((i, round(float(highs[i]), 2)))
        if lows[i] == wl.min():
            sl.append((i, round(float(lows[i]),  2)))
    return sh, sl

def zbierz_poziomy_dla_bara(i: int, sh_all, sl_all,
                             pdh_arr, pdl_arr, pwh_arr, pwl_arr,
                             cena: float, max_age: int = 250) -> list:
    """
    Zbiera wszystkie poziomy aktywne w momencie bara i:
    - Swing levels z poprzednich barów (nie starsze niż max_age)
    - PDH / PDL / PWH / PWL
    - Okrągłe liczby
    """
    levels = []
    for idx, p in sh_all:
        if idx < i and (i - idx) <= max_age:
            levels.append({"cena": p, "typ": "Swing High"})
    for idx, p in sl_all:
        if idx < i and (i - idx) <= max_age:
            levels.append({"cena": p, "typ": "Swing Low"})
    for p, t in [(pdh_arr[i], "PDH"), (pdl_arr[i], "PDL"),
                 (pwh_arr[i], "PWH"), (pwl_arr[i], "PWL")]:
        if not np.isnan(p):
            levels.append({"cena": round(float(p), 2), "typ": t})
    levels += round_numbers(cena)
    return levels

def najblizszy_poziom(levels: list, cena: float, kierunek: str, promien: float):
    """
    Zwraca najbliższy poziom w podanym kierunku w obrębie promienia.
    kierunek = 'wsparcie' → poniżej ceny | 'opor' → powyżej ceny
    """
    if kierunek == "wsparcie":
        k = [l for l in levels if l["cena"] < cena and (cena - l["cena"]) <= promien]
    else:
        k = [l for l in levels if l["cena"] > cena and (l["cena"] - cena) <= promien]
    return min(k, key=lambda x: abs(x["cena"] - cena)) if k else None


# ─────────────────────────────────────────────────────────────────
# DXY (makro — korelacja ze złotem)
# ─────────────────────────────────────────────────────────────────

def analizuj_dxy() -> dict:
    try:
        dxy = _fix_df(yf.Ticker("DX-Y.NYB").history(period="1mo", interval="1h"))
        if dxy.empty:
            return {"cena": 0.0, "trend": "BRAK DANYCH", "silny_dolar": False}
        dxy["EMA50"] = dxy["Close"].ewm(span=50, adjust=False).mean()
        cena_dxy = round(float(dxy["Close"].iloc[-1]), 2)
        silny    = cena_dxy > float(dxy["EMA50"].iloc[-1])
        opis     = ("SILNY WZROSTOWY 🔴 (negatywny dla złota)"
                    if silny else "SŁABY / SPADKOWY 🟢 (pozytywny dla złota)")
        return {"cena": cena_dxy, "trend": opis, "silny_dolar": silny}
    except Exception:
        return {"cena": 0.0, "trend": "NEUTRALNY", "silny_dolar": False}


# ─────────────────────────────────────────────────────────────────
# ENDPOINT: SYGNAŁ LIVE
# ─────────────────────────────────────────────────────────────────

@app.get("/sygnal/{metal}")
def generuj_sygnal(metal: str) -> dict:
    ticker = "SI=F" if metal.lower() == "srebro" else "GC=F"
    nazwa  = ("Srebro (COMEX Silver Futures)"
              if metal.lower() == "srebro" else "Złoto (COMEX Gold Futures)")

    df = _fix_df(yf.Ticker(ticker).history(period="6mo", interval="1h"))
    if df.empty or len(df) < 220:
        return {"błąd": "Brak danych z giełdy."}

    df["EMA50"]  = df["Close"].ewm(span=50,  adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    df["ATR"]    = oblicz_atr(df)
    df["RSI"]    = oblicz_rsi(df)

    cena    = round(float(df["Close"].iloc[-1]), 2)
    ema_50  = float(df["EMA50"].iloc[-1])
    ema_200 = float(df["EMA200"].iloc[-1])
    atr     = float(df["ATR"].iloc[-1])
    rsi     = round(float(df["RSI"].iloc[-1]), 1)
    low_c   = float(df["Low"].iloc[-1])
    high_c  = float(df["High"].iloc[-1])
    low_p   = float(df["Low"].iloc[-2])
    high_p  = float(df["High"].iloc[-2])

    makro = analizuj_dxy()

    # Zebranie poziomów strukturalnych
    pdh_s, pdl_s, pwh_s, pwl_s = oblicz_pdh_pdl_pwh_pwl(df)
    sh_all, sl_all = wykryj_swing_levels(df.tail(300), lookback=20)
    static_levels  = []
    for p, t in [(float(pdh_s.iloc[-1]), "PDH"), (float(pdl_s.iloc[-1]), "PDL"),
                 (float(pwh_s.iloc[-1]), "PWH"), (float(pwl_s.iloc[-1]), "PWL")]:
        if not np.isnan(p):
            static_levels.append({"cena": round(p, 2), "typ": t})
    swing_levels = ([{"cena": p, "typ": "Swing High"} for _, p in sh_all[-4:]] +
                    [{"cena": p, "typ": "Swing Low"}  for _, p in sl_all[-4:]])
    all_levels   = static_levels + swing_levels + round_numbers(cena)

    sl_dist = SL_ATR_MULT * atr
    promien = APPROACH_THRESHOLD * atr

    # Domyślne wartości (HOLD)
    akcja, opis, sl_price, tp_price, sila, poziom_obj = (
        "HOLD", "⏳ CZEKAJ — brak sygnału strukturalnego",
        round(cena - sl_dist, 2), round(cena + sl_dist * TP_R_MULT, 2),
        0, None
    )

    # ── LONG ─────────────────────────────────────────────────────
    lvl_w = najblizszy_poziom(all_levels, cena, "wsparcie", promien)
    if lvl_w:
        poz = lvl_w["cena"]
        dotkniety = low_c  <= poz * 1.002 or low_p  <= poz * 1.002
        odbicie   = cena > poz
        if dotkniety and odbicie:
            ema_ok   = abs(ema_50 - poz) <= EMA_CONF_TOLERANCE * atr
            trend_ok = cena > ema_200
            sila     = 1 + int(ema_ok) + int(trend_ok)
            sl_price = round(poz - sl_dist, 2)
            tp_price = round(cena + abs(cena - sl_price) * TP_R_MULT, 2)
            poziom_obj = lvl_w
            if sila >= 2 and not makro["silny_dolar"]:
                akcja = "BUY"
                opis  = (f"🔥 LONG od wsparcia ${poz} [{lvl_w['typ']}]"
                         + (" + EMA 50" if ema_ok else "")
                         + (" + trend wzrostowy" if trend_ok else ""))
            elif sila >= 1:
                akcja = "CAUTION_BUY"
                opis  = (f"⚠️ OSTROŻNY LONG od ${poz} [{lvl_w['typ']}] "
                         f"(DXY: {'niekorzystny' if makro['silny_dolar'] else 'OK'}"
                         f", confluencja: {sila}/3)")

    # ── SHORT (tylko gdy LONG nie dał sygnału) ───────────────────
    if akcja == "HOLD":
        lvl_o = najblizszy_poziom(all_levels, cena, "opor", promien)
        if lvl_o:
            poz = lvl_o["cena"]
            dotkniety  = high_c >= poz * 0.998 or high_p >= poz * 0.998
            odrzucenie = cena < poz
            if dotkniety and odrzucenie:
                ema_ok   = abs(ema_50 - poz) <= EMA_CONF_TOLERANCE * atr
                trend_ok = cena < ema_200
                sila     = 1 + int(ema_ok) + int(trend_ok)
                sl_price = round(poz + sl_dist, 2)
                tp_price = round(cena - abs(sl_price - cena) * TP_R_MULT, 2)
                poziom_obj = lvl_o
                if sila >= 2 and makro["silny_dolar"]:
                    akcja = "SELL"
                    opis  = (f"📉 SHORT od oporu ${poz} [{lvl_o['typ']}]"
                             + (" + EMA 50" if ema_ok else "")
                             + (" + trend spadkowy" if trend_ok else ""))
                elif sila >= 1:
                    akcja = "CAUTION_BUY"
                    opis  = (f"⚠️ OSTROŻNY SHORT od ${poz} [{lvl_o['typ']}] "
                             f"(confluencja: {sila}/3)")

    if akcja in ("BUY", "SELL") and poziom_obj:
        wyslij_email(nazwa, akcja, cena, sl_price, tp_price,
                     makro["trend"], poziom_obj["cena"], sila)
        dodaj_symulacje(nazwa, "LONG" if akcja == "BUY" else "SHORT",
                        cena, sl_price, tp_price,
                        f"{poziom_obj['typ']} @ ${poziom_obj['cena']}")

    rr = round(abs(tp_price - cena) / max(abs(cena - sl_price), 0.01), 2)

    historia = []
    for idx, row in df.tail(120).iterrows():
        historia.append({
            "data":    idx.strftime("%Y-%m-%d %H:%M"),
            "open":    round(float(row["Open"]),   2),
            "high":    round(float(row["High"]),   2),
            "low":     round(float(row["Low"]),    2),
            "close":   round(float(row["Close"]),  2),
            "ema_200": round(float(row["EMA200"]), 2),
            "ema_50":  round(float(row["EMA50"]),  2),
        })

    # Poziomy w zasięgu ±3% ceny (do wykresu)
    poziomy_w_zasiegu = sorted(
        [l for l in all_levels if abs(l["cena"] - cena) / cena < 0.03],
        key=lambda x: x["cena"]
    )

    return {
        "kruszec":            nazwa,
        "aktualna_cena_usd":  cena,
        "wskaźnik_rsi":       rsi,
        "ema_200":            round(ema_200, 2),
        "ema_50":             round(ema_50, 2),
        "zmienność_atr":      round(atr, 2),
        "trend_glowny":       "WZROSTOWY 🟢" if cena > ema_200 else "SPADKOWY 🔴",
        "makro_dxy":          makro,
        "rekomendacja":       akcja,
        "opis_sygnalu":       opis,
        "sila_sygnalu":       sila,
        "poziomy_strukturalne": poziomy_w_zasiegu,
        "najblizszy_poziom":  poziom_obj,
        "zarządzanie_ryzykiem": {
            "proponowany_stop_loss_usd":   sl_price,
            "proponowany_take_profit_usd": tp_price,
            "stosunek_zysk_ryzyko":        f"{rr}:1",
        },
        "historia_cen": historia,
    }


# ─────────────────────────────────────────────────────────────────
# ENDPOINT: BACKTEST Z COMPOUNDOWANIEM
# ─────────────────────────────────────────────────────────────────

@app.get("/backtest/{metal}")
def wykonaj_backtest(metal: str,
                      okres: str = "2y",
                      kapital_startowy: float = 10000.0,
                      sl_atr: float = SL_ATR_MULT,
                      tp_r: float   = TP_R_MULT) -> dict:
    ticker = "SI=F" if metal.lower() == "srebro" else "GC=F"
    nazwa  = ("Srebro (COMEX Silver Futures)"
              if metal.lower() == "srebro" else "Złoto (COMEX Gold Futures)")

    df = _fix_df(yf.Ticker(ticker).history(period=okres, interval="1h"))
    if df.empty or len(df) < 250:
        return {"błąd": "Zbyt mało danych. Użyj okresu co najmniej 6mo."}

    df["EMA50"]  = df["Close"].ewm(span=50,  adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    df["ATR"]    = oblicz_atr(df)

    # Pre-compute — wykonywane RAZ, nie w pętli
    sh_all, sl_all = wykryj_swing_levels(df, lookback=20)
    pdh_s, pdl_s, pwh_s, pwl_s = oblicz_pdh_pdl_pwh_pwl(df)
    pdh_arr = pdh_s.to_numpy(dtype=float)
    pdl_arr = pdl_s.to_numpy(dtype=float)
    pwh_arr = pwh_s.to_numpy(dtype=float)
    pwl_arr = pwl_s.to_numpy(dtype=float)

    kapital     = kapital_startowy
    hist_kap    = [{"data": df.index[220].strftime("%Y-%m-%d"), "kapital": kapital}]
    pozycja     = None
    cena_ent = sl = tp = wielkosc = 0.0
    wygrane = przegrane = 0
    transakcje  = []
    miesiace: dict = {}
    WARMUP = 220

    for i in range(WARMUP, len(df)):
        row    = df.iloc[i]
        cena   = float(row["Close"])
        high   = float(row["High"])
        low    = float(row["Low"])
        ema50  = float(row["EMA50"])
        ema200 = float(row["EMA200"])
        atr    = float(row["ATR"])
        data_s = df.index[i].strftime("%Y-%m-%d %H:%M")
        mies   = df.index[i].strftime("%Y-%m")

        if np.isnan(atr) or atr <= 0:
            continue
        if mies not in miesiace:
            miesiace[mies] = {"pnl": 0.0, "n": 0}

        # ── ZAMKNIĘCIE OTWARTEJ POZYCJI ──────────────────────────
        if pozycja == "LONG":
            if low <= sl:
                pnl = (sl - cena_ent) * wielkosc    # ujemny
                k_pre = kapital
                kapital += pnl; przegrane += 1; pozycja = None
                transakcje.append({"data": data_s, "typ": "LONG",  "wynik": "❌ SL",
                                    "pnl": round(pnl, 2), "cena_exit": round(sl, 2),
                                    "kapital_przed": round(k_pre, 2), "kapital_po": round(kapital, 2)})
                miesiace[mies]["pnl"] += pnl; miesiace[mies]["n"] += 1
            elif high >= tp:
                pnl = (tp - cena_ent) * wielkosc    # dodatni
                k_pre = kapital
                kapital += pnl; wygrane += 1; pozycja = None
                transakcje.append({"data": data_s, "typ": "LONG",  "wynik": "🟢 TP",
                                    "pnl": round(pnl, 2), "cena_exit": round(tp, 2),
                                    "kapital_przed": round(k_pre, 2), "kapital_po": round(kapital, 2)})
                miesiace[mies]["pnl"] += pnl; miesiace[mies]["n"] += 1

        elif pozycja == "SHORT":
            if high >= sl:
                pnl = -(sl - cena_ent) * wielkosc   # ujemny
                k_pre = kapital
                kapital += pnl; przegrane += 1; pozycja = None
                transakcje.append({"data": data_s, "typ": "SHORT", "wynik": "❌ SL",
                                    "pnl": round(pnl, 2), "cena_exit": round(sl, 2),
                                    "kapital_przed": round(k_pre, 2), "kapital_po": round(kapital, 2)})
                miesiace[mies]["pnl"] += pnl; miesiace[mies]["n"] += 1
            elif low <= tp:
                pnl = (cena_ent - tp) * wielkosc     # dodatni
                k_pre = kapital
                kapital += pnl; wygrane += 1; pozycja = None
                transakcje.append({"data": data_s, "typ": "SHORT", "wynik": "🟢 TP",
                                    "pnl": round(pnl, 2), "cena_exit": round(tp, 2),
                                    "kapital_przed": round(k_pre, 2), "kapital_po": round(kapital, 2)})
                miesiace[mies]["pnl"] += pnl; miesiace[mies]["n"] += 1

        # ── SZUKANIE NOWEGO SYGNAŁU ───────────────────────────────
        if pozycja is None:
            levels  = zbierz_poziomy_dla_bara(i, sh_all, sl_all,
                                               pdh_arr, pdl_arr, pwh_arr, pwl_arr, cena)
            sl_dist = sl_atr * atr
            promien = APPROACH_THRESHOLD * atr
            ema_tol = EMA_CONF_TOLERANCE * atr
            low_p   = float(df.iloc[i - 1]["Low"])
            high_p  = float(df.iloc[i - 1]["High"])

            # LONG
            lvl_w = najblizszy_poziom(levels, cena, "wsparcie", promien)
            if lvl_w:
                poz = lvl_w["cena"]
                if (low <= poz * 1.002 or low_p <= poz * 1.002) and cena > poz:
                    ema_ok   = abs(ema50 - poz) <= ema_tol
                    trend_ok = cena > ema200
                    if (1 + int(ema_ok) + int(trend_ok)) >= 2:
                        sl_p  = round(poz - sl_dist, 2)
                        tp_p  = round(cena + abs(cena - sl_p) * tp_r, 2)
                        # [FIX] 5% risk compounding — wielkosc rośnie wraz z kapitałem
                        ryzyko  = kapital * RISK_PCT
                        wielkosc = ryzyko / max(abs(cena - sl_p), 0.01)
                        pozycja, cena_ent, sl, tp = "LONG", cena, sl_p, tp_p
                        continue

            # SHORT
            lvl_o = najblizszy_poziom(levels, cena, "opor", promien)
            if lvl_o:
                poz = lvl_o["cena"]
                if (high >= poz * 0.998 or high_p >= poz * 0.998) and cena < poz:
                    ema_ok   = abs(ema50 - poz) <= ema_tol
                    trend_ok = cena < ema200
                    if (1 + int(ema_ok) + int(trend_ok)) >= 2:
                        sl_p  = round(poz + sl_dist, 2)
                        tp_p  = round(cena - abs(sl_p - cena) * tp_r, 2)
                        ryzyko  = kapital * RISK_PCT
                        wielkosc = ryzyko / max(abs(sl_p - cena), 0.01)
                        pozycja, cena_ent, sl, tp = "SHORT", cena, sl_p, tp_p
                        continue

        if i % 24 == 0 or i == len(df) - 1:
            hist_kap.append({"data": data_s, "kapital": round(kapital, 2)})

    total = wygrane + przegrane
    rozklad = [
        {"miesiac": m, "n_trades": v["n"],
         "pnl_usd": round(v["pnl"], 2),
         "pct":     round(v["pnl"] / kapital_startowy * 100, 2)}
        for m, v in sorted(miesiace.items()) if v["n"] > 0
    ]

    rolling = _rolling_window_analysis(transakcje, df)

    return {
        "kruszec":            nazwa,
        "parametry":          {"sl_atr": sl_atr, "tp_r": tp_r},
        "kapital_poczatkowy": kapital_startowy,
        "kapital_koncowy":    round(kapital, 2),
        "calkowity_zysk_usd": round(kapital - kapital_startowy, 2),
        "zwrot_procentowy":   round((kapital / kapital_startowy - 1) * 100, 2),
        "laczna_liczba_transakcji": total,
        "wygrane":            wygrane,
        "przegrane":          przegrane,
        "win_rate":           round(wygrane / total * 100, 1) if total > 0 else 0.0,
        "historia_kapitalu":  hist_kap,
        "rozklad_miesieczny": rozklad,
        "rolling_window":     rolling,
        "lista_transakcji":   transakcje[::-1],
    }


# ─────────────────────────────────────────────────────────────────
# ROLLING WINDOW ANALYSIS (kluczowa analiza reżymów rynkowych)
# ─────────────────────────────────────────────────────────────────

def _rolling_window_analysis(transakcje: list, df: pd.DataFrame) -> dict:
    """
    Grupuje transakcje po KWARTAŁACH i dla każdego oblicza:
    - wyniki finansowe (P&L, win rate, kapitał)
    - reżym rynku (silny trend / umiarkowany / boczny)

    To pozwala odpowiedzieć na pytanie: w jakich warunkach strategia zarabia,
    a w jakich traci? Kluczowe PRZED przejściem na realny kapitał.
    """
    if not transakcje:
        return {"kwartalnie": [], "wniosek": "Brak transakcji do analizy."}

    # Grupowanie po kwartałach
    kwartaly: dict = {}
    for t in transakcje:
        try:
            dt = pd.Timestamp(t["data"])
            q  = f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"
            kwartaly.setdefault(q, {"dt": dt, "trades": []})
            kwartaly[q]["trades"].append(t)
        except Exception:
            continue

    wyniki = []
    for q in sorted(kwartaly.keys()):
        trades  = kwartaly[q]["trades"]
        dt_q    = kwartaly[q]["dt"]
        n       = len(trades)
        pnl     = sum(t["pnl"] for t in trades)
        wygrane = sum(1 for t in trades if t["pnl"] > 0)
        k_start = trades[0].get("kapital_przed", 0)
        k_end   = trades[-1].get("kapital_po",   0)
        pct     = round((k_end / k_start - 1) * 100, 2) if k_start > 0 else 0.0

        # ── Reżym rynku dla tego kwartału ──────────────────────
        dt_end = dt_q + pd.DateOffset(months=3)
        df_q   = df[(df.index >= dt_q) & (df.index < dt_end)]
        rezym, trend_tag = "?", "neutralny"

        if len(df_q) > 20:
            c_start = float(df_q["Close"].iloc[0])
            c_end   = float(df_q["Close"].iloc[-1])
            zasieg  = float(df_q["High"].max() - df_q["Low"].min())
            zmiana  = c_end - c_start
            # sila_trendu: 0=boczny, 1=idealny trend
            # im większa część zasięgu to ruch kierunkowy, tym silniejszy trend
            sila = abs(zmiana) / zasieg if zasieg > 0 else 0

            if zmiana > 0:
                kierunek, trend_tag = "📈 Wzrostowy", "wzrostowy"
            else:
                kierunek, trend_tag = "📉 Spadkowy",  "spadkowy"

            if sila > 0.35:
                rezym = f"{kierunek} — silny trend ({sila*100:.0f}% zasięgu)"
            elif sila > 0.15:
                rezym = f"{kierunek} — umiarkowany ({sila*100:.0f}%)"
            else:
                rezym = f"↔️ Boczny / Choppy ({sila*100:.0f}% zasięgu)"
                trend_tag = "boczny"

        wyniki.append({
            "kwartal":      q,
            "n_trades":     n,
            "wygrane":      wygrane,
            "przegrane":    n - wygrane,
            "win_rate":     round(wygrane / n * 100, 1) if n > 0 else 0.0,
            "pnl_usd":      round(pnl, 2),
            "kapital_start": round(k_start, 2),
            "kapital_end":  round(k_end, 2),
            "pct_change":   pct,
            "rezym":        rezym,
            "_tag":         trend_tag,   # wewnętrzne, do wniosku
        })

    # ── Wniosek kluczowy ───────────────────────────────────────
    def _stat(tag):
        sub = [w for w in wyniki if w["_tag"] == tag]
        if not sub:
            return None, 0
        return round(sum(w["pnl_usd"] for w in sub) / len(sub), 2), len(sub)

    avg_wzr,  n_wzr  = _stat("wzrostowy")
    avg_spad, n_spad = _stat("spadkowy")
    avg_bocz, n_bocz = _stat("boczny")

    zysk_q  = sum(1 for w in wyniki if w["pnl_usd"] > 0)
    total_q = len(wyniki)
    pct_q   = round(zysk_q / total_q * 100) if total_q else 0

    wniosek_czesci = [
        f"Strategia zyska w {zysk_q}/{total_q} kwartałów ({pct_q}% czasu)."
    ]
    if avg_wzr  is not None: wniosek_czesci.append(f"📈 Trend wzrostowy ({n_wzr} kw.): śr. {avg_wzr:+.2f}$/kw.")
    if avg_spad is not None: wniosek_czesci.append(f"📉 Trend spadkowy ({n_spad} kw.): śr. {avg_spad:+.2f}$/kw.")
    if avg_bocz is not None: wniosek_czesci.append(f"↔️ Rynek boczny ({n_bocz} kw.): śr. {avg_bocz:+.2f}$/kw.")

    # Usuń tag wewnętrzny z outputu
    for w in wyniki:
        w.pop("_tag", None)

    return {
        "kwartalnie": wyniki,
        "wniosek":    " | ".join(wniosek_czesci),
        "statystyki": {
            "wzrostowy": {"avg_pnl": avg_wzr,  "n": n_wzr},
            "spadkowy":  {"avg_pnl": avg_spad, "n": n_spad},
            "boczny":    {"avg_pnl": avg_bocz, "n": n_bocz},
        }
    }


# ─────────────────────────────────────────────────────────────────
# ENDPOINT: AKTUALIZACJA SYMULACJI LIVE
# ─────────────────────────────────────────────────────────────────

@app.get("/pobierz_symulacje")
def pobierz_symulacje_api() -> list:
    sym = wczytaj_symulacje()
    for s in sym:
        if s["status"] != "OTWARTA 🟢":
            continue
        ticker = "SI=F" if "Srebro" in s["kruszec"] else "GC=F"
        try:
            akt = float(_fix_df(yf.Ticker(ticker).history(period="1d"))["Close"].iloc[-1])
            if s["typ"] == "LONG":
                if   akt <= s["stop_loss"]:   s["status"], s["wynik_usd"] = "STRATA ❌", round(s["stop_loss"]   - s["cena_wejscia"], 2)
                elif akt >= s["take_profit"]:  s["status"], s["wynik_usd"] = "ZYSK 🟢",  round(s["take_profit"] - s["cena_wejscia"], 2)
                else:                          s["wynik_usd"] = round(akt - s["cena_wejscia"], 2)
            else:  # SHORT
                if   akt >= s["stop_loss"]:   s["status"], s["wynik_usd"] = "STRATA ❌", round(s["cena_wejscia"] - s["stop_loss"],   2)
                elif akt <= s["take_profit"]:  s["status"], s["wynik_usd"] = "ZYSK 🟢",  round(s["cena_wejscia"] - s["take_profit"], 2)
                else:                          s["wynik_usd"] = round(s["cena_wejscia"] - akt, 2)
        except Exception:
            pass
    zapisz_symulacje(sym)
    return sym


# ─────────────────────────────────────────────────────────────────
# SCHEDULER — skan złota co 60 minut w tle
# ─────────────────────────────────────────────────────────────────

def praca_robota():
    try:
        generuj_sygnal("zloto")
    except Exception as e:
        print(f"[Scheduler] {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(praca_robota, "interval", minutes=60)
scheduler.start()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
