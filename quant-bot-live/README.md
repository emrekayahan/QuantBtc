# QuantBtc — Autonomous Quantitative Cryptocurrency Trading Engine (v3.0)

**QuantBtc (`quant-bot-live`)**, Binance Spot piyasası için tasarlanmış, 7/24 otonom çalışan, çift katmanlı (Dual-Layer) kantitatif algoritmik alım-satım motoru, risk yönetim sistemi, yerel/uzak Web Dashboard ve çift yönlü Telegram kontrol platformudur.

---

## Temel Özellikler ve Mimari

Sistem, hız ve güvenliği birbirinden ayırmak için **İki Katmanlı (Brain & Reflex)** bir mimariyle çalışır ve hiçbir matematiksel hesaplamayı veya risk limitini yapay zekaya (LLM) devretmez:

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ KATMAN 1: THE BRAIN (System 2 — Makro Rejim, Piyasa Tarayıcı & Kalibrasyon)│
│ - Her 1 saatte bir ve gece 00:05 UTC'de çalışır.                           │
│ - BTC/ETH üzerinden piyasa rejimini ('trending', 'mean_reverting',         │
│   'high_vol', 'crisis') deterministik matematik + OpenRouter havuzuyla     │
│   sınıflandırır.                                                           │
│ - Binance Spot'ta $50M+ hacimli ve <= %0.15 spread'li en likit USDT        │
│   paritelerini tarayarak aktif İzleme Listesini (Watchlist) günceller.     │
│ - Gece Brier Skoru kalibrasyonu ile güven eşiği ve kasa riskini ayarlar.   │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Aktif Watchlist ve Rejim Güncellemesi
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ DETERMINİSTİK DURUM MOTORU (Saf Python / NumPy / TA-Lib — Sıfır Sızıntı)   │
│ - Yalnızca KAPANMIŞ mumları (iloc[-2]) kullanarak gelecek verisi sızıntısını│
│   (future leakage) tamamen engeller.                                       │
│ - EMA(9/21/50/200), RSI(14), ATR(14), ADX(14), Bollinger Bantları, Hacim   │
│   ve Emir Defteri Dengesizliğini (Orderbook Imbalance) hesaplar.           │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ StateSnapshotSchema (<50ms)
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ KATMAN 2: THE REFLEX (System 1 — Hızlı Karar ve Sinyal Puanlayıcı)         │
│ - Her 60 saniyede bir izleme listesindeki pariteleri <50ms içinde puanlar. │
│ - Setup Quality (0-3), Yön Güveni (%0-%100), Giriş, Stop-Loss ve           │
│   Take-Profit fiyatlarını deterministik olarak üretir.                     │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ ReflexDecisionSchema
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ SERT RİSK KAPISI & İDEMPOTENT EMİR İLETİMİ (Hard Risk Gate & Execution)    │
│ - Kriz rejimi, toksik emir akışı, maksimum açık pozisyon ve günlük zarar   │
│   limitlerini denetler.                                                    │
│ - Quarter-Kelly (0.25x Kelly) formülü ile pozisyon büyüklüğünü sınırlar.   │
│ - Alım emri gerçekleştiği anda Binance sunucusuna STOP_LOSS_LIMIT emrini   │
│   kurar. 3 denemede Stop-Loss kurulamazsa pozisyonu anında piyasa          │
│   fiyatından kapatarak korumasız pozisyon bırakmaz.                        │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Öne Çıkan Güvenlik ve Koruma Mekanizmaları

1. **Sıfır Korumasız Pozisyon (Mandatory Server-Side Stop-Loss):**
   Açılan her Spot Long pozisyonu için anında Binance üzerinde `STOP_LOSS_LIMIT` emri oluşturulur. Sunucu kapansa dahi stop emri borsa tarafında aktif kalır.
