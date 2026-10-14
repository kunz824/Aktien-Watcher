"""
aktien_watcher.py  --  Aktien-Watcher fuer Aktien und Indizes (ausserhalb MT5/Libertex)

Startet einen lokalen Webserver mit der App (aktien-watcher.html) und
  * ueberwacht SMI, DAX, S&P 500 (Index-Alarm, im Backtest validiert),
  * findet Einzeltitel mit Kurseinbruch (Tag/Woche/Monat) in SMI/DAX/S&P 500,
  * bewertet sie mit Kriterien (Wachstum, Gewinn, Bilanz, Bewertung, Analysten,
    Innovation, Nachrichten) -> Ampel gruen/gelb/rot,
  * verfolgt dein Depot (Kurs, Veraenderung in %, Kontouebersicht).
Datenquelle: Yahoo Finance via yfinance (kostenlos, kein Schluessel).

Aufruf:
    python aktien_watcher.py              (Server nur lokal: http://localhost:8765)
    python aktien_watcher.py --lan        (auch im Heimnetz / Tailscale erreichbar)
    python aktien_watcher.py --demo       (Testdaten ohne Internet)

WICHTIG: Die Ampel ist eine Entscheidungshilfe, keine Anlageberatung. Im Backtest
validiert ist nur der Index-Alarm; Einzeltitel-Kriterien sind NICHT als Gewinnquelle
belegt (siehe Tab "Info" in der App).
"""
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HIER = os.path.dirname(os.path.abspath(__file__))
DATEN_DIR = os.path.join(HIER, "daten_aktien")
DATEI_DATEN = os.path.join(HIER, "markt_daten.json")
DATEI_DEPOT = os.path.join(HIER, "depot.json")
CACHE_KURSE = os.path.join(DATEN_DIR, "kurse.csv")
CACHE_MARKT = os.path.join(DATEN_DIR, "markt.csv")
CACHE_FUND = os.path.join(DATEN_DIR, "fundamentals")
PORT = 8765
DEMO = "--demo" in sys.argv
LAN = "--lan" in sys.argv

FENSTER_TAGE = 10          # Einbruch gilt als "aktuell", wenn in den letzten 10 Handelstagen ausgeloest
MAX_KANDIDATEN = 60
FUND_GUELTIG_STD = 20
TRIGGER = {"Tag -5%": (1, -0.05), "Woche -10%": (5, -0.10), "Monat -20%": (21, -0.20)}
IDX_TRIGGER = {"Tag -2.5%": (1, -0.025), "Woche -4%": (5, -0.04), "Monat -8%": (21, -0.08)}
INDIZES = {"^GSPC": "S&P 500", "^SSMI": "SMI", "^GDAXI": "DAX"}

# Ergebnis des Backtests (Hold-out, 3 Monate nach Signal, nach Kosten):
# Trefferquote = Anteil positiver 3-Monats-Renditen; Vorsprung = Mehrertrag gegenueber Durchschnittstag.
INDEX_STATS = {
    "S&P 500": {"Tag -2.5%": (0.76, 3.1, 25), "Woche -4%": (0.75, 1.7, 24), "Monat -8%": (0.69, 3.6, 13)},
    "SMI":     {"Tag -2.5%": (0.73, 0.5, 15), "Woche -4%": (0.71, 0.7, 21), "Monat -8%": (0.91, 3.2, 11)},
    "DAX":     {"Tag -2.5%": (0.74, 2.1, 31), "Woche -4%": (0.66, 1.5, 35), "Monat -8%": (0.77, 5.9, 13)},
}

SMI = ["ABBN", "ALC", "CFR", "GEBN", "GIVN", "HOLN", "KNIN", "LOGN", "LONN", "NESN",
       "NOVN", "PGHN", "ROG", "SCMN", "SIKA", "SLHN", "SOON", "SREN", "UBSG", "ZURN"]
DAX = ["ADS", "AIR", "ALV", "BAS", "BAYN", "BEI", "BMW", "BNR", "CBK", "CON", "1COV", "DTG",
       "DBK", "DB1", "DHL", "DTE", "EOAN", "FME", "FRE", "HNR1", "HEI", "HEN3", "IFX", "MBG",
       "MRK", "MTX", "MUV2", "PAH3", "QIA", "RHM", "RWE", "SAP", "SRT3", "SIE", "ENR", "SHL",
       "SY1", "VOW3", "VNA", "ZAL"]

POS_WOERTER = ["launch", "unveil", "approval", "approves", "approved", "partnership", "record", "raises guidance",
               "raised guidance", "beats", "beat estimates", "new product", "breakthrough", "wins", "contract",
               "expands", "upgrade", "buyback", "innovation", "introduces"]
NEG_WOERTER = ["warns", "profit warning", "cuts guidance", "lowers guidance", "fraud", "probe", "investigation",
               "recall", "lawsuit", "sues", "downgrade", "misses", "miss estimates", "layoffs", "bankruptcy",
               "default", "short seller", "subpoena", "delay"]
KRITISCH_NEG = ["profit warning", "warns", "cuts guidance", "lowers guidance", "fraud", "probe", "investigation",
                "recall", "bankruptcy", "default", "short seller", "subpoena"]
# 2026-10-14: Ursachen-Themen aus Schlagzeilen (Stichwortsuche, EN + DE). Keine inhaltliche Zusammenfassung, nur Einordnung.
THEMEN = [
    ("Klage / Schadenersatz", ["lawsuit", "sued", "sues", "class action", "class-action", "litigation", "settlement", "verdict", "damages",
                               "court", "klage", "schadenersatz", "prozess", "vergleich", "urteil", "gericht"]),
    ("Kartell / Regulierung", ["antitrust", "cartel", "competition", "monopoly", "regulator", "fined", "fine of", "ftc", "doj", "european commission",
                               "eu commission", "ban ", "banned", "bans ", "sanction", "kartell", "wettbewerb", "busse", "behörde", "aufsicht", "strafe"]),
    ("Untersuchung / Betrugsvorwurf", ["investigation", "probe", "subpoena", "sec ", "fraud", "short seller", "accounting", "whistleblower",
                                       "untersuchung", "ermittl", "betrug", "bilanzfälsch"]),
    ("Gewinnwarnung / Prognose gesenkt", ["profit warning", "warns", "warning", "cuts guidance", "lowers guidance", "cuts outlook", "lowers outlook",
                                          "weak guidance", "guidance cut", "gewinnwarnung", "senkt prognose", "senkt ausblick", "prognose gesenkt"]),
    ("Zahlen enttäuschen", ["misses", "missed", "miss estimates", "below expectations", "disappoint", "earnings fall", "profit falls", "profit drops",
                            "revenue falls", "sales fall", "slump", "enttäusch", "verfehlt", "gewinneinbruch", "umsatzrückgang"]),
    ("Analysten stufen ab", ["downgrade", "downgrades", "cuts price target", "lowers price target", "price target cut", "abgestuft", "herabgestuft", "kursziel gesenkt"]),
    ("Management / Führung", ["ceo", "cfo", "resigns", "steps down", "stepping down", "departure", "ousted", "rücktritt", "abgang", "verwaltungsrat"]),
    ("Produkt / Zulassung / Rückruf", ["recall", "rückruf", "fda", "rejects", "complete response", "halts", "delay", "delays", "defect", "safety", "zulassung"]),
    ("Makro / Zölle / Zinsen", ["tariff", "tariffs", "trade war", "inflation", "interest rate", "rate hike", "recession", "zölle", "zinsen", "rezession"]),
    ("Kapital / Dividende / Übernahme", ["share offering", "dilution", "dividend cut", "suspends dividend", "capital increase", "takeover", "acquisition", "acquires",
                                         "kapitalerhöhung", "dividende gekürzt", "übernahme"]),
    ("Entlassungen / Restrukturierung", ["layoffs", "job cuts", "restructuring", "plant closure", "stellenabbau", "restrukturierung"]),
]


