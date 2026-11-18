"""
Fiyat Takip Botu -> WhatsApp
Le Male Le Parfum EDP 125 ml (Boyner + Sephora)

Kurulum:
    pip install requests beautifulsoup4
    # Siteler engellerse (önerilir):
    pip install playwright && playwright install chromium

Çalıştırma:
    python fiyat_botu.py          # sürekli çalışır, her KONTROL_DAKIKA'da bir bakar
    python fiyat_botu.py --test   # bir kez kontrol eder + test mesajı atar
"""

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ======================= AYARLAR =======================
WHATSAPP_TELEFON = os.environ.get("WHATSAPP_TELEFON", "")  # GitHub Secrets'ten gelir
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")  # GitHub Secrets'ten gelir

KONTROL_DAKIKA = 30                  # kaç dakikada bir kontrol edilsin

# Sadece fiyat bu değerin ALTINA inince mesaj atılır.
# Altındayken fiyat daha da düşerse yine haber verir.
HEDEF_FIYAT = 7000

URUNLER = [
    {
        "magaza": "Boyner",
        "url": "https://www.boyner.com.tr/jean-paul-gaultier-le-male-le-parfum-edp-125-ml-erkek-parfum-p-983107",
    },
    {
        "magaza": "Sephora",
        "url": "https://www.sephora.com.tr/p/jpg-le-male-edp-125ml-P10010217.html",
        # Sayfa varsayılan olarak 75 ml açılıyor -> bot önce "125 ml" seçeneğine tıklar
        "secim": "125 ml",
    },
]
# =======================================================

DURUM_DOSYASI = Path(__file__).with_name("fiyat_durum.json")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------- WhatsApp ----------------
def whatsapp_gonder(mesaj: str):
    url = ("https://api.callmebot.com/whatsapp.php?phone="
           f"{urllib.parse.quote(WHATSAPP_TELEFON)}"
           f"&text={urllib.parse.quote(mesaj)}&apikey={CALLMEBOT_APIKEY}")
    try:
        r = requests.get(url, timeout=30)
        log(f"WhatsApp gönderildi (HTTP {r.status_code})")
    except Exception as e:
        log(f"WhatsApp HATASI: {e}")


# ---------------- Fiyat okuma ----------------
def tl_to_float(s) -> float | None:
    """'4.250,90 TL' / '4250.90' / 4250 -> 4250.9"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"[^\d.,]", "", str(s))
    if not s:
        return None
    if "," in s and "." in s:            # 4.250,90
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                       # 4250,90
        s = s.replace(",", ".")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):  # 4.250
        s = s.replace(".", "")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def jsonld_fiyat(soup) -> float | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            if "@graph" in d:
                stack.extend(d["@graph"])
            offers = d.get("offers")
            if offers:
                for o in (offers if isinstance(offers, list) else [offers]):
                    if isinstance(o, dict):
                        p = tl_to_float(o.get("price") or o.get("lowPrice"))
                        if p:
                            return p
    return None


def html_fiyat(html: str) -> float | None:
    soup = BeautifulSoup(html, "html.parser")

    # 1) Yapısal veri (JSON-LD)
    p = jsonld_fiyat(soup)
    if p:
        return p

    # 2) Meta etiketleri
    for attr, name in [("property", "product:price:amount"),
                       ("property", "og:price:amount"),
                       ("itemprop", "price")]:
        t = soup.find(attrs={attr: name})
        if t:
            p = tl_to_float(t.get("content") or t.get_text())
            if p:
                return p

    # 3) Sık kullanılan CSS sınıfları
    for sel in ["[data-price]", ".price-sales", ".product-price", ".price__current",
                ".sales .value", ".product-price-container", ".price"]:
        t = soup.select_one(sel)
        if t:
            p = tl_to_float(t.get("data-price") or t.get("content") or t.get_text())
            if p and p > 100:
                return p

    # 4) Son çare: sayfadaki "x.xxx,xx TL" kalıpları içinde en sık geçeni
    adaylar = re.findall(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:TL|₺)", html)
    sayilar = [tl_to_float(a) for a in adaylar]
    sayilar = [s for s in sayilar if s and s > 500]
    if sayilar:
        return max(set(sayilar), key=sayilar.count)
    return None


def sayfa_getir_requests(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def sayfa_getir_playwright(url, secim=None):
    """HTML döndürür; secim verilirse önce o seçeneğe tıklar ve görünen fiyatı da döndürür."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(user_agent=HEADERS["User-Agent"], locale="tr-TR")
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(4000)
        gorunen = None
        if secim:
            gorunen = secili_boy_fiyati(pg, secim)
        html = pg.content()
        b.close()
        return html, gorunen


def _sephora_oku(pg):
    """(seçili boy ml, ana fiyat, 100ml birim fiyat) döndürür."""
    metin = pg.inner_text("body")
    i = metin.find("farklı boy")
    if i < 0:
        return None, None, None
    tutarlar = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*(?:TL|₺)", metin[:i])
    m = re.search(r"farklı boy[^\n]*\n+\s*(\d+)\s*ml\s*\n+\s*([\d.,]+)\s*₺\s*/\s*100", metin[i - 5:])
    ana = tl_to_float(tutarlar[-1]) if tutarlar else None
    if not m:
        return None, ana, None
    return int(m.group(1)), ana, tl_to_float(m.group(2))