2. **Nedensellik Garantisi (Strict Causality — Zero Future Leakage):**
   İndikatör hesaplamalarında henüz kapanmamış aktif mum (`iloc[-1]`) asla kullanılmaz; yalnızca kesinleşmiş kapanmış mumlar (`iloc[:-1]`) değerlendirilir.
3. **İdempotent Emir Gönderimi (`clientOrderId`):**
   Ağ zaman aşımlarında mükerrer (çift) alım yapılmasını önlemek için her emir deterministik bir `newClientOrderId` ile sorgulanır.
4. **Çift Yönlü Telegram Botu ve Acil Durum (Kill-Switch):**
   Alım/satım bildirimleri anlık olarak Telegram'a iletilir. Yalnızca yetkili `TELEGRAM_CHAT_ID` üzerinden gelen komutlar kabul edilir (`/durum`, `/liste`, `/pozisyonlar`, `/audit`, `/panic`, `/baslat`).
5. **Çoklu Anahtarlı OpenRouter Havuzu ve Deterministik Yedekleme:**
   1–3 adet ücretsiz OpenRouter API anahtarını dönüşümlü (round-robin) kullanır. `HTTP 429` veya zaman aşımı durumunda işlem döngüsünü aksatmadan saf matematiksel yedeklemeye (deterministic fallback) geçer.

---

## Proje Dizin Yapısı

```text
QuantBtc/
├── QUANT_BOT_SPEC.md               # Sistem mimarisi ve teknik spesifikasyon dokümanı
├── KURULUM_VE_TASIMA_REHBERI.md    # Sunucu taşıma ve hızlı kurulum kılavuzu
└── quant-bot-live/
    ├── .env.example                # Örnek ortam değişkenleri şablonu (Kişisel veri içermez)
    ├── install_ubuntu.sh           # Tek komutla Ubuntu kurulum ve servis betiği
    ├── preflight_check.py          # Canlı öncesi DB, NTP, Binance ve OpenRouter test betiği
    ├── main.py                     # Ana orkestratör (Brain, Reflex, Reconciler, Dashboard, Telegram)
    ├── config/
    │   └── settings.py             # Pydantic tabanlı tip güvenli yapılandırma
    ├── database/
    │   └── db_manager.py           # Thread-safe DuckDB veri katmanı
    ├── engine/
    │   ├── exchange.py             # CCXT Binance Spot asenkron sarmalayıcı ve saat senkronizasyonu
    │   ├── scanner.py              # Otonom piyasa tarayıcı ($50M hacim, %0.15 spread) ve rejim tespiti
    │   ├── state_engine.py         # Nedensel (causal) teknik indikatör ve orderbook motoru
    │   ├── decision_reflex.py      # <50ms deterministik sinyal puanlayıcı
    │   ├── risk_engine.py          # Quarter-Kelly ve sert risk kapısı + Kill-Switch
    │   ├── execution.py            # İdempotent emir iletimi ve zorunlu sunucu taraflı Stop-Loss
    │   ├── reconciler.py           # 30 saniyelik bakiye, TP ve SL senkronizasyonu
    │   ├── openrouter_pool.py      # Çoklu anahtar rotasyonlu LLM havuzu
    │   └── telegram_bot.py         # Çift yönlü güvenli Telegram bildirim ve komut modülü
    ├── review/
    │   └── overnight_review.py     # Gece 00:05 UTC Brier Skoru öz-kalibrasyon motoru
    ├── dashboard/
    │   ├── server.py               # HTTP Basic Auth korumalı FastAPI sunucusu
    │   └── static/index.html       # TradingView Lightweight Charts tabanlı canlı arayüz
    └── tests/                      # Birim ve entegrasyon testleri (pytest)
```

---

## Hızlı Kurulum (Ubuntu 22.04 / 24.04 LTS)