def ursachen_aus_news(news):
    """Schlagzeilen den Ursachen-Themen zuordnen. Rueckgabe: Liste nach Haeufigkeit {thema, n, titel, quelle, zeit, link}."""
    res = []
    for thema, woerter in THEMEN:
        treffer = [n for n in news if any(re.search(r"\b" + re.escape(w.strip()) + (r"\b" if w.endswith(" ") else ""), (n.get("titel") or "").lower())
                                         for w in woerter)]
        if treffer:
            n0 = treffer[0]
            res.append({"thema": thema, "n": len(treffer), "titel": n0.get("titel"), "quelle": n0.get("quelle"),
                        "zeit": n0.get("zeit"), "link": n0.get("link")})
    res.sort(key=lambda x: -x["n"])
    return res


KEIN_RD_SEKTOREN = {"Financial Services", "Utilities", "Real Estate", "Energy", "Consumer Defensive", "Basic Materials"}
FINANZ = {"Financial Services"}

# 2026-10-14: Gewichtung der Kriterien fuer den Score (0-100). Ampel bleibt regelbasiert, der Score sortiert das Ranking.
# Orientierung: Gewinn-/Umsatzprognosen und Schaetzungsrevisionen am staerksten, dann Bilanz/Marge/Bewertung;
# Stichwort-Nachrichten und Forschungsanteil nur schwach (grobe Naeherung). Nicht per Backtest belegt - Ermessen, in der Config aenderbar.
GEWICHTE = {"gewinn": 18, "umsatz": 14, "revision": 14, "bilanz": 12, "marge": 10, "bewertung": 10,
            "analysten": 8, "ursache": 8, "innovation": 3, "news": 3}
DATEI_KONFIG = os.path.join(HIER, "aktien_watcher_config.json")
KONFIG_VORLAGE = {
    "jsonbin_bin_id": "",
    "jsonbin_access_key": "",
    # 2026-10-14: gestaffelter Push (schont das JSONBin-Kontingent): Kernzeit dichter, sonst seltener
    "push_kern_min": 30,           # Schreiben nach JSONBin in der Kernzeit höchstens alle 30 Min
    "push_aussen_min": 60,         # ausserhalb der Kernzeit höchstens alle 60 Min
    "kernzeit": ["09:00", "18:00"],
    "schnell_intervall_min": 15,   # lokale Kursaktualisierung während der Börsenzeit
    "boersenzeit": ["08:30", "22:30"],
    "vollscan_zeiten": ["09:15", "12:30", "15:45", "18:00", "22:30"],
    "gewichte": GEWICHTE,
    "max_bewertet": 150,           # so viele Einbrueche (die staerksten) werden bewertet ...
    "max_kandidaten": 60,          # ... und davon die 60 mit dem besten Score angezeigt
    "max_kb": 90                   # Bin-Grösse begrenzen (Gratis-Konten haben ein Limit pro Bin)
}

lock = threading.RLock()
status = {"laeuft": False, "meldung": "bereit", "letzter_lauf": None, "fehler": None}


# ---------------------------------------------------------------- Hilfen
def jetzt():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def lade_json(pfad, default):
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def speichere_json(pfad, obj):
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, pfad)


def nz(x):
    try:
        if x is None:
            return None
        x = float(x)
        return None if x != x else x
    except Exception:
        return None


def rund(x, n=2):
    x = nz(x)
    return None if x is None else round(x, n)


def waehrung_von(ticker):
    if ticker.endswith(".SW"):
        return "CHF"
    if ticker.endswith(".DE"):
        return "EUR"
    return "USD"


# ---------------------------------------------------------------- Ticker / Kurse
def ticker_liste():
    import io
    import pandas as pd
    t = {f"{x}.SW": "CH" for x in SMI}
    t.update({f"{x}.DE": "DE" for x in DAX})
    try:
        import requests
        r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        tab = pd.read_html(io.StringIO(r.text))[0]
        for s in tab["Symbol"].astype(str):
            t[s.replace(".", "-")] = "US"
    except Exception as e:
        log("S&P-500-Liste nicht ladbar, nur SMI/DAX:", e)
    return t


def kurse_aktualisieren():
    """Cache (aus dem Backtest-Skript) laden und die letzten Wochen nachladen."""
    import pandas as pd
    import yfinance as yf
    os.makedirs(DATEN_DIR, exist_ok=True)
    if os.path.exists(CACHE_KURSE) and os.path.exists(CACHE_MARKT):
        k = pd.read_csv(CACHE_KURSE, index_col=0)
        # 2026-10-14: Cache robust einlesen (Kopfzeilen-Reste/Textzeilen im Index fuehrten zu "str < Timestamp")
        k.index = pd.to_datetime(k.index, errors="coerce")
        k = k[~k.index.isna()]
        k.index = k.index.tz_localize(None) if k.index.tz is not None else k.index
        k = k.apply(pd.to_numeric, errors="coerce")
        k = k[~k.index.duplicated(keep="last")]
        markt = pd.read_csv(CACHE_MARKT, index_col=0)["markt"]
        namen = list(k.columns)
        teile = []
        for i in range(0, len(namen), 80):
            blk = namen[i:i + 80]
            d = yf.download(blk, period="3mo", auto_adjust=True, progress=False, threads=False)["Close"]
            if hasattr(d, "to_frame") and getattr(d, "ndim", 2) == 1:
                d = d.to_frame(blk[0])
            teile.append(d)
        neu = pd.concat(teile, axis=1)
        neu.index = pd.to_datetime(neu.index, errors="coerce")
        if neu.index.tz is not None:
            neu.index = neu.index.tz_localize(None)
        neu = neu[~neu.index.isna()]
        neu = neu.loc[:, ~neu.columns.duplicated()]
        k = neu.combine_first(k).sort_index()
        k = k.loc[:, ~k.columns.duplicated()]
    else:
        t = ticker_liste()
        namen = list(t)
        teile = []
        for i in range(0, len(namen), 80):
            blk = namen[i:i + 80]
            log(f"Erstladen Kurse {i + 1}-{i + len(blk)} von {len(namen)}")
            d = yf.download(blk, start="2019-01-01", auto_adjust=True, progress=False, threads=False)["Close"]
            if getattr(d, "ndim", 2) == 1:
                d = d.to_frame(blk[0])
            teile.append(d)
        k = pd.concat(teile, axis=1).sort_index()
        k = k.loc[:, k.notna().sum() > 200]
        markt = pd.Series({c: t.get(c, "?") for c in k.columns}, name="markt")
    k.to_csv(CACHE_KURSE)
    pd.DataFrame({"markt": markt}).to_csv(CACHE_MARKT)
    return k, markt


# ---------------------------------------------------------------- Index-Alarm
def index_status():
    import yfinance as yf
    res = []
    for sym, name in INDIZES.items():
        try:
            c = yf.download(sym, period="2y", auto_adjust=True, progress=False)["Close"].squeeze().dropna()
            if len(c) < 60:
                continue
            kurs = float(c.iloc[-1])
            def r(f):
                return float(c.iloc[-1] / c.iloc[-1 - f] - 1)
            hoch = float(c.tail(252).max())
            ausgeloest = []
            for tn, (f, schw) in IDX_TRIGGER.items():
                rr = (c / c.shift(f) - 1).tail(3)
                if (rr <= schw).any():
                    st = INDEX_STATS.get(name, {}).get(tn)
                    ausgeloest.append({"trigger": tn, "stats": st})
            vh = kurs / hoch - 1
            stat = "ALARM" if ausgeloest else ("beobachten" if vh <= -0.05 else "ruhig")
            res.append({"name": name, "symbol": sym, "kurs": rund(kurs), "tag_pct": rund(r(1) * 100),
                        "woche_pct": rund(r(5) * 100), "monat_pct": rund(r(21) * 100),
                        "vom_hoch_pct": rund(vh * 100), "status": stat, "ausgeloest": ausgeloest,
                        "verlauf": [rund(x, 2) for x in c.tail(90).tolist()]})
        except Exception as e:
            log("Index", sym, "Fehler:", e)
    return res


