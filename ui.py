"""
ui.py — Gold AI Trader | Streamlit Frontend
Odczytuje dane bezpośrednio z main.py (bez HTTP).
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from main import generuj_sygnal, wykonaj_backtest, pobierz_symulacje_api

st.set_page_config(
    page_title="Gold AI Trader",
    layout="wide",
    page_icon="🥇",
    initial_sidebar_state="collapsed"
)

# ── nagłówek ─────────────────────────────────────────────────────
h1, h2 = st.columns([4, 1])
with h1:
    st.title("🥇 Gold AI Trader — Structural Levels + EMA Confluencja")
    st.caption("Strategia: PDH/PDL · PWH/PWL · Round numbers · Swing H/L · EMA 50 konfluencja · DXY filtr")
with h2:
    st.write("")
    st.success("🤖 BOT 24/7: AKTYWNY")
st.divider()

instrument = st.selectbox("Instrument:", ("Złoto (XAU/USD)", "Srebro (XAG/USD)"))
url_param  = "zloto" if "Złoto" in instrument else "srebro"

tab1, tab2, tab3 = st.tabs(["🚨 Terminal na Żywo", "📊 Backtest", "💼 Dziennik Symulacji"])


# ═══════════════════════════════════════════════════════════════════
# ZAKŁADKA 1: LIVE TERMINAL
# ═══════════════════════════════════════════════════════════════════
with tab1:
    try:
        with st.spinner(f"Analizuję strukturę rynku — {instrument}..."):
            dane = generuj_sygnal(url_param)
        if "błąd" in dane:
            st.error(dane["błąd"]); st.stop()

        rek  = dane["rekomendacja"]
        sila = dane.get("sila_sygnalu", 0)

        # ── metryki ──────────────────────────────────────────────
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            st.metric("📍 Aktualna Cena", f"${dane['aktualna_cena_usd']:,.2f}")
            st.metric("📈 Trend (EMA 200)", dane["trend_glowny"])
        with c2:
            st.metric("EMA 200", f"${dane['ema_200']:,.2f}")
            st.metric("EMA 50",  f"${dane['ema_50']:,.2f}")
        with c3:
            rsi = dane["wskaźnik_rsi"]
            rsi_opis = "🔴 Wykupienie" if rsi > 70 else ("🟢 Wyprzedanie" if rsi < 30 else "⚪ Neutralne")
            st.metric("RSI (14)", f"{rsi}", rsi_opis)
            st.metric("ATR (14)", f"${dane['zmienność_atr']:,.2f}")
        with c4:
            gwiazdki = ("⭐" * sila + "☆" * (3 - sila)) if sila else "—"
            st.metric("Siła Sygnału", gwiazdki, f"{sila}/3 czynniki")
            # Kierunek EMA200 — nowy filtr trendowy
            kier_ema = dane.get("kierunek_ema200", "—")
            st.metric("Kierunek EMA200 (7d)", kier_ema)
        with c5:
            ryzyko = dane["zarządzanie_ryzykiem"]
            st.info(f"🛑 **SL:** ${ryzyko['proponowany_stop_loss_usd']:,.2f}")
            st.info(f"🎯 **TP:** ${ryzyko['proponowany_take_profit_usd']:,.2f}")
            st.info(f"⚖️ **R:R:** {ryzyko['stosunek_zysk_ryzyko']}")

        st.divider()

        # ── makro DXY ────────────────────────────────────────────
        md1, md2 = st.columns([1, 3])
        with md1:
            st.metric("🌍 DXY (Indeks Dolara)", f"{dane['makro_dxy']['cena']} pkt")
        with md2:
            st.info(f"**Wpływ na złoto:** {dane['makro_dxy']['trend']}")

        st.divider()

        # ── sygnał ───────────────────────────────────────────────
        if   rek == "BUY":         st.success(f"🤖 **SYGNAŁ:** {dane['opis_sygnalu']}")
        elif rek == "SELL":        st.error(  f"🤖 **SYGNAŁ:** {dane['opis_sygnalu']}")
        elif rek == "CAUTION_BUY": st.warning(f"🤖 **SYGNAŁ:** {dane['opis_sygnalu']}")
        else:                      st.info(   f"🤖 **SYGNAŁ:** {dane['opis_sygnalu']}")

        st.divider()

        # ── wykres świecowy ──────────────────────────────────────
        st.subheader(f"📈 Wykres — {instrument}")
        hist = dane["historia_cen"]
        cena_akt = dane["aktualna_cena_usd"]

        fig = go.Figure()

        # Świece
        fig.add_trace(go.Candlestick(
            x=[h["data"] for h in hist],
            open=[h["open"] for h in hist], high=[h["high"] for h in hist],
            low=[h["low"]  for h in hist],  close=[h["close"] for h in hist],
            name=instrument,
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a",  decreasing_fillcolor="#ef5350",
        ))

        # EMA 200
        fig.add_trace(go.Scatter(
            x=[h["data"] for h in hist], y=[h["ema_200"] for h in hist],
            mode="lines", name="EMA 200",
            line=dict(color="#FFD700", width=2)
        ))

        # EMA 50
        if "ema_50" in hist[0]:
            fig.add_trace(go.Scatter(
                x=[h["data"] for h in hist], y=[h["ema_50"] for h in hist],
                mode="lines", name="EMA 50",
                line=dict(color="#FFA500", width=1.5, dash="dash")
            ))

        # SL i TP
        fig.add_hline(y=ryzyko["proponowany_take_profit_usd"],
                      line_dash="dash", line_color="#00C853", line_width=1.5,
                      annotation_text="🎯 TP", annotation_position="right",
                      annotation_font_color="#00C853")
        fig.add_hline(y=ryzyko["proponowany_stop_loss_usd"],
                      line_dash="dash", line_color="#FF1744", line_width=1.5,
                      annotation_text="🛑 SL", annotation_position="right",
                      annotation_font_color="#FF1744")

        # Poziomy strukturalne (te w zasięgu ±3% ceny)
        KOLOR_POZIOMU = {
            "PDH": "cyan", "PDL": "cyan",
            "PWH": "#AA80FF", "PWL": "#AA80FF",
            "Swing High": "#FF8C69", "Swing Low": "#90EE90",
            "Round": "#888888",
        }
        for p in dane.get("poziomy_strukturalne", []):
            kolor = next(
                (v for k, v in KOLOR_POZIOMU.items() if k in p["typ"]),
                "#888888"
            )
            fig.add_hline(
                y=p["cena"], line_dash="dot", line_color=kolor, line_width=1,
                annotation_text=f"  {p['typ']} ${p['cena']:,.0f}",
                annotation_font_color=kolor, annotation_position="left"
            )

        fig.update_layout(
            xaxis_rangeslider_visible=False,
            height=620, template="plotly_dark",
            yaxis_title="Cena (USD/oz)",
            margin=dict(l=80, r=140, t=30, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.01)
        )
        st.plotly_chart(fig, use_container_width=True)

        # ── tabela poziomów ───────────────────────────────────────
        poziomy = dane.get("poziomy_strukturalne", [])
        if poziomy:
            with st.expander(f"📋 Wszystkie poziomy strukturalne w zasięgu ({len(poziomy)} szt.)"):
                df_poz = pd.DataFrame(poziomy)
                df_poz["odległość ($)"] = (df_poz["cena"] - cena_akt).round(2)
                df_poz["kierunek"]      = df_poz["cena"].apply(
                    lambda x: "⬆️ Opór" if x > cena_akt else "⬇️ Wsparcie"
                )
                st.dataframe(
                    df_poz.rename(columns={"cena": "Poziom ($)", "typ": "Typ"})
                          .sort_values("Poziom ($)", ascending=False),
                    use_container_width=True, hide_index=True
                )

    except Exception as e:
        st.error(f"Błąd w zakładce Live: {e}")
        st.exception(e)


# ═══════════════════════════════════════════════════════════════════
# ZAKŁADKA 2: BACKTEST
# ═══════════════════════════════════════════════════════════════════
with tab2:
    st.subheader(f"⏳ Backtester — {instrument}")
    st.caption("Strategia: Poziomy strukturalne + EMA 50 confluencja · 5% ryzyka na trade z kompoundowaniem")

    p1, p2, p3, p4, p5 = st.columns(5)
    with p1:
        okres    = st.selectbox("Okres testu:", ("2y", "1y", "6mo"))
    with p2:
        kapital  = st.number_input("Kapitał startowy ($):", 1000.0, 1_000_000.0, 10000.0, 1000.0)
    with p3:
        sl_atr_v = st.selectbox("SL (× ATR):", (1.2, 1.5, 1.8, 2.0), index=1)
    with p4:
        tp_r_v   = st.selectbox("TP (R-Multiple):", (1.0, 1.5, 2.0, 3.0, 4.0, 5.0), index=2)
    with p5:
        st.write(""); st.write("")
        run_bt = st.button("🚀 URUCHOM", use_container_width=True, type="primary")

    if run_bt:
        with st.spinner("Symulacja w toku — może potrwać 30-90 sekund..."):
            wyniki = wykonaj_backtest(url_param, okres=okres,
                                       kapital_startowy=kapital,
                                       sl_atr=sl_atr_v, tp_r=tp_r_v)
        if "błąd" in wyniki:
            st.error(wyniki["błąd"])
        else:
            st.success(f"✅ Zakończono! SL = {sl_atr_v}×ATR | TP = {tp_r_v}R | "
                        f"Okres: {okres}")

            # ── metryki wynikowe ──────────────────────────────────
            k1, k2, k3, k4, k5 = st.columns(5)
            zysk = wyniki["calkowity_zysk_usd"]
            with k1:
                st.metric("Kapitał Końcowy", f"${wyniki['kapital_koncowy']:,.2f}",
                          f"{wyniki['zwrot_procentowy']:+.2f}%")
            with k2:
                st.metric("Zysk / Strata", f"${zysk:,.2f}")
            with k3:
                st.metric("Win Rate", f"{wyniki['win_rate']}%",
                          f"{wyniki['wygrane']}W / {wyniki['przegrane']}L")
            with k4:
                st.metric("Liczba transakcji", wyniki["laczna_liczba_transakcji"])
            with k5:
                roz = wyniki.get("rozklad_miesieczny", [])
                n_m = len(roz) or 1
                avg_m = wyniki["zwrot_procentowy"] / n_m
                st.metric("Śr. miesięczny zwrot", f"{avg_m:+.2f}%")

            st.divider()

            # ── equity curve ──────────────────────────────────────
            st.subheader("📈 Krzywa Kapitału (Equity Curve)")
            df_eq = pd.DataFrame(wyniki["historia_kapitalu"])
            kolor = "#00C853" if zysk >= 0 else "#FF1744"

            fig_eq = go.Figure()
            fig_eq.add_trace(go.Scatter(
                x=df_eq["data"], y=df_eq["kapital"],
                mode="lines", name="Portfel ($)",
                fill="tozeroy",
                fillcolor=("rgba(0,200,83,0.08)" if zysk >= 0 else "rgba(255,23,68,0.08)"),
                line=dict(color=kolor, width=2.5)
            ))
            fig_eq.add_hline(y=kapital, line_dash="dash", line_color="#888888",
                             annotation_text="Kapitał startowy", annotation_font_color="#888888")
            fig_eq.update_layout(height=380, template="plotly_dark",
                                  yaxis_title="Kapitał ($)",
                                  margin=dict(l=20, r=20, t=20, b=20))
            st.plotly_chart(fig_eq, use_container_width=True)

            # ── rozkład miesięczny ────────────────────────────────
            roz = wyniki.get("rozklad_miesieczny", [])
            if roz:
                st.subheader("📅 Rozkład miesięczny")
                df_m = pd.DataFrame(roz)

                fig_m = go.Figure(go.Bar(
                    x=df_m["miesiac"], y=df_m["pnl_usd"],
                    marker_color=["#26a69a" if v >= 0 else "#ef5350"
                                  for v in df_m["pnl_usd"]],
                    name="P&L miesięczny ($)"
                ))
                fig_m.update_layout(height=280, template="plotly_dark",
                                     yaxis_title="P&L ($)",
                                     margin=dict(l=20, r=20, t=20, b=20))
                st.plotly_chart(fig_m, use_container_width=True)

                df_m_show = df_m.copy()
                df_m_show.columns = ["Miesiąc", "Transakcji", "P&L ($)", "Zwrot (%)"]
                st.dataframe(df_m_show, use_container_width=True, hide_index=True)

            # ── Rolling Window Analysis ───────────────────────────
            rw = wyniki.get("rolling_window", {})
            kwartalnie = rw.get("kwartalnie", [])
            if kwartalnie:
                st.divider()
                st.subheader("🔬 Rolling Window Analysis — wyniki per kwartał")
                st.caption("To jest kluczowa analiza: pokazuje w jakich warunkach rynkowych "
                           "strategia zarabia, a w jakich traci. Niezbędna przed realnym kapitałem.")

                # Wniosek kluczowy — najważniejsza informacja
                wniosek = rw.get("wniosek", "")
                st.info(f"💡 **Wniosek:** {wniosek}")

                # 3 metryki reżymowe obok siebie
                stats = rw.get("statystyki", {})
                r1, r2, r3 = st.columns(3)
                def _rezym_metric(col, label, emoji, tag):
                    s = stats.get(tag, {})
                    avg = s.get("avg_pnl")
                    n   = s.get("n", 0)
                    if avg is not None and n > 0:
                        kol = "normal" if avg >= 0 else "inverse"
                        col.metric(f"{emoji} {label}", f"{avg:+.2f}$/kw.", f"{n} kwartałów")
                    else:
                        col.metric(f"{emoji} {label}", "Brak danych", f"0 kwartałów")
                _rezym_metric(r1, "Trend wzrostowy", "📈", "wzrostowy")
                _rezym_metric(r2, "Trend spadkowy",  "📉", "spadkowy")
                _rezym_metric(r3, "Rynek boczny",    "↔️", "boczny")

                st.write("")

                # Wykres kwartalny — kolory wg reżymu rynku
                df_rw = pd.DataFrame(kwartalnie)

                def _kolor_baru(rezym: str) -> str:
                    if "Wzrostowy" in rezym and "silny" in rezym:  return "#00C853"
                    if "Wzrostowy" in rezym:                        return "#69F0AE"
                    if "Spadkowy"  in rezym and "silny" in rezym:  return "#FF1744"
                    if "Spadkowy"  in rezym:                        return "#FF6D00"
                    return "#888888"  # boczny

                kolory_q = [_kolor_baru(r) for r in df_rw["rezym"]]
                symbole  = ["▲" if v >= 0 else "▼" for v in df_rw["pnl_usd"]]

                fig_rw = go.Figure()
                fig_rw.add_trace(go.Bar(
                    x=df_rw["kwartal"],
                    y=df_rw["pnl_usd"],
                    marker_color=kolory_q,
                    text=[f"{s} ${abs(v):,.0f}" for s, v in zip(symbole, df_rw["pnl_usd"])],
                    textposition="outside",
                    customdata=df_rw[["n_trades", "win_rate", "rezym"]].values,
                    hovertemplate=(
                        "<b>%{x}</b><br>"
                        "P&L: $%{y:,.2f}<br>"
                        "Transakcji: %{customdata[0]}<br>"
                        "Win Rate: %{customdata[1]}%<br>"
                        "Reżym: %{customdata[2]}<extra></extra>"
                    ),
                    name="P&L kwartalny ($)"
                ))
                fig_rw.add_hline(y=0, line_color="#555555", line_width=1)
                fig_rw.update_layout(
                    height=350, template="plotly_dark",
                    yaxis_title="P&L ($)",
                    margin=dict(l=20, r=20, t=30, b=20),
                    title=dict(
                        text="<span style='font-size:11px;color:#888'>🟢 Silny wzrost &nbsp; "
                             "🟩 Umiarkowany wzrost &nbsp; 🟠 Trend spadkowy &nbsp; "
                             "⬜ Rynek boczny</span>",
                        x=0.01, xanchor="left"
                    )
                )
                st.plotly_chart(fig_rw, use_container_width=True)

                # Tabela kwartalna z pełnymi danymi
                df_rw_show = df_rw[["kwartal", "n_trades", "win_rate",
                                     "pnl_usd", "pct_change",
                                     "kapital_start", "kapital_end", "rezym"]].copy()
                df_rw_show.columns = ["Kwartał", "Transakcji", "Win Rate (%)",
                                       "P&L ($)", "Zmiana (%)",
                                       "Kapitał start ($)", "Kapitał end ($)", "Reżym rynku"]
                st.dataframe(df_rw_show, use_container_width=True, hide_index=True)

                # Minianaliza: kiedy strategia NIE działa
                przegrane_q = [w for w in kwartalnie if w["pnl_usd"] < 0]
                if przegrane_q:
                    rezym_strat = [w["rezym"] for w in przegrane_q]
                    with st.expander(f"🔴 Analiza {len(przegrane_q)} stratnych kwartałów"):
                        for w in przegrane_q:
                            st.markdown(
                                f"**{w['kwartal']}**: {w['pnl_usd']:+.2f}$ | "
                                f"Win Rate: {w['win_rate']}% | "
                                f"Reżym: {w['rezym']}"
                            )
                        st.caption("👆 Jeśli większość strat przypada na rynek boczny (↔️), "
                                   "warto dodać filtr trendu (np. tylko trade gdy EMA 200 ma "
                                   "wzrostowy kierunek na interwale tygodniowym).")

            # ── lista transakcji ──────────────────────────────────
            with st.expander(f"📜 Lista wszystkich transakcji ({wyniki['laczna_liczba_transakcji']})"):
                lst = wyniki.get("lista_transakcji", [])
                if lst:
                    df_t = pd.DataFrame(lst)
                    st.dataframe(df_t, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════
# ZAKŁADKA 3: DZIENNIK SYMULACJI
# ═══════════════════════════════════════════════════════════════════
with tab3:
    st.subheader("💼 Dziennik Symulacji na Żywo (Forward Test)")
    st.markdown("Bot zapisuje tutaj każdą pozycję według strategii strukturalnej. "
                "Status aktualizuje się przy każdym odświeżeniu.")

    if st.button("🔄 Odśwież status pozycji", type="primary"):
        st.rerun()

    sym = pobierz_symulacje_api()
    if sym:
        df_sym = pd.DataFrame(sym)

        # Podsumowanie statystyk
        s1, s2, s3, s4 = st.columns(4)
        otwarte = sum(1 for s in sym if "OTWARTA" in s["status"])
        zyski   = sum(1 for s in sym if "ZYSK"    in s["status"])
        straty  = sum(1 for s in sym if "STRATA"  in s["status"])
        total_pnl = sum(s.get("wynik_usd", 0) for s in sym)
        s1.metric("Łącznie pozycji", len(sym))
        s2.metric("Otwarte 🟢",      otwarte)
        s3.metric("Zamknięte z zyskiem ✅", zyski)
        s4.metric("Łączny wynik P&L", f"${total_pnl:,.2f}")

        st.divider()
        st.dataframe(df_sym, use_container_width=True, hide_index=True)
    else:
        st.info("Brak zapisanych symulacji. "
                "Bot zapisze pierwszą pozycję gdy wykryje sygnał strukturalny "
                "(wymagana siła ≥ 3/3: poziom + EMA 50 + trend tygodniowy).")