def secili_boy_fiyati(pg, secim):
    """Sephora tipi boy seçici: seçili boy istenen değilse seçiciyi açıp istenen boya tıklar."""
    istenen = int(re.search(r"\d+", secim).group())
    try:
        pg.get_by_text(re.compile("farklı boy")).first.wait_for(timeout=25000)
    except Exception:
        pass
    boy, ana, birim = _sephora_oku(pg)
    if boy is None:
        metin = re.sub(r"\s+", " ", pg.inner_text("body"))
        log(f"  Boy seçici bulunamadı. Başlık: {pg.title()[:80]!r} | Metin: {metin[:300]!r}")
        return None
    if boy != istenen:
        try:
            pg.get_by_text(f"{boy} ml", exact=True).first.click(timeout=10000)
            pg.wait_for_timeout(2000)
            secenek = pg.get_by_text(f"{istenen} ml", exact=True)
            if secenek.count() == 0:
                log(f"  '{istenen} ml' seçeneği yok (şu an satışta değil)")
                return None
            secenek.last.click(timeout=10000)
            pg.wait_for_timeout(4000)
        except Exception as e:
            log(f"  Boy seçilemedi: {e.__class__.__name__}")
            return None
        boy, ana, birim = _sephora_oku(pg)
    if boy != istenen or not ana:
        log(f"  {istenen} ml seçilemedi (seçili: {boy} ml)")
        return None
    # Güvenlik: ana fiyat, birim fiyatla tutarlı mı? (yanlış boyun fiyatını okumamak için)
    if birim and abs(ana - birim * istenen / 100) > ana * 0.02:
        log(f"  Fiyat tutarsız: {ana} vs {birim}/100ml")
        return None
    return ana


def fiyat_al(url, secim=None) -> float | None:
    if secim:  # boyut seçimi gereken sayfa: doğrudan tarayıcıyla
        try:
            html, gorunen = sayfa_getir_playwright(url, secim)
            return gorunen  # HTML yedeği kullanılmaz: varsayılan 75 ml fiyatını okuyup yanlış alarm vermesin
        except ImportError:
            log("  Bu mağaza için Playwright gerekli (pip install playwright && playwright install chromium)")
            return None
        except Exception as e:
            log(f"  Playwright hatası: {e}")
            return None
    try:
        p = html_fiyat(sayfa_getir_requests(url))
        if p:
            return p
    except Exception as e:
        log(f"  requests başarısız ({e.__class__.__name__}), tarayıcı deneniyor...")
    try:
        html, _ = sayfa_getir_playwright(url)
        return html_fiyat(html)
    except ImportError:
        log("  Playwright kurulu değil (pip install playwright && playwright install chromium)")
    except Exception as e:
        log(f"  Playwright hatası: {e}")
    return None


# ---------------- Durum ----------------
def durum_oku():
    if DURUM_DOSYASI.exists():
        return json.loads(DURUM_DOSYASI.read_text(encoding="utf-8"))
    return {}


def durum_yaz(d):
    DURUM_DOSYASI.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- Ana döngü ----------------
def kontrol_et():
    durum = durum_oku()
    for u in URUNLER:
        magaza, url = u["magaza"], u["url"]
        if url.startswith("BURAYA"):
            log(f"{magaza}: link girilmemiş, atlanıyor")
            continue

        fiyat = fiyat_al(url, u.get("secim"))
        if fiyat is None:
            log(f"{magaza}: fiyat okunamadı")
            continue

        eski = durum.get(url, {}).get("fiyat")
        log(f"{magaza}: {fiyat:,.2f} TL (önceki: {eski if eski else '-'})")

        hedefte = HEDEF_FIYAT is None or fiyat < HEDEF_FIYAT
        ilk_kez_hedefte = HEDEF_FIYAT and fiyat < HEDEF_FIYAT and (not eski or eski >= HEDEF_FIYAT)

        if ilk_kez_hedefte:
            whatsapp_gonder(
                f"🎯 {HEDEF_FIYAT:,.0f} TL ALTINA DÜŞTÜ - {magaza}\n"
                f"Le Male Le Parfum EDP 125 ml\n"
                f"Şu an: {fiyat:,.2f} TL" + (f" (önceki {eski:,.2f} TL)" if eski else "") +
                f"\n{url}"
            )
        elif hedefte and eski and fiyat < eski:
            fark = eski - fiyat
            yuzde = fark / eski * 100
            whatsapp_gonder(
                f"🔻 FİYAT DÜŞTÜ - {magaza}\n"
                f"Le Male Le Parfum EDP 125 ml\n"
                f"{eski:,.2f} TL → {fiyat:,.2f} TL\n"
                f"(-{fark:,.2f} TL / %{yuzde:.1f})\n{url}"
            )

        durum[url] = {"magaza": magaza, "fiyat": fiyat,
                      "zaman": datetime.now().isoformat(timespec="seconds")}
    durum_yaz(durum)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--once" in sys.argv:          # GitHub Actions: tek kontrol yap ve çık
        kontrol_et()
        return
    if "--test" in sys.argv:
        whatsapp_gonder("✅ Fiyat botu test mesajı - bağlantı çalışıyor!")
        kontrol_et()
        return
    log(f"Bot başladı, her {KONTROL_DAKIKA} dk'da kontrol edecek. (Durdurmak: Ctrl+C)")
    while True:
        try:
            kontrol_et()
        except Exception as e:
            log(f"Beklenmeyen hata: {e}")
        time.sleep(KONTROL_DAKIKA * 60)


if __name__ == "__main__":
    main()