# ---------------------------------------------------------------- Fundamentaldaten
def _google_news(name):
    """Google-News-RSS (kostenlos, ohne Schluessel). Fehler -> leere Liste."""
    out = []
    try:
        import requests
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        from urllib.parse import quote
        q = quote(f'"{name}" (stock OR shares OR Aktie) when:14d')
        r = requests.get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        for it in ET.fromstring(r.content).iter("item"):
            titel = (it.findtext("title") or "").strip()
            if not titel:
                continue
            quelle = (it.findtext("source") or "").strip()
            if quelle and titel.endswith(" - " + quelle):
                titel = titel[:-(len(quelle) + 3)]
            try:
                zeit = parsedate_to_datetime(it.findtext("pubDate")).strftime("%Y-%m-%d")
            except Exception:
                zeit = ""
            out.append({"titel": titel, "quelle": quelle, "zeit": zeit, "link": (it.findtext("link") or "").strip()})
            if len(out) >= 15:
                break
    except Exception:
        pass
    return out


def _news(t, name=None):
    out = []
    try:
        for n in (t.news or [])[:12]:
            c = n.get("content") if isinstance(n, dict) and "content" in n else n
            titel = c.get("title")
            if not titel:
                continue
            prov = c.get("provider")
            quelle = prov.get("displayName") if isinstance(prov, dict) else c.get("publisher", "")
            zeit = c.get("pubDate") or c.get("providerPublishTime") or ""
            if isinstance(zeit, (int, float)):
                zeit = dt.datetime.fromtimestamp(zeit).strftime("%Y-%m-%d")
            url = ""
            if isinstance(c.get("canonicalUrl"), dict):
                url = c["canonicalUrl"].get("url", "")
            url = url or c.get("link", "")
            out.append({"titel": titel, "quelle": quelle, "zeit": str(zeit)[:10], "link": url})
    except Exception:
        pass
    if name:
        gesehen = {n["titel"].lower()[:60] for n in out}
        for n in _google_news(name):
            if n["titel"].lower()[:60] not in gesehen:
                out.append(n)
                gesehen.add(n["titel"].lower()[:60])
    out.sort(key=lambda n: n.get("zeit") or "", reverse=True)
    return out[:20]


def hole_fundamentals(ticker):
    """Mit Datei-Cache (20 h). Rueckgabe: dict mit Rohwerten."""
    import yfinance as yf
    os.makedirs(CACHE_FUND, exist_ok=True)
    pfad = os.path.join(CACHE_FUND, ticker.replace("^", "_") + ".json")
    if os.path.exists(pfad) and (time.time() - os.path.getmtime(pfad)) < FUND_GUELTIG_STD * 3600:
        d = lade_json(pfad, None)
        if d and d.get("v") == 2:
            return d
    t = yf.Ticker(ticker)
    d = {"ticker": ticker, "v": 2}
    try:
        info = t.info or {}
    except Exception:
        info = {}
    for k in ["shortName", "longName", "sector", "industry", "currency", "currentPrice", "revenueGrowth", "earningsGrowth",
              "profitMargins", "operatingMargins", "debtToEquity", "currentRatio", "freeCashflow",
              "returnOnEquity", "forwardPE", "trailingPE", "pegRatio", "recommendationMean", "targetMeanPrice",
              "numberOfAnalystOpinions", "forwardEps", "trailingEps", "totalRevenue", "marketCap"]:
        d[k] = info.get(k)
    # Prognosen
    try:
        re_ = t.revenue_estimate
        d["umsatz_prognose_wachstum"] = nz(re_.loc["+1y", "growth"]) if "+1y" in re_.index else nz(re_.loc["0y", "growth"])
    except Exception:
        d["umsatz_prognose_wachstum"] = None
    try:
        ee = t.earnings_estimate
        d["gewinn_prognose_wachstum"] = nz(ee.loc["+1y", "growth"]) if "+1y" in ee.index else nz(ee.loc["0y", "growth"])
    except Exception:
        d["gewinn_prognose_wachstum"] = None
    try:
        et = t.eps_trend
        z = et.loc["+1y"] if "+1y" in et.index else et.loc["0y"]
        aktuell, vor30 = nz(z.get("current")), nz(z.get("30daysAgo"))
        d["eps_revision_30d"] = (aktuell / vor30 - 1) if aktuell is not None and vor30 not in (None, 0) else None
    except Exception:
        d["eps_revision_30d"] = None
    # Forschung/Entwicklung
    try:
        inc = t.income_stmt
        rd = None
        for nm in ("Research And Development", "ResearchAndDevelopment"):
            if nm in inc.index:
                rd = nz(inc.loc[nm].dropna().iloc[0])
                break
        umsatz = nz(inc.loc["Total Revenue"].dropna().iloc[0]) if "Total Revenue" in inc.index else None
        d["rd_quote"] = (rd / umsatz) if rd is not None and umsatz else None
    except Exception:
        d["rd_quote"] = None
    d["news"] = _news(t, d.get("longName") or d.get("shortName") or ticker)
    speichere_json(pfad, d)
    return d


# ---------------------------------------------------------------- Kriterien / Ampel
def K(key, titel, wert, status_, text):
    return {"key": key, "titel": titel, "wert": wert, "status": status_, "text": text}


def pct(x):
    return None if x is None else f"{x * 100:+.1f} %"