> **Önemli Sunucu Bölgesi Notu:** Binance Global (`api.binance.com`), ABD (`us-*`) veri merkezi IP'lerinden gelen özel API isteklerini `HTTP 451` ile engellemektedir. Canlı emir iletimi için sunucunuzu **Avrupa (`europe-west1`, `europe-west3`, `europe-west12`)** veya **Asya (`asia-northeast1`, `asia-southeast1`)** bölgelerinde konumlandırın.

### 1. Depoyu Klonlayın ve `.env` Dosyasını Oluşturun

```bash
git clone https://github.com/emrekayahan/QuantBtc.git
cd QuantBtc/quant-bot-live
cp .env.example .env
nano .env
```

`.env` dosyasındaki ilgili alanlara kendi anahtarlarınızı girin:

```ini
BINANCE_API_KEY=your_binance_spot_api_key_here
BINANCE_API_SECRET=your_binance_spot_api_secret_here

OPENROUTER_API_KEY_1=sk-or-v1-first_free_key
OPENROUTER_API_KEY_2=sk-or-v1-second_free_key
OPENROUTER_API_KEY_3=sk-or-v1-third_free_key

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change_me_strong_password
```

### 2. Tek Komutla Otomatik Kurulum (`install_ubuntu.sh`)

Betik; `chrony` (NTP saat senkronizasyonu), `uv`, Python 3.12 sanal ortamı (`.venv`), bağımlılıklar, birim testleri (`pytest`), ön kontrol (`preflight_check.py`) ve `quant-bot.service` Systemd servisini otomatik olarak kurup başlatır:

```bash
chmod +x install_ubuntu.sh
bash install_ubuntu.sh
```

### 3. Alternatif: Docker ile Çalıştırma

```bash
cd QuantBtc/quant-bot-live
docker compose run --rm quant-bot python preflight_check.py
docker compose up -d --build
```

---

## Web Dashboard ve Telegram Kullanımı

### Web Dashboard (`Port 8000`)
Sunucu portunu dış dünyaya açmadan güvenli erişim için yerel bilgisayarınızdan SSH tüneli başlatın:

```bash
ssh -L 8000:127.0.0.1:8000 <kullanici>@<sunucu-ip>
```
Tarayıcınızdan `http://localhost:8000` adresine gidin ve `.env` dosyanızda belirlediğiniz `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` ile giriş yapın.

### Telegram Bot Komutları
- `/durum` veya `/status` — Toplam bakiye, serbest USDT, günlük PnL, drawdown ve piyasa rejimi.
- `/liste` veya `/watchlist` — Aktif izleme listesindeki pariteler, fiyat, RSI(14), kalite ve güven skorları.
- `/pozisyonlar` veya `/pos` — Açık pozisyonlar, giriş fiyatı, Take-Profit ve Stop-Loss seviyeleri.
- `/audit` veya `/brier` — Gece 00:05 UTC Brier kalibrasyon raporu.
- `/panic` veya `/stop` — Tüm açık emirleri iptal eder, pozisyonları piyasa fiyatından kapatır ve botu kilitler (`HALTED`).
- `/baslat` veya `/resume` — Durdurulmuş sistemi yeniden aktif duruma geçirir.

---

## Testlerin Çalıştırılması

```bash
cd QuantBtc/quant-bot-live
./.venv/bin/pytest -v
./.venv/bin/python preflight_check.py
```

---

## Güvenlik İlkeleri (Public Repo Uyumluluğu)

- `.env`, `*.db`, `*.log`, `*.tar.gz` ve yedek dosyaları `.gitignore` ile versiyon kontrolü dışında tutulmaktadır.
- `.env.example` ve kaynak kod içerisinde hiçbir gerçek API anahtarı, parola, IP adresi veya kişisel Telegram ID'si yer almaz.
- Web arayüzü sabit zamanlı (`secrets.compare_digest`) HTTP Basic Auth ile, Telegram komut işleyicisi ise katı `TELEGRAM_CHAT_ID` doğrulaması ile korunmaktadır.
