"""
main.py — Gold AI Trader (XAU/USD)
Strategia: Poziomy Strukturalne + EMA 50 Confluencja + DXY Filtr
"""

import json, os, smtplib
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

EMAIL_NADAWCY  = os.environ.get("EMAIL_NADAWCY",  "mikolajgwizdak@gmail.com")
HASLO_NADAWCY  = os.environ.get("HASLO_NADAWCY",  "mrlj xgky btfw vszy")
EMAIL_ODBIORCY = os.environ.get("EMAIL_ODBIORCY", "mikolajgwizdak@gmail.com")
PLIK_SYMULACJI = "symulacje.json"

SL_ATR_MULT        = 1.5
TP_R_MULT          = 4.0    # wynik grid search: 4R najlepsza matematyka przy win rate ~24%
EMA_CONF_TOLERANCE = 1.0
APPROACH_THRESHOLD = 1.5    # było 3.0 — teraz tylko sygnały BLISKO poziomu
MIN_CONFLUENCJA    = 3      # było 2 — teraz WSZYSTKIE 3 czynniki muszą grać
N_SLOPE_BARS       = 168    # 1 tydzień godzinowych świec = okno kierunku EMA200
RISK_PCT           = 0.05
ROUND_STEP         = 50


# ─── Symulacje ────────────────────────────────────────────────────

def wczytaj_symulacje():
    if os.path.exists(PLIK_SYMULACJI):
        try:
            with open(PLIK_SYMULACJI) as f: return json.load(f)
        except Exception: return []
    return []

def zapisz_symulacje(dane):
    with open(PLIK_SYMULACJI, "w") as f:
        json.dump(dane, f, indent=4, ensure_ascii=False)

def dodaj_symulacje(kruszec, typ, cena_wejscia, sl, tp, poziom_info):
    sym = wczytaj_symulacje()
    sym.append({"id": len(sym)+1,
                "data_otwarcia": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "kruszec": kruszec, "typ": typ,
                "cena_wejscia": cena_wejscia, "stop_loss": sl, "take_profit": tp,
                "poziom": poziom_info, "status": "OTWARTA 🟢", "wynik_usd": 0.0})
    zapisz_symulacje(sym)


# ─── Email ────────────────────────────────────────────────────────

def wyslij_email(kruszec, sygnal, cena, sl, tp, dxy_info, poziom_cena, sila):
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_NADAWCY; msg["To"] = EMAIL_ODBIORCY
        msg["Subject"] = f"🚨 GOLD [{sila}/3] {kruszec} – {sygnal}"
        msg.attach(MIMEText(
            f"Sygnał: {sygnal}\nSiła: {'⭐'*sila} ({sila}/3)\n"
            f"Cena: ${cena}\nPoziom: ${poziom_cena}\nDXY: {dxy_info}\n"
            f"🛑 SL: ${sl}\n🎯 TP: ${tp}", "plain", "utf-8"))
        s = smtplib.SMTP("smtp.gmail.com", 587)
        s.starttls(); s.login(EMAIL_NADAWCY, HASLO_NADAWCY.replace(" ", ""))
        s.send_message(msg); s.quit()
    except Exception as e:
        print(f"[EMAIL] {e}")


# ─── Wskaźniki ────────────────────────────────────────────────────

def oblicz_atr(df, okres=14):
    hl = df["High"]-df["Low"]
    hc = (df["High"]-df["Close"].shift()).abs()
    lc = (df["Low"] -df["Close"].shift()).abs()
    return pd.concat([hl,hc,lc],axis=1).max(axis=1).rolling(okres).mean()

def oblicz_rsi(df, okres=14):
    d = df["Close"].diff()
    g = d.clip(lower=0).rolling(okres).mean()
    l = (-d.clip(upper=0)).rolling(okres).mean()
    return (100-100/(1+g/l.replace(0,np.nan))).fillna(50.0)