def bewerte(f, kurs, perf, markt_perf_21d):
    """f = Fundamentals-dict. Rueckgabe: (kriterien, ampel, punkte_text)"""
    kr = []
    sektor = f.get("sektor") or f.get("sector") or ""

    # Ursache: marktbedingt oder titelspezifisch (Rueckgang vs. Marktdurchschnitt, 21 Tage) + Themen aus Schlagzeilen
    m21, t21 = markt_perf_21d, perf.get("monat")
    ur = ursachen_aus_news(f.get("news") or [])
    ur_text = (" Mögliche Auslöser laut Schlagzeilen: " + ", ".join(f"{u['thema']} ({u['n']})" for u in ur[:3]) + ".") if ur else \
              " In den Schlagzeilen ist kein klarer Auslöser erkennbar."
    if t21 is not None and m21 is not None:
        rel = t21 - m21
        wert = f"Titel {t21 * 100:+.0f} % / Markt {m21 * 100:+.0f} %"
        if m21 <= -0.04 and rel > -0.08:
            kr.append(K("ursache", "Ursache des Einbruchs", wert, "gruen",
                        "Eher marktbedingt: der Gesamtmarkt ist im selben Zeitraum ebenfalls gefallen." + ur_text))
        elif rel > -0.10:
            kr.append(K("ursache", "Ursache des Einbruchs", wert, "gelb", "Teilweise titelspezifisch." + ur_text))
        else:
            kr.append(K("ursache", "Ursache des Einbruchs", wert, "gelb", "Klar titelspezifisch (deutlich schwächer als der Markt)." + ur_text))

    # Wachstum Umsatz
    ug = f.get("umsatz_prognose_wachstum")
    if ug is None:
        ug = f.get("revenueGrowth")
        quelle = "bisher"
    else:
        quelle = "Prognose"
    if ug is None:
        kr.append(K("umsatz", "Umsatzwachstum", "k. A.", "grau", "Keine Daten."))
    else:
        s = "gruen" if ug >= 0.08 else ("gelb" if ug >= 0 else "rot")
        kr.append(K("umsatz", "Umsatzwachstum", f"{pct(ug)} ({quelle})", s,
                    "Umsatz wächst" if s == "gruen" else ("Umsatz stagniert/langsam" if s == "gelb" else "Umsatz schrumpft")))

    # Gewinnwachstum
    gg = f.get("gewinn_prognose_wachstum")
    if gg is None:
        gg = f.get("earningsGrowth")
        quelle = "bisher"
    else:
        quelle = "Prognose"
    eps = nz(f.get("trailingEps"))
    if gg is None:
        kr.append(K("gewinn", "Gewinnwachstum", "k. A.", "grau", "Keine Daten."))
    else:
        s = "gruen" if gg >= 0.10 else ("gelb" if gg >= 0 else "rot")
        kr.append(K("gewinn", "Gewinnwachstum", f"{pct(gg)} ({quelle})", s,
                    "Gewinn wächst deutlich" if s == "gruen" else ("Gewinn leicht steigend/stabil" if s == "gelb" else "Gewinn sinkt")))

    # Profitabilitaet
    om = nz(f.get("operatingMargins"))
    if om is None:
        kr.append(K("marge", "Betriebsmarge", "k. A.", "grau", "Keine Daten."))
    else:
        s = "gruen" if om >= 0.10 else ("gelb" if om >= 0 else "rot")
        kr.append(K("marge", "Betriebsmarge", f"{om * 100:.1f} %", s,
                    "Profitabel" if s == "gruen" else ("Dünne Marge" if s == "gelb" else "Verlust im Kerngeschäft")))

    # Bilanz
    de = nz(f.get("debtToEquity"))
    fcf = nz(f.get("freeCashflow"))
    if sektor in FINANZ:
        kr.append(K("bilanz", "Verschuldung", "n/a", "grau", "Bei Finanzfirmen nicht aussagekräftig."))
    elif de is None:
        kr.append(K("bilanz", "Verschuldung", "k. A.", "grau", "Keine Daten."))
    else:
        s = "gruen" if de <= 80 else ("gelb" if de <= 200 else "rot")
        if fcf is not None and fcf < 0 and s == "gruen":
            s = "gelb"
        if fcf is not None and fcf < 0 and de > 120:
            s = "rot"
        kr.append(K("bilanz", "Verschuldung / Cashflow", f"D/E {de:.0f} %" + ("" if fcf is None else f", FCF {'+' if fcf >= 0 else '–'}"), s,
                    "Solide Bilanz" if s == "gruen" else ("Erhöhte Verschuldung oder negativer Cashflow" if s == "gelb" else "Hoch verschuldet bei negativem Cashflow")))

    # Bewertung
    fpe = nz(f.get("forwardPE"))
    if fpe is None:
        kr.append(K("bewertung", "Bewertung (Forward-KGV)", "k. A.", "grau", "Keine Daten / negativer Gewinn."))
    else:
        s = "gruen" if 0 < fpe <= 20 else ("gelb" if 0 < fpe <= 35 else "rot")
        kr.append(K("bewertung", "Bewertung (Forward-KGV)", f"{fpe:.1f}", s,
                    "Moderat bewertet" if s == "gruen" else ("Eher teuer" if s == "gelb" else "Sehr teuer")))

    # Analysten
    rm, tp, na = nz(f.get("recommendationMean")), nz(f.get("targetMeanPrice")), f.get("numberOfAnalystOpinions")
    if rm is None and tp is None:
        kr.append(K("analysten", "Analysten", "k. A.", "grau", "Keine Daten."))
    else:
        pot = (tp / kurs - 1) if tp and kurs else None
        s = "gelb"
        if rm is not None and pot is not None:
            if rm <= 2.2 and pot >= 0.15:
                s = "gruen"
            elif rm > 2.8 or pot < 0.02:
                s = "rot"
        wert = ("Empf. " + f"{rm:.1f}" if rm is not None else "") + (f", Kursziel {pot * 100:+.0f} %" if pot is not None else "")
        kr.append(K("analysten", "Analysten", wert.strip(", "), s,
                    f"Mittel von {na or '?'} Analysten (1 = Kaufen … 5 = Verkaufen)."))

    # Revisionen
    rv = nz(f.get("eps_revision_30d"))
    if rv is None:
        kr.append(K("revision", "Gewinnschätzungen (30 Tage)", "k. A.", "grau", "Keine Daten."))
    else:
        s = "gruen" if rv >= 0.01 else ("gelb" if rv >= -0.03 else "rot")
        kr.append(K("revision", "Gewinnschätzungen (30 Tage)", pct(rv), s,
                    "Analysten heben Schätzungen an" if s == "gruen" else ("Schätzungen stabil" if s == "gelb" else "Schätzungen werden gesenkt (Warnzeichen)")))

    # Innovation
    rd = nz(f.get("rd_quote"))
    if sektor in KEIN_RD_SEKTOREN and rd is None:
        kr.append(K("innovation", "Innovation (F&E-Anteil)", "n/a", "grau", "Für diese Branche nicht aussagekräftig."))
    elif rd is None:
        kr.append(K("innovation", "Innovation (F&E-Anteil)", "k. A.", "grau", "Keine F&E-Angabe."))
    else:
        s = "gruen" if rd >= 0.05 else ("gelb" if rd >= 0.02 else "grau")
        kr.append(K("innovation", "Innovation (F&E-Anteil)", f"{rd * 100:.1f} % vom Umsatz", s,
                    "Hohe Forschungsausgaben" if s == "gruen" else ("Moderate Forschungsausgaben" if s == "gelb" else "Geringe Forschungsausgaben")))

    # Nachrichten / neue Produkte (Stichwort-Auswertung, grob!)
    news = f.get("news") or []
    pos = neg = krit = 0
    for n in news:
        t = n["titel"].lower()
        pos += any(w in t for w in POS_WOERTER)
        neg += any(w in t for w in NEG_WOERTER)
        krit += any(w in t for w in KRITISCH_NEG)
    if not news:
        kr.append(K("news", "Nachrichten / neue Produkte", "keine", "grau", "Keine aktuellen Schlagzeilen gefunden."))
    elif krit > 0:
        kr.append(K("news", "Nachrichten / neue Produkte", f"{krit} kritische Meldung(en)", "rot",
                    "Schlagzeilen mit Warnzeichen (Gewinnwarnung, Untersuchung, Rückruf …). Unbedingt lesen."))
    elif pos > neg:
        kr.append(K("news", "Nachrichten / neue Produkte", f"{pos} positive / {neg} negative", "gruen",
                    "Überwiegend positive Schlagzeilen (Produkte, Aufträge, Prognosen). Stichwort-Auswertung, nicht gelesen."))
    else:
        kr.append(K("news", "Nachrichten / neue Produkte", f"{pos} positive / {neg} negative", "gelb",
                    "Keine klare Tendenz. Schlagzeilen selbst lesen."))

    # Gesamtampel
    anw = [k for k in kr if k["status"] != "grau"]
    g = sum(k["status"] == "gruen" for k in anw)
    r = [k for k in anw if k["status"] == "rot"]
    kritisch_rot = {"bilanz", "revision", "news"} & {k["key"] for k in r}
    beides_rot = {"umsatz", "gewinn"} <= {k["key"] for k in r}
    if kritisch_rot or beides_rot or len(r) >= 2:
        ampel = "rot"
    elif not r and len(anw) >= 5 and g >= max(5, int(0.6 * len(anw) + 0.5)):
        ampel = "gruen"
    else:
        ampel = "gelb"
    return kr, ampel, f"{g} von {len(anw)} grün, {len(r)} rot"


def gesamtbewertung(kr):
    """Score 0-100 (gewichtet; gruen=1, gelb=0.5, rot=0, 'grau' = keine Daten zaehlt nicht mit) und Anzahl erfuellter Kriterien."""
    g = dict(GEWICHTE)
    g.update(KONFIG.get("gewichte") or {})
    anw = [k for k in kr if k["status"] != "grau"]
    pk = {"gruen": 1.0, "gelb": 0.5, "rot": 0.0}
    ws = sum(g.get(k["key"], 5) for k in anw)
    score = round(100 * sum(g.get(k["key"], 5) * pk.get(k["status"], 0) for k in anw) / ws) if ws else None
    return {"score": score, "erfuellt": sum(k["status"] == "gruen" for k in anw), "bewertbar": len(anw)}


def depot_bewertung(ticker, kurs):
    """Bewertung eines Depot-Titels (ohne 'Ursache des Einbruchs': nur Zustand des Unternehmens, bis 9 Kriterien)."""
    try:
        f = hole_fundamentals(ticker)
        kr, ampel, punkte = bewerte(f, kurs, {"monat": None}, None)
        return {"ampel": ampel, "punkte": punkte, "kriterien": kr, **gesamtbewertung(kr)}
    except Exception as e:
        log("Depot-Bewertung", ticker, "Fehler:", e)
        return None


# ---------------------------------------------------------------- Scan
def scan():
    import numpy as np
    import pandas as pd
    k, markt = kurse_aktualisieren()
    k = k.where(k > 0)
    ret = {n: k.pct_change(f, fill_method=None) for n, (f, _) in TRIGGER.items()}
    r1, r5, r21 = k.pct_change(1, fill_method=None), k.pct_change(5, fill_method=None), k.pct_change(21, fill_method=None)
    hoch = k.rolling(252, min_periods=120).max()
    markt21 = float(r21.iloc[-1].mean())
    kand = []
    for tk in k.columns:
        if pd.isna(k[tk].iloc[-1]):
            continue
        ausl = []
        for tn, (f, schw) in TRIGGER.items():
            s = ret[tn][tk].tail(FENSTER_TAGE)
            if (s <= schw).any():
                tage = int(len(s) - 1 - np.where((s <= schw).values)[0][-1])
                ausl.append({"trigger": tn, "vor_tagen": tage})
        if not ausl:
            continue
        kand.append({"ticker": tk, "markt": markt.get(tk, "?"), "ausloeser": ausl,
                     "schwere": float(min(r21[tk].iloc[-1] if pd.notna(r21[tk].iloc[-1]) else 0,
                                          r5[tk].iloc[-1] * 2 if pd.notna(r5[tk].iloc[-1]) else 0))})
    kand.sort(key=lambda x: x["schwere"])
    kand = kand[:int(KONFIG.get("max_bewertet", 150))]
    log(f"{len(kand)} Einbruch-Kandidaten, lade Kennzahlen ...")
    ergebnis = []
    for i, c in enumerate(kand, 1):
        tk = c["ticker"]
        try:
            f = hole_fundamentals(tk)
        except Exception as e:
            log("Fundamentals", tk, "Fehler:", e)
            f = {"ticker": tk, "news": []}
        kurs = float(k[tk].iloc[-1])
        perf = {"tag": nz(r1[tk].iloc[-1]), "woche": nz(r5[tk].iloc[-1]), "monat": nz(r21[tk].iloc[-1])}
        kr, ampel, punkte = bewerte(f, kurs, perf, markt21)
        ergebnis.append({
            "ticker": tk, "name": f.get("shortName") or tk, "markt": c["markt"], "sektor": f.get("sector") or "",
            "waehrung": f.get("currency") or waehrung_von(tk), "kurs": rund(kurs),
            "tag_pct": rund((perf["tag"] or 0) * 100), "woche_pct": rund((perf["woche"] or 0) * 100),
            "monat_pct": rund((perf["monat"] or 0) * 100),
            "vom_hoch_pct": rund((kurs / float(hoch[tk].iloc[-1]) - 1) * 100) if pd.notna(hoch[tk].iloc[-1]) else None,
            "ausloeser": c["ausloeser"], "ampel": ampel, "punkte": punkte, "kriterien": kr, **gesamtbewertung(kr),
            "news": (f.get("news") or [])[:8], "ursachen": ursachen_aus_news(f.get("news") or [])[:5], "verlauf": [rund(x) for x in k[tk].tail(90).tolist()]})
        if i % 10 == 0:
            log(f"  {i}/{len(kand)}")
    # 2026-10-14: Ranking rein nach Score, rote Ampel (Warnzeichen) immer zuletzt; danach die besten N behalten
    ergebnis.sort(key=lambda x: (x["ampel"] == "rot", -(x.get("score") or 0), x["monat_pct"] or 0))
    return ergebnis[:int(KONFIG.get("max_kandidaten", MAX_KANDIDATEN))], k


# ---------------------------------------------------------------- Depot
def fx_raten():
    import yfinance as yf
    fx = {"CHF": 1.0, "USD": 1.0, "EUR": 1.0}
    try:
        for sym, w in (("USDCHF=X", "USD"), ("EURCHF=X", "EUR")):
            c = yf.download(sym, period="5d", auto_adjust=True, progress=False)["Close"].squeeze().dropna()
            fx[w] = float(c.iloc[-1])
    except Exception as e:
        log("FX nicht ladbar:", e)
    return fx


def kurse_fuer(tickers):
    """Aktueller Kurs (Tageskerze mit Live-Preis) + Veraenderungen fuer eine Ticker-Liste."""
    import pandas as pd
    import yfinance as yf
    q = {}
    if not tickers:
        return q
    for i in range(0, len(tickers), 80):
        blk = tickers[i:i + 80]
        dd = yf.download(blk, period="3mo", auto_adjust=True, progress=False, threads=False)["Close"]
        if getattr(dd, "ndim", 2) == 1:
            dd = dd.to_frame(blk[0])
        for t in blk:
            s = dd[t].dropna() if t in dd else pd.Series(dtype=float)
            if len(s) >= 22:
                q[t] = {"kurs": float(s.iloc[-1]), "tag": float(s.iloc[-1] / s.iloc[-2] - 1),
                        "woche": float(s.iloc[-1] / s.iloc[-6] - 1), "monat": float(s.iloc[-1] / s.iloc[-22] - 1),
                        "verlauf": [round(x, 2) for x in s.tail(60).tolist()]}
    return q


def demo_bewertung(ticker):
    kr = [K("umsatz", "Umsatzwachstum", "+11.0 % (Prognose)", "gruen", "Umsatz wächst"),
          K("gewinn", "Gewinnwachstum", "+14.0 % (Prognose)", "gruen", "Gewinn wächst deutlich"),
          K("marge", "Betriebsmarge", "24.1 %", "gruen", "Profitabel"),
          K("bilanz", "Verschuldung / Cashflow", "D/E 90 %, FCF +", "gelb", "Erhöhte Verschuldung"),
          K("bewertung", "Bewertung (Forward-KGV)", "22.0", "gelb", "Eher teuer"),
          K("analysten", "Analysten", "Empf. 2.0, Kursziel +20 %", "gruen", "Mittel von 20 Analysten"),
          K("revision", "Gewinnschätzungen (30 Tage)", "+0.5 %", "gelb", "Schätzungen stabil"),
          K("innovation", "Innovation (F&E-Anteil)", "n/a", "grau", "Für diese Branche nicht aussagekräftig."),
          K("news", "Nachrichten / neue Produkte", "1 positive / 0 negative", "gruen", "Überwiegend positive Schlagzeilen")]
    return {"ampel": "gelb", "punkte": "4 von 8 grün, 0 rot", "kriterien": kr, **gesamtbewertung(kr)}