def _fix(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ─── Poziomy strukturalne ─────────────────────────────────────────

def oblicz_pdh_pdl_pwh_pwl(df1h):
    dd = df1h.resample("D").agg({"High":"max","Low":"min"}).dropna()
    dw = df1h.resample("W").agg({"High":"max","Low":"min"}).dropna()
    return (dd["High"].shift(1).reindex(df1h.index,method="ffill"),
            dd["Low"].shift(1).reindex(df1h.index,method="ffill"),
            dw["High"].shift(1).reindex(df1h.index,method="ffill"),
            dw["Low"].shift(1).reindex(df1h.index,method="ffill"))

def round_numbers(cena, step=ROUND_STEP):
    base = round(cena/step)*step
    return [{"cena":float(base+i*step),"typ":f"Round ${int(base+i*step)}"}
            for i in range(-4,5) if abs(base+i*step-cena)>5]

def wykryj_swing_levels(df, lookback=20):
    H,L,n = df["High"].to_numpy(),df["Low"].to_numpy(),len(df)
    sh,sl = [],[]
    for i in range(lookback,n-lookback):
        if H[i]==H[i-lookback:i+lookback+1].max(): sh.append((i,round(float(H[i]),2)))
        if L[i]==L[i-lookback:i+lookback+1].min(): sl.append((i,round(float(L[i]),2)))
    return sh,sl

def zbierz_poziomy(i,sh,sl,pdh,pdl,pwh,pwl,cena,max_age=250):
    levels=[]
    for idx,p in sh:
        if idx<i and (i-idx)<=max_age: levels.append({"cena":p,"typ":"Swing High"})
    for idx,p in sl:
        if idx<i and (i-idx)<=max_age: levels.append({"cena":p,"typ":"Swing Low"})
    for p,t in [(pdh[i],"PDH"),(pdl[i],"PDL"),(pwh[i],"PWH"),(pwl[i],"PWL")]:
        if not np.isnan(p): levels.append({"cena":round(float(p),2),"typ":t})
    return levels+round_numbers(cena)

def najblizszy(levels,cena,kier,promien):
    k=[l for l in levels if (l["cena"]<cena and kier=="wsparcie" and cena-l["cena"]<=promien)
                          or (l["cena"]>cena and kier=="opor"     and l["cena"]-cena<=promien)]
    return min(k,key=lambda x:abs(x["cena"]-cena)) if k else None


# ─── DXY ──────────────────────────────────────────────────────────

def analizuj_dxy():
    try:
        d = _fix(yf.Ticker("DX-Y.NYB").history(period="1mo",interval="1h"))
        if d.empty: return {"cena":0.0,"trend":"BRAK DANYCH","silny_dolar":False}
        d["E50"]=d["Close"].ewm(span=50,adjust=False).mean()
        c=round(float(d["Close"].iloc[-1]),2); silny=c>float(d["E50"].iloc[-1])
        return {"cena":c,
                "trend":("SILNY WZROSTOWY 🔴 (negatywny dla złota)" if silny
                         else "SŁABY / SPADKOWY 🟢 (pozytywny dla złota)"),
                "silny_dolar":silny}
    except Exception:
        return {"cena":0.0,"trend":"NEUTRALNY","silny_dolar":False}


# ─── Rolling Window Analysis ──────────────────────────────────────

def _rolling_window_analysis(transakcje: list, df: pd.DataFrame) -> dict:
    """
    Grupuje transakcje po kwartałach, dla każdego liczy wyniki + reżym rynku.
    Kluczowa analiza przed realnym kapitałem.
    """
    if not transakcje:
        return {"kwartalnie":[],"wniosek":"Brak transakcji.","statystyki":{}}

    # Obsługa timezone — yfinance zwraca UTC
    df_tz = getattr(df.index,"tz",None)

    kwartaly: dict = {}
    for t in transakcje:
        try:
            dt = pd.Timestamp(t["data"])
            if dt.tzinfo is None and df_tz is not None:
                dt = dt.tz_localize(df_tz)
            q = f"{dt.year}-Q{(dt.month-1)//3+1}"
            kwartaly.setdefault(q,{"dt":dt,"trades":[]})
            kwartaly[q]["trades"].append(t)
        except Exception:
            continue

    if not kwartaly:
        return {"kwartalnie":[],"wniosek":"Nie udało się zgrupować transakcji.","statystyki":{}}

    wyniki=[]
    for q in sorted(kwartaly.keys()):
        try:
            trades = kwartaly[q]["trades"]
            dt_q   = kwartaly[q]["dt"]
            n      = len(trades)
            pnl    = sum(t["pnl"] for t in trades)
            wygr   = sum(1 for t in trades if t["pnl"]>0)
            k_s    = trades[0].get("kapital_przed",0)
            k_e    = trades[-1].get("kapital_po",0)
            pct    = round((k_e/k_s-1)*100,2) if k_s>0 else 0.0
            dt_end = dt_q+pd.DateOffset(months=3)

            # Reżym — z bezpiecznym porównaniem dat
            try:
                df_q = df[(df.index>=dt_q)&(df.index<dt_end)]
            except TypeError:
                idx_n = df.index.tz_localize(None) if df_tz else df.index
                dt_n  = dt_q.tz_localize(None)  if dt_q.tzinfo  else dt_q
                de_n  = dt_end.tz_localize(None) if dt_end.tzinfo else dt_end
                df_q  = df[(idx_n>=dt_n)&(idx_n<de_n)]

            rezym,tag="↔️ Brak danych","neutralny"
            if len(df_q)>20:
                cs=float(df_q["Close"].iloc[0]); ce=float(df_q["Close"].iloc[-1])
                zas=float(df_q["High"].max()-df_q["Low"].min())
                zm=ce-cs; sila=abs(zm)/zas if zas>0 else 0
                kier="📈 Wzrostowy" if zm>0 else "📉 Spadkowy"
                tag ="wzrostowy"   if zm>0 else "spadkowy"
                if   sila>0.35: rezym=f"{kier} — silny ({sila*100:.0f}% zasięgu)"
                elif sila>0.15: rezym=f"{kier} — umiarkowany ({sila*100:.0f}%)"
                else:           rezym=f"↔️ Boczny / Choppy ({sila*100:.0f}% zasięgu)"; tag="boczny"

            wyniki.append({"kwartal":q,"n_trades":n,"wygrane":wygr,"przegrane":n-wygr,
                           "win_rate":round(wygr/n*100,1) if n else 0.0,
                           "pnl_usd":round(pnl,2),"kapital_start":round(k_s,2),
                           "kapital_end":round(k_e,2),"pct_change":pct,"rezym":rezym,"_tag":tag})
        except Exception as e:
            print(f"[RollingWindow] {q}: {e}"); continue

    if not wyniki:
        return {"kwartalnie":[],"wniosek":"Brak danych.","statystyki":{}}

    def _st(tag):
        sub=[w for w in wyniki if w["_tag"]==tag]
        return (round(sum(w["pnl_usd"] for w in sub)/len(sub),2),len(sub)) if sub else (None,0)

    av,nv=_st("wzrostowy"); as_,ns=_st("spadkowy"); ab,nb=_st("boczny")
    zq=sum(1 for w in wyniki if w["pnl_usd"]>0); tq=len(wyniki)
    cz=[f"Strategia zyska w {zq}/{tq} kwartałów ({round(zq/tq*100) if tq else 0}% czasu)."]
    if av  is not None: cz.append(f"📈 Trend wzrostowy ({nv} kw.): śr. {av:+.2f}$/kw.")
    if as_ is not None: cz.append(f"📉 Trend spadkowy ({ns} kw.): śr. {as_:+.2f}$/kw.")
    if ab  is not None: cz.append(f"↔️ Rynek boczny ({nb} kw.): śr. {ab:+.2f}$/kw.")
    for w in wyniki: w.pop("_tag",None)

    return {"kwartalnie":wyniki,"wniosek":" | ".join(cz),
            "statystyki":{"wzrostowy":{"avg_pnl":av,"n":nv},
                          "spadkowy": {"avg_pnl":as_,"n":ns},
                          "boczny":   {"avg_pnl":ab,"n":nb}}}


# ─── Endpoint: Sygnał Live ────────────────────────────────────────

@app.get("/sygnal/{metal}")
def generuj_sygnal(metal: str) -> dict:
    ticker = "SI=F" if metal.lower()=="srebro" else "GC=F"
    nazwa  = ("Srebro (COMEX Silver Futures)" if metal.lower()=="srebro"
              else "Złoto (COMEX Gold Futures)")

    df = _fix(yf.Ticker(ticker).history(period="6mo",interval="1h"))
    if df.empty or len(df)<220: return {"błąd":"Brak danych z giełdy."}

    df["EMA50"] =df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"]=df["Close"].ewm(span=200,adjust=False).mean()
    df["ATR"]   =oblicz_atr(df); df["RSI"]=oblicz_rsi(df)

    cena=round(float(df["Close"].iloc[-1]),2)
    ema50=float(df["EMA50"].iloc[-1]); ema200=float(df["EMA200"].iloc[-1])
    atr=float(df["ATR"].iloc[-1]);     rsi=round(float(df["RSI"].iloc[-1]),1)
    lc=float(df["Low"].iloc[-1]);      hc=float(df["High"].iloc[-1])
    lp=float(df["Low"].iloc[-2]);      hp=float(df["High"].iloc[-2])

    # Kierunek EMA200 w ostatnim tygodniu — filtr antytrendowy
    n_sl = min(N_SLOPE_BARS, len(df)-1)
    trend_w = 1 if ema200 > float(df["EMA200"].iloc[-1-n_sl]) else -1
    trend_w_opis = "📈 EMA200 rośnie (sprzyja LONG)" if trend_w > 0 else "📉 EMA200 spada (sprzyja SHORT)"

    makro = analizuj_dxy()
    pdh_s,pdl_s,pwh_s,pwl_s = oblicz_pdh_pdl_pwh_pwl(df)
    static=[]
    for p,t in [(float(pdh_s.iloc[-1]),"PDH"),(float(pdl_s.iloc[-1]),"PDL"),
                (float(pwh_s.iloc[-1]),"PWH"),(float(pwl_s.iloc[-1]),"PWL")]:
        if not np.isnan(p): static.append({"cena":round(p,2),"typ":t})
    sh_all,sl_all=wykryj_swing_levels(df.tail(300),lookback=20)
    swings=([{"cena":p,"typ":"Swing High"} for _,p in sh_all[-4:]]+
            [{"cena":p,"typ":"Swing Low"}  for _,p in sl_all[-4:]])
    all_lvl=static+swings+round_numbers(cena)

    sl_dist=SL_ATR_MULT*atr; promien=APPROACH_THRESHOLD*atr
    akcja,opis,sl_p,tp_p,sila,pobj=(
        "HOLD","⏳ CZEKAJ — brak sygnału strukturalnego",
        round(cena-sl_dist,2),round(cena+sl_dist*TP_R_MULT,2),0,None)

    lw=najblizszy(all_lvl,cena,"wsparcie",promien)
    if lw:
        poz=lw["cena"]; dot=lc<=poz*1.002 or lp<=poz*1.002; odb=cena>poz
        if dot and odb:
            eok=abs(ema50-poz)<=EMA_CONF_TOLERANCE*atr; tok=cena>ema200
            sila=1+int(eok)+int(tok)
            sl_p=round(poz-sl_dist,2); tp_p=round(cena+abs(cena-sl_p)*TP_R_MULT,2); pobj=lw
            if sila>=MIN_CONFLUENCJA and trend_w > 0 and not makro["silny_dolar"]:
                akcja="BUY"; opis=(f"🔥 LONG od wsparcia ${poz} [{lw['typ']}]"
                                   +(" + EMA 50" if eok else "")+(" + trend" if tok else "")
                                   +f" | {trend_w_opis}")
            elif sila>=2:
                akcja="CAUTION_BUY"
                opis=(f"⚠️ OSTROŻNY LONG od ${poz} [{lw['typ']}] "
                      f"(confluencja: {sila}/3 | {trend_w_opis})")

    if akcja=="HOLD":
        lo=najblizszy(all_lvl,cena,"opor",promien)
        if lo:
            poz=lo["cena"]; dot=hc>=poz*0.998 or hp>=poz*0.998; odj=cena<poz
            if dot and odj:
                eok=abs(ema50-poz)<=EMA_CONF_TOLERANCE*atr; tok=cena<ema200
                sila=1+int(eok)+int(tok)
                sl_p=round(poz+sl_dist,2); tp_p=round(cena-abs(sl_p-cena)*TP_R_MULT,2); pobj=lo
                if sila>=MIN_CONFLUENCJA and trend_w < 0 and makro["silny_dolar"]:
                    akcja="SELL"; opis=(f"📉 SHORT od oporu ${poz} [{lo['typ']}]"
                                        +(" + EMA 50" if eok else "")+(" + trend" if tok else "")
                                        +f" | {trend_w_opis}")
                elif sila>=2:
                    akcja="CAUTION_BUY"
                    opis=(f"⚠️ OSTROŻNY SHORT od ${poz} [{lo['typ']}] "
                          f"(confluencja: {sila}/3 | {trend_w_opis})")

    if akcja in ("BUY","SELL") and pobj:
        wyslij_email(nazwa,akcja,cena,sl_p,tp_p,makro["trend"],pobj["cena"],sila)
        dodaj_symulacje(nazwa,"LONG" if akcja=="BUY" else "SHORT",
                        cena,sl_p,tp_p,f"{pobj['typ']} @ ${pobj['cena']}")

    rr=round(abs(tp_p-cena)/max(abs(cena-sl_p),0.01),2)
    historia=[{"data":idx.strftime("%Y-%m-%d %H:%M"),
               "open":round(float(r["Open"]),2),"high":round(float(r["High"]),2),
               "low":round(float(r["Low"]),2),  "close":round(float(r["Close"]),2),
               "ema_200":round(float(r["EMA200"]),2),"ema_50":round(float(r["EMA50"]),2)}
              for idx,r in df.tail(120).iterrows()]
    poz_zasiegu=sorted([l for l in all_lvl if abs(l["cena"]-cena)/cena<0.03],key=lambda x:x["cena"])

    return {"kruszec":nazwa,"aktualna_cena_usd":cena,"wskaźnik_rsi":rsi,
            "ema_200":round(ema200,2),"ema_50":round(ema50,2),"zmienność_atr":round(atr,2),
            "trend_glowny":"WZROSTOWY 🟢" if cena>ema200 else "SPADKOWY 🔴",
            "kierunek_ema200": trend_w_opis,
            "makro_dxy":makro,"rekomendacja":akcja,"opis_sygnalu":opis,
            "sila_sygnalu":sila,"poziomy_strukturalne":poz_zasiegu,"najblizszy_poziom":pobj,
            "zarządzanie_ryzykiem":{"proponowany_stop_loss_usd":sl_p,
                                    "proponowany_take_profit_usd":tp_p,
                                    "stosunek_zysk_ryzyko":f"{rr}:1"},
            "historia_cen":historia}


# ─── Endpoint: Backtest ───────────────────────────────────────────

@app.get("/backtest/{metal}")
def wykonaj_backtest(metal: str, okres: str="2y",
                      kapital_startowy: float=10000.0,
                      sl_atr: float=SL_ATR_MULT,
                      tp_r:   float=TP_R_MULT) -> dict:
    ticker = "SI=F" if metal.lower()=="srebro" else "GC=F"
    nazwa  = ("Srebro (COMEX Silver Futures)" if metal.lower()=="srebro"
              else "Złoto (COMEX Gold Futures)")

    df=_fix(yf.Ticker(ticker).history(period=okres,interval="1h"))
    if df.empty or len(df)<250: return {"błąd":"Zbyt mało danych."}

    df["EMA50"] =df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"]=df["Close"].ewm(span=200,adjust=False).mean()
    df["ATR"]   =oblicz_atr(df)

    sh_all,sl_all=wykryj_swing_levels(df,lookback=20)
    pdh_s,pdl_s,pwh_s,pwl_s=oblicz_pdh_pdl_pwh_pwl(df)
    pdh=pdh_s.to_numpy(float); pdl=pdl_s.to_numpy(float)
    pwh=pwh_s.to_numpy(float); pwl=pwl_s.to_numpy(float)

    # Kierunek EMA200 w horyzoncie 1 tygodnia (N_SLOPE_BARS barów 1H)
    # +1=wzrostowy → tylko LONG, -1=spadkowy → tylko SHORT
    ema200_np = df["EMA200"].to_numpy()
    trend_w_arr = np.zeros(len(df))
    for _j in range(N_SLOPE_BARS, len(df)):
        trend_w_arr[_j] = 1.0 if ema200_np[_j] > ema200_np[_j - N_SLOPE_BARS] else -1.0

    kapital=kapital_startowy
    hist_kap=[{"data":df.index[220].strftime("%Y-%m-%d"),"kapital":kapital}]
    poz=None; cent=sl=tp=wiel=0.0
    wygrane=przegrane=0; transakcje=[]; miesiace={}; WARMUP=220

    for i in range(WARMUP,len(df)):
        row=df.iloc[i]
        cena=float(row["Close"]); high=float(row["High"]); low=float(row["Low"])
        e50=float(row["EMA50"]); e200=float(row["EMA200"]); atr=float(row["ATR"])
        ds=df.index[i].strftime("%Y-%m-%d %H:%M"); ms=df.index[i].strftime("%Y-%m")
        if np.isnan(atr) or atr<=0: continue
        if ms not in miesiace: miesiace[ms]={"pnl":0.0,"n":0}

        # Zamknięcie pozycji
        if poz=="LONG":
            if low<=sl:
                pnl=(sl-cent)*wiel; kp=kapital; kapital+=pnl; przegrane+=1; poz=None
                transakcje.append({"data":ds,"typ":"LONG","wynik":"❌ SL",
                                    "pnl":round(pnl,2),"cena_exit":round(sl,2),
                                    "kapital_przed":round(kp,2),"kapital_po":round(kapital,2)})
                miesiace[ms]["pnl"]+=pnl; miesiace[ms]["n"]+=1
            elif high>=tp:
                pnl=(tp-cent)*wiel; kp=kapital; kapital+=pnl; wygrane+=1; poz=None
                transakcje.append({"data":ds,"typ":"LONG","wynik":"🟢 TP",
                                    "pnl":round(pnl,2),"cena_exit":round(tp,2),
                                    "kapital_przed":round(kp,2),"kapital_po":round(kapital,2)})
                miesiace[ms]["pnl"]+=pnl; miesiace[ms]["n"]+=1
        elif poz=="SHORT":
            if high>=sl:
                pnl=-(sl-cent)*wiel; kp=kapital; kapital+=pnl; przegrane+=1; poz=None
                transakcje.append({"data":ds,"typ":"SHORT","wynik":"❌ SL",
                                    "pnl":round(pnl,2),"cena_exit":round(sl,2),
                                    "kapital_przed":round(kp,2),"kapital_po":round(kapital,2)})
                miesiace[ms]["pnl"]+=pnl; miesiace[ms]["n"]+=1
            elif low<=tp:
                pnl=(cent-tp)*wiel; kp=kapital; kapital+=pnl; wygrane+=1; poz=None
                transakcje.append({"data":ds,"typ":"SHORT","wynik":"🟢 TP",
                                    "pnl":round(pnl,2),"cena_exit":round(tp,2),
                                    "kapital_przed":round(kp,2),"kapital_po":round(kapital,2)})
                miesiace[ms]["pnl"]+=pnl; miesiace[ms]["n"]+=1

        # Nowy sygnał
        if poz is None:
            lvls=zbierz_poziomy(i,sh_all,sl_all,pdh,pdl,pwh,pwl,cena)
            sld=sl_atr*atr; prom=APPROACH_THRESHOLD*atr; etol=EMA_CONF_TOLERANCE*atr
            lp2=float(df.iloc[i-1]["Low"]); hp2=float(df.iloc[i-1]["High"])
            trend_w = trend_w_arr[i]  # +1=wzrost, -1=spadek, 0=brak danych

            lw=najblizszy(lvls,cena,"wsparcie",prom)
            if lw and trend_w > 0:        # LONG tylko gdy EMA200 rośnie od tygodnia
                pz=lw["cena"]
                if (low<=pz*1.002 or lp2<=pz*1.002) and cena>pz:
                    eok=abs(e50-pz)<=etol; tok=cena>e200
                    if (1+int(eok)+int(tok))>=MIN_CONFLUENCJA:
                        sl_n=round(pz-sld,2); tp_n=round(cena+abs(cena-sl_n)*tp_r,2)
                        ry=kapital*RISK_PCT; wiel=ry/max(abs(cena-sl_n),0.01)
                        poz,cent,sl,tp="LONG",cena,sl_n,tp_n; continue

            lo=najblizszy(lvls,cena,"opor",prom)
            if lo and trend_w < 0:        # SHORT tylko gdy EMA200 spada od tygodnia
                pz=lo["cena"]
                if (high>=pz*0.998 or hp2>=pz*0.998) and cena<pz:
                    eok=abs(e50-pz)<=etol; tok=cena<e200
                    if (1+int(eok)+int(tok))>=MIN_CONFLUENCJA:
                        sl_n=round(pz+sld,2); tp_n=round(cena-abs(sl_n-cena)*tp_r,2)
                        ry=kapital*RISK_PCT; wiel=ry/max(abs(sl_n-cena),0.01)
                        poz,cent,sl,tp="SHORT",cena,sl_n,tp_n; continue

        if i%24==0 or i==len(df)-1:
            hist_kap.append({"data":ds,"kapital":round(kapital,2)})

    total=wygrane+przegrane
    rozklad=[{"miesiac":m,"n_trades":v["n"],"pnl_usd":round(v["pnl"],2),
               "pct":round(v["pnl"]/kapital_startowy*100,2)}
              for m,v in sorted(miesiace.items()) if v["n"]>0]
    rolling=_rolling_window_analysis(transakcje,df)

    return {"kruszec":nazwa,"parametry":{"sl_atr":sl_atr,"tp_r":tp_r},
            "kapital_poczatkowy":kapital_startowy,"kapital_koncowy":round(kapital,2),
            "calkowity_zysk_usd":round(kapital-kapital_startowy,2),
            "zwrot_procentowy":round((kapital/kapital_startowy-1)*100,2),
            "laczna_liczba_transakcji":total,"wygrane":wygrane,"przegrane":przegrane,
            "win_rate":round(wygrane/total*100,1) if total>0 else 0.0,
            "historia_kapitalu":hist_kap,"rozklad_miesieczny":rozklad,
            "rolling_window":rolling,"lista_transakcji":transakcje[::-1]}


# ─── Endpoint: Symulacje ──────────────────────────────────────────

@app.get("/pobierz_symulacje")
def pobierz_symulacje_api() -> list:
    sym=wczytaj_symulacje()
    for s in sym:
        if s["status"]!="OTWARTA 🟢": continue
        ticker="SI=F" if "Srebro" in s["kruszec"] else "GC=F"
        try:
            akt=float(_fix(yf.Ticker(ticker).history(period="1d"))["Close"].iloc[-1])
            if s["typ"]=="LONG":
                if   akt<=s["stop_loss"]:  s["status"],s["wynik_usd"]="STRATA ❌",round(s["stop_loss"]  -s["cena_wejscia"],2)
                elif akt>=s["take_profit"]: s["status"],s["wynik_usd"]="ZYSK 🟢", round(s["take_profit"]-s["cena_wejscia"],2)
                else:                       s["wynik_usd"]=round(akt-s["cena_wejscia"],2)
            else:
                if   akt>=s["stop_loss"]:  s["status"],s["wynik_usd"]="STRATA ❌",round(s["cena_wejscia"]-s["stop_loss"],  2)
                elif akt<=s["take_profit"]: s["status"],s["wynik_usd"]="ZYSK 🟢", round(s["cena_wejscia"]-s["take_profit"],2)
                else:                       s["wynik_usd"]=round(s["cena_wejscia"]-akt,2)
        except Exception: pass
    zapisz_symulacje(sym)
    return sym


# ─── Scheduler ────────────────────────────────────────────────────

def praca_robota():
    try: generuj_sygnal("zloto")
    except Exception as e: print(f"[Scheduler] {e}")

scheduler=BackgroundScheduler()
scheduler.add_job(praca_robota,"interval",minutes=60)
scheduler.start()

if __name__=="__main__":
    uvicorn.run(app,host="127.0.0.1",port=8000)