def depot_berechnen(kurse_func=None):
    """Liest depot.json, holt Kurse, rechnet Veraenderungen und Konto (in CHF)."""
    import pandas as pd
    d = lade_json(DATEI_DEPOT, {"positionen": [], "bargeld_chf": 0})
    pos = d.get("positionen", [])
    res, konto = [], {"einstand_chf": 0.0, "wert_chf": 0.0, "tag_chf": 0.0}
    if not pos:
        return {"positionen": [], "konto": {**konto, "gewinn_chf": 0, "gewinn_pct": 0, "bargeld_chf": d.get("bargeld_chf", 0)}}
    tickers = sorted({p["ticker"] for p in pos})
    if kurse_func:
        q = kurse_func(tickers)
        fx = {"CHF": 1.0, "USD": 0.8, "EUR": 0.95}
    else:
        q = kurse_fuer(tickers)
        fx = fx_raten()
    for p in pos:
        t = p["ticker"]
        w = p.get("waehrung") or waehrung_von(t)
        qq = q.get(t)
        stueck, einst = float(p["stueck"]), float(p["einstand"])
        kurs = qq["kurs"] if qq else None
        wert = kurs * stueck if kurs else None
        gew = (kurs - einst) * stueck if kurs else None
        f = fx.get(w, 1.0)
        res.append({"ticker": t, "name": p.get("name") or t, "stueck": stueck, "einstand": einst,
                    "datum": p.get("datum", ""), "waehrung": w, "notiz": p.get("notiz", ""),
                    "kurs": rund(kurs), "wert": rund(wert), "gewinn": rund(gew),
                    "gewinn_pct": rund((kurs / einst - 1) * 100) if kurs else None,
                    "tag_pct": rund(qq["tag"] * 100) if qq else None, "woche_pct": rund(qq["woche"] * 100) if qq else None,
                    "monat_pct": rund(qq["monat"] * 100) if qq else None,
                    "verlauf": qq.get("verlauf") if qq else None,
                    "kauf": p.get("kauf"),
                    "bewertung": (demo_bewertung(t) if kurse_func else depot_bewertung(t, kurs)) if qq else None,
                    "wert_chf": rund(wert * f) if wert else None})
        konto["einstand_chf"] += einst * stueck * f
        if wert:
            konto["wert_chf"] += wert * f
            konto["tag_chf"] += wert * f * (qq["tag"] / (1 + qq["tag"]))
    gew = konto["wert_chf"] - konto["einstand_chf"]
    konto.update({"gewinn_chf": round(gew, 2), "gewinn_pct": round(gew / konto["einstand_chf"] * 100, 2) if konto["einstand_chf"] else 0,
                  "bargeld_chf": d.get("bargeld_chf", 0), "einstand_chf": round(konto["einstand_chf"], 2),
                  "wert_chf": round(konto["wert_chf"], 2), "tag_chf": round(konto["tag_chf"], 2),
                  "fx": {k: round(v, 4) for k, v in fx.items()}})
    return {"positionen": res, "konto": konto}



# ---------------------------------------------------------------- JSONBin (Web-App-Anbindung)
def konfig_laden():
    if not os.path.exists(DATEI_KONFIG):
        speichere_json(DATEI_KONFIG, KONFIG_VORLAGE)
        log("Konfigurationsdatei angelegt:", DATEI_KONFIG, "(für die Web-App: Bin-ID und Access-Key eintragen)")
    k = dict(KONFIG_VORLAGE)
    k.update(lade_json(DATEI_KONFIG, {}))
    return k


KONFIG = konfig_laden()


def web_aktiv():
    return bool(KONFIG.get("jsonbin_bin_id") and KONFIG.get("jsonbin_access_key")) and not DEMO


def _bin_url():
    return "https://api.jsonbin.io/v3/b/" + KONFIG["jsonbin_bin_id"].strip()


def bin_lesen():
    import requests
    r = requests.get(_bin_url() + "/latest", headers={"X-Access-Key": KONFIG["jsonbin_access_key"].strip(),
                                                      "X-Bin-Meta": "false"}, timeout=30)
    r.raise_for_status()
    return r.json()


def bin_schreiben(obj):
    import requests
    r = requests.put(_bin_url(), data=json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                     headers={"X-Access-Key": KONFIG["jsonbin_access_key"].strip(),
                              "Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()


def web_nutzlast(daten, depot_roh):
    """Verkleinerte Fassung fuer den Bin: kuerzere Verlaeufe, weniger Nachrichten, Groessenlimit."""
    out = {k: daten.get(k) for k in ("stand", "depot_stand", "index", "positionen", "konto")}
    out["index"] = [dict(i, verlauf=(i.get("verlauf") or [])[-45:]) for i in (daten.get("index") or [])]
    out["positionen"] = [dict(p, verlauf=(p.get("verlauf") or [])[-30:]) for p in (daten.get("positionen") or [])]
    kand = []
    for c in daten.get("kandidaten") or []:
        c2 = dict(c)
        c2["verlauf"] = (c.get("verlauf") or [])[-40:]
        c2["news"] = [{"titel": n.get("titel"), "quelle": n.get("quelle"), "zeit": n.get("zeit"), "link": n.get("link")}
                      for n in (c.get("news") or [])[:5]]
        kand.append(c2)
    out["depot"] = depot_roh
    out["kandidaten"] = kand
    limit = int(KONFIG.get("max_kb", 90)) * 1024
    while len(json.dumps(out, ensure_ascii=False)) > limit and out["kandidaten"]:
        out["kandidaten"].pop()          # Liste ist nach Ampel sortiert: schlechteste (rot) fallen zuerst weg
        out["gekuerzt"] = True
    return out


_letzter_hash = [None]


def web_push(daten):
    """Depot-Aenderungen aus der App uebernehmen (neueste gewinnt), dann Daten schreiben."""
    if not web_aktiv():
        return
    try:
        remote = {}
        try:
            remote = bin_lesen() or {}
        except Exception as e:
            log("Bin lesen fehlgeschlagen:", e)
        lokal = lade_json(DATEI_DEPOT, {"positionen": [], "bargeld_chf": 0, "rev": 0})
        rd = remote.get("depot")
        if isinstance(rd, dict) and int(rd.get("rev", 0)) > int(lokal.get("rev", 0)):
            speichere_json(DATEI_DEPOT, rd)
            lokal = rd
            log("Depot aus der Web-App übernommen (rev", rd.get("rev"), ")")
            daten.update(depot_berechnen())
            speichere_json(DATEI_DATEN, daten)
        payload = web_nutzlast(daten, lokal)
        # 2026-10-14: unveraendert (ausser Zeitstempel) -> kein Schreib-Request
        import hashlib
        h = hashlib.md5(json.dumps({k: v for k, v in payload.items() if k not in ("stand", "depot_stand")},
                                   sort_keys=True, default=str).encode()).hexdigest()
        if h == _letzter_hash[0] and int((remote.get("depot") or {}).get("rev", -1)) == int(lokal.get("rev", 0)) and remote.get("stand"):
            log("Web-Bin unverändert - Schreiben übersprungen")
            return
        bin_schreiben(payload)
        _letzter_hash[0] = h
        status["push"] = jetzt()
        log(f"Web-Bin aktualisiert ({len(json.dumps(payload)) // 1024} KB)")
    except Exception as e:
        status["fehler"] = "Web-Sync: " + str(e)[:200]
        log("Web-Sync Fehler:", e)


# ---------------------------------------------------------------- Laeufe
letzter_push = [0.0]


def _push_intervall_s():
    """Kernzeit (Standard 09:00-18:00) dichter, sonst seltener."""
    n = dt.datetime.now()
    m = n.hour * 60 + n.minute
    k0, k1 = (_hhmm(x) for x in KONFIG["kernzeit"])
    kern = n.weekday() < 5 and k0 <= m <= k1
    return int(KONFIG["push_kern_min"] if kern else KONFIG["push_aussen_min"]) * 60


def _ranking(daten):
    """Score (neu) berechnen und Kandidaten nach Score sortieren (rot zuletzt) (Gewichte aus der Config gelten sofort)."""
    for c in daten.get("kandidaten") or []:
        c.update(gesamtbewertung(c.get("kriterien") or []))
    (daten.get("kandidaten") or []).sort(key=lambda x: (x.get("ampel") == "rot", -(x.get("score") or 0), x.get("monat_pct") or 0))
    for p in daten.get("positionen") or []:
        b = p.get("bewertung")
        if b and b.get("kriterien"):
            b.update(gesamtbewertung(b["kriterien"]))


def _schreiben_und_pushen(daten, erzwingen=False):
    _ranking(daten)
    daten["stand"] = daten.get("stand") or jetzt()
    daten["demo"] = DEMO
    speichere_json(DATEI_DATEN, daten)
    if web_aktiv() and (erzwingen or time.time() - letzter_push[0] >= _push_intervall_s()):
        web_push(daten)
        letzter_push[0] = time.time()


def voll_lauf():
    with lock:
        if status["laeuft"]:
            return
        status.update({"laeuft": True, "meldung": "Vollscan läuft ...", "fehler": None})
    try:
        if DEMO:
            daten = demo_daten()
        else:
            idx = index_status()
            kand, _ = scan()
            daten = {"index": idx, "kandidaten": kand, **depot_berechnen()}
        daten["stand"] = jetzt()
        _schreiben_und_pushen(daten, erzwingen=True)
        status.update({"meldung": "fertig", "letzter_lauf": jetzt()})
    except Exception as e:
        traceback.print_exc()
        status.update({"fehler": str(e), "meldung": "Fehler"})
    finally:
        status["laeuft"] = False


def schnell_lauf():
    """Kurse von Indizes, Kandidaten und Depot aktualisieren (ohne neue Kandidaten zu suchen)."""
    with lock:
        daten = lade_json(DATEI_DATEN, {"index": [], "kandidaten": []})
        try:
            if DEMO:
                dep = depot_berechnen(demo_kurse)
            else:
                daten["index"] = index_status() or daten.get("index", [])
                tk = [c["ticker"] for c in daten.get("kandidaten", [])]
                q = kurse_fuer(tk)
                for c in daten.get("kandidaten", []):
                    x = q.get(c["ticker"])
                    if x:
                        c["kurs"] = rund(x["kurs"])
                        c["tag_pct"], c["woche_pct"], c["monat_pct"] = rund(x["tag"] * 100), rund(x["woche"] * 100), rund(x["monat"] * 100)
                        c["verlauf"] = x["verlauf"]
                dep = depot_berechnen()
            daten.update(dep)
            daten["depot_stand"] = jetzt()
            _schreiben_und_pushen(daten)
        except Exception as e:
            traceback.print_exc()
            status["fehler"] = str(e)[:200]


def depot_lauf():
    schnell_lauf()


def im_hintergrund(fn):
    threading.Thread(target=fn, daemon=True).start()


def _hhmm(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def planer():
    """Boersentage: alle 15 Min Kurse, mehrmals taeglich Vollscan. Sonst nur beim Start/alle 12 h."""
    done_voll = set()
    letzte_schnell = 0.0
    while True:
        try:
            jetzt_dt = dt.datetime.now()
            minuten = jetzt_dt.hour * 60 + jetzt_dt.minute
            tag = jetzt_dt.strftime("%Y-%m-%d")
            alter = time.time() - os.path.getmtime(DATEI_DATEN) if os.path.exists(DATEI_DATEN) else 1e9
            handelstag = jetzt_dt.weekday() < 5
            b0, b1 = (_hhmm(x) for x in KONFIG["boersenzeit"])
            if alter > 12 * 3600 and not status["laeuft"]:
                voll_lauf()
            elif handelstag and b0 <= minuten <= b1 + 20:
                faellig = [z for z in KONFIG["vollscan_zeiten"] if _hhmm(z) <= minuten and (tag, z) not in done_voll]
                if faellig:
                    for z in faellig:
                        done_voll.add((tag, z))
                    voll_lauf()
                elif time.time() - letzte_schnell >= int(KONFIG["schnell_intervall_min"]) * 60:
                    schnell_lauf()
                    letzte_schnell = time.time()
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ---------------------------------------------------------------- Demo
def demo_kurse(tickers):
    import random
    out = {}
    for i, t in enumerate(tickers):
        random.seed(t)
        base = 50 + (hash(t) % 200)
        v = [base]
        for _ in range(59):
            v.append(v[-1] * (1 + random.uniform(-0.02, 0.022)))
        out[t] = {"kurs": v[-1], "tag": v[-1] / v[-2] - 1, "woche": v[-1] / v[-6] - 1, "monat": v[-1] / v[-22] - 1,
                  "verlauf": [round(x, 2) for x in v]}
    return out


def demo_daten():
    import random
    random.seed(4)

    def verlauf(start, ende, n=90):
        v = []
        for i in range(n):
            v.append(start + (ende - start) * i / (n - 1) + random.uniform(-1, 1) * abs(start) * 0.01)
        return [round(x, 2) for x in v]
    idx = [
        {"name": "S&P 500", "symbol": "^GSPC", "kurs": 6210.5, "tag_pct": -1.1, "woche_pct": -4.6, "monat_pct": -6.2,
         "vom_hoch_pct": -7.4, "status": "ALARM", "ausgeloest": [{"trigger": "Woche -4%", "stats": INDEX_STATS["S&P 500"]["Woche -4%"]}],
         "verlauf": verlauf(6700, 6210)},
        {"name": "SMI", "symbol": "^SSMI", "kurs": 12980.0, "tag_pct": 0.3, "woche_pct": -0.8, "monat_pct": -1.5,
         "vom_hoch_pct": -2.6, "status": "ruhig", "ausgeloest": [], "verlauf": verlauf(13300, 12980)},
        {"name": "DAX", "symbol": "^GDAXI", "kurs": 23100.0, "tag_pct": -0.6, "woche_pct": -2.2, "monat_pct": -5.4,
         "vom_hoch_pct": -6.1, "status": "beobachten", "ausgeloest": [], "verlauf": verlauf(24500, 23100)}]

    def kr(*a):
        return [K(*x) for x in a]
    k1 = kr(("ursache", "Ursache des Einbruchs", "Titel -22 % / Markt -6 %", "gelb", "Klar titelspezifisch."),
            ("umsatz", "Umsatzwachstum", "+11.0 % (Prognose)", "gruen", "Umsatz wächst"),
            ("gewinn", "Gewinnwachstum", "+14.0 % (Prognose)", "gruen", "Gewinn wächst deutlich"),
            ("marge", "Betriebsmarge", "24.1 %", "gruen", "Profitabel"),
            ("bilanz", "Verschuldung / Cashflow", "D/E 35 %, FCF +", "gruen", "Solide Bilanz"),
            ("bewertung", "Bewertung (Forward-KGV)", "17.2", "gruen", "Moderat bewertet"),
            ("analysten", "Analysten", "Empf. 1.9, Kursziel +28 %", "gruen", "Mittel von 31 Analysten"),
            ("revision", "Gewinnschätzungen (30 Tage)", "+1.8 %", "gruen", "Analysten heben Schätzungen an"),
            ("innovation", "Innovation (F&E-Anteil)", "9.4 % vom Umsatz", "gruen", "Hohe Forschungsausgaben"),
            ("news", "Nachrichten / neue Produkte", "2 positive / 0 negative", "gruen", "Überwiegend positive Schlagzeilen"))
    k2 = kr(("ursache", "Ursache des Einbruchs", "Titel -35 % / Markt -6 %", "gelb", "Klar titelspezifisch."),
            ("umsatz", "Umsatzwachstum", "-3.0 % (Prognose)", "rot", "Umsatz schrumpft"),
            ("gewinn", "Gewinnwachstum", "-12.0 % (Prognose)", "rot", "Gewinn sinkt"),
            ("marge", "Betriebsmarge", "4.0 %", "gelb", "Dünne Marge"),
            ("bilanz", "Verschuldung / Cashflow", "D/E 160 %, FCF –", "rot", "Hoch verschuldet bei negativem Cashflow"),
            ("news", "Nachrichten / neue Produkte", "1 kritische Meldung(en)", "rot", "Gewinnwarnung"))
    k3 = kr(("ursache", "Ursache des Einbruchs", "Titel -9 % / Markt -6 %", "gruen", "Eher marktbedingt."),
            ("umsatz", "Umsatzwachstum", "+5.0 % (Prognose)", "gelb", "Umsatz stagniert/langsam"),
            ("gewinn", "Gewinnwachstum", "+9.0 % (Prognose)", "gelb", "Gewinn leicht steigend"),
            ("marge", "Betriebsmarge", "13.0 %", "gruen", "Profitabel"),
            ("bilanz", "Verschuldung / Cashflow", "D/E 90 %, FCF +", "gelb", "Erhöhte Verschuldung"),
            ("bewertung", "Bewertung (Forward-KGV)", "22.0", "gelb", "Eher teuer"),
            ("innovation", "Innovation (F&E-Anteil)", "n/a", "grau", "Für diese Branche nicht aussagekräftig."),
            ("news", "Nachrichten / neue Produkte", "0 positive / 0 negative", "gelb", "Keine klare Tendenz."))
    cand = [
        {"ticker": "DEMO1", "name": "Demo Technologie AG", "markt": "US", "sektor": "Technology", "waehrung": "USD",
         "kurs": 182.4, "tag_pct": -2.1, "woche_pct": -11.5, "monat_pct": -22.0, "vom_hoch_pct": -31.0,
         "ausloeser": [{"trigger": "Woche -10%", "vor_tagen": 2}, {"trigger": "Monat -20%", "vor_tagen": 1}],
         "ampel": "gruen", "punkte": "9 von 10 grün, 0 rot", "kriterien": k1, "verlauf": verlauf(240, 182),
         "news": [{"titel": "Demo launches new AI product line", "quelle": "Demo News", "zeit": "2026-10-13", "link": ""},
                  {"titel": "Demo raises guidance after strong cloud demand", "quelle": "Demo News", "zeit": "2026-10-11", "link": ""}]},
        {"ticker": "DEMO2", "name": "Demo Industrie SE", "markt": "DE", "sektor": "Industrials", "waehrung": "EUR",
         "kurs": 31.7, "tag_pct": -6.5, "woche_pct": -14.0, "monat_pct": -35.0, "vom_hoch_pct": -52.0,
         "ausloeser": [{"trigger": "Tag -5%", "vor_tagen": 0}], "ampel": "rot", "punkte": "0 von 6 grün, 4 rot", "kriterien": k2,
         "verlauf": verlauf(49, 31.7),
         "news": [{"titel": "Demo SE issues profit warning, cuts guidance", "quelle": "Demo News", "zeit": "2026-10-14", "link": ""}]},
        {"ticker": "DEMO3.SW", "name": "Demo Konsum AG", "markt": "CH", "sektor": "Consumer Defensive", "waehrung": "CHF",
         "kurs": 88.0, "tag_pct": -1.0, "woche_pct": -5.0, "monat_pct": -9.0, "vom_hoch_pct": -16.0,
         "ausloeser": [{"trigger": "Woche -10%", "vor_tagen": 6}], "ampel": "gelb", "punkte": "3 von 7 grün, 0 rot", "kriterien": k3,
         "verlauf": verlauf(97, 88), "news": []}]
    cand[1]["news"].append({"titel": "Demo SE faces class action lawsuit and EU antitrust probe over pricing", "quelle": "Demo News", "zeit": "2026-10-13", "link": ""})
    for c in cand:
        c["ursachen"] = ursachen_aus_news(c.get("news") or [])
    d = {"index": idx, "kandidaten": cand}
    if not os.path.exists(DATEI_DEPOT):
        speichere_json(DATEI_DEPOT, {"bargeld_chf": 1500, "positionen": [
            {"ticker": "DEMO1", "name": "Demo Technologie AG", "stueck": 5, "einstand": 170.0, "datum": "2026-09-20", "waehrung": "USD"},
            {"ticker": "DEMO3.SW", "name": "Demo Konsum AG", "stueck": 10, "einstand": 92.0, "datum": "2026-08-15", "waehrung": "CHF"}]})
    d.update(depot_berechnen(demo_kurse))
    return d


# ---------------------------------------------------------------- Server
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html", "/aktien-watcher.html", "/marktbeobachter.html"):
            try:
                with open(os.path.join(HIER, "aktien-watcher.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                return self._send(404, "aktien-watcher.html fehlt neben aktien_watcher.py", "text/plain; charset=utf-8")
        if p in ("/manifest.json", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            try:
                with open(os.path.join(HIER, p[1:]), "rb") as f:
                    return self._send(200, f.read(), "application/json" if p.endswith(".json") else "image/png")
            except Exception:
                return self._send(404, "{}")
        if p == "/api/daten":
            d = lade_json(DATEI_DATEN, {"index": [], "kandidaten": [], "positionen": [], "konto": {}})
            d["status"] = status
            return self._send(200, json.dumps(d, ensure_ascii=False))
        if p == "/api/depot":
            return self._send(200, json.dumps(lade_json(DATEI_DEPOT, {"positionen": [], "bargeld_chf": 0}), ensure_ascii=False))
        if p == "/api/status":
            return self._send(200, json.dumps(status))
        self._send(404, "{}")

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if p == "/api/depot":
            try:
                d = json.loads(body.decode("utf-8"))
                assert isinstance(d.get("positionen"), list)
                d["rev"] = int(time.time())
                speichere_json(DATEI_DEPOT, d)
                letzter_push[0] = 0.0          # Depot-Aenderung sofort in die Web-App
                depot_lauf()
                return self._send(200, '{"ok":true}')
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "fehler": str(e)}))
        if p == "/api/refresh":
            was = (json.loads(body.decode("utf-8")) if body else {}).get("was", "depot")
            im_hintergrund(voll_lauf if was == "voll" else depot_lauf)
            return self._send(200, '{"ok":true}')
        self._send(404, "{}")


def main():
    host = "0.0.0.0" if LAN else "127.0.0.1"
    if DEMO:
        log("DEMO-Modus: Testdaten, keine Internetzugriffe.")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    log(f"Aktien-Watcher läuft: http://localhost:{PORT}" + ("  (auch im Netz erreichbar)" if LAN else ""))
    log("Beenden mit Strg+C. Erster Vollscan startet jetzt und dauert beim ersten Mal 10-20 Minuten.")
    if web_aktiv():
        log("Web-Sync aktiv (JSONBin), Schreiben alle", KONFIG["push_kern_min"], "Min. in der Kernzeit", "-".join(KONFIG["kernzeit"]), "sonst alle", KONFIG["push_aussen_min"], "Min.")
    else:
        log("Web-Sync AUS (Bin-ID/Key fehlen in aktien_watcher_config.json) - nur lokale App.")
    threading.Thread(target=planer, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
