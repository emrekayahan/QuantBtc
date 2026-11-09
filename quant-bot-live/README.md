# QuantBtc — Autonomous Quantitative Cryptocurrency Trading Engine (v3.0)

<p align="center">
  <strong><a href="#english">🇬🇧 English Documentation</a></strong> &nbsp;|&nbsp;
  <strong><a href="#türkçe">🇹🇷 Türkçe Dokümantasyon</a></strong>
</p>

---

<a name="english"></a>

## 🇬🇧 English Documentation

**QuantBtc (`quant-bot-live`)** is a 24/7 autonomous, dual-layer quantitative cryptocurrency trading engine, deterministic risk management framework, protected Web Dashboard, and two-way Telegram remote control system built for **Binance Spot**.

### Key Features & Dual-Layer Architecture

To strictly decouple deep macro research from sub-50ms execution and prevent any LLM hallucinations from touching risk limits or position math, the system operates on a **Dual-Layer (Brain & Reflex)** architecture:

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ LAYER 1: THE BRAIN (System 2 — Macro Regime, Market Scanner & Calibration) │
│ - Runs every 1 hour and overnight at 00:05 UTC.                            │
│ - Classifies BTC/ETH Macro Regime ('trending', 'mean_reverting',           │
│   'high_vol', 'crisis') via deterministic math + OpenRouter Multi-Key Pool.│
│ - Scans Binance Spot for top liquid USDT pairs ($50M+ 24h volume,          │
│   <= 0.15% spread, excluding stablecoins & leveraged tokens).              │
│ - Performs Overnight Brier Score self-calibration to tune risk & thresholds│
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Updates Approved Watchlist & Regime
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC STATE ENGINE (Pure Python / NumPy / TA-Lib — Zero Leakage)   │
│ - Evaluates strictly CLOSED candles (iloc[-2]) to guarantee zero future    │
│   data leakage.                                                            │
│ - Computes EMA(9/21/50/200), RSI(14), ATR(14), ADX(14), Bollinger Bands,   │
│   Volume Surge, and Orderbook Imbalance.                                   │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ StateSnapshotSchema (<50ms)
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ LAYER 2: THE REFLEX (System 1 — Fast Deterministic Signal Scorer)          │
│ - Evaluates watchlist candidates every 60 seconds in <50ms.                │
│ - Outputs Setup Quality (0-3), Direction Confidence (0%-100%), Entry,      │
│   Invalidation (Stop-Loss), and Take-Profit levels deterministically.      │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ ReflexDecisionSchema
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ HARD RISK GATE & IDEMPOTENT EXECUTION ENGINE                               │
│ - Enforces crisis regime blocks, toxic orderflow filters, max open         │
│   positions, daily max loss, and max drawdown guardrails.                  │
│ - Sizes positions using fractional Quarter-Kelly (0.25x Kelly) capped by   │
│   MAX_POSITION_USD.                                                        │
│ - Immediately places a Server-Side STOP_LOSS_LIMIT order on Binance upon   │
│   entry fill. If Stop-Loss placement fails after 3 retries, immediately    │
│   market-sells the position (Zero Unprotected Exposure).                   │
└────────────────────────────────────────────────────────────────────────────┘
```

### Core Safety & Engineering Guarantees

1. **Zero Unprotected Exposure (Mandatory Server-Side Stop-Loss):**
   Every filled Spot Long entry immediately places a `STOP_LOSS_LIMIT` order directly on Binance servers. Even if the server loses power or connectivity, your stop-loss remains active on the exchange.
2. **Strict Causality (Zero Future Leakage):**
   Indicator calculations never inspect the currently forming candle (`iloc[-1]`); all features are computed strictly from closed historical bars (`iloc[:-1]`).
3. **Idempotent Order Execution (`clientOrderId`):**
   Every entry and stop-loss order uses a deterministic `newClientOrderId` and queries open/recent orders on network timeouts to prevent duplicate fills.
4. **Two-Way Telegram Bot & Armed Emergency Kill-Switch:**
   Real-time entry/exit alerts and interactive remote commands (`/status`, `/watchlist`, `/pos`, `/brier`, `/panic`, `/resume`) restricted strictly to the operator's authorized `TELEGRAM_CHAT_ID`.
5. **Multi-Key OpenRouter Pool with Deterministic Fallback:**
   Rotates across 1–3 free OpenRouter API keys (`HTTP 429` aware) and falls back instantaneously to pure deterministic math if external LLM providers time out.

### Repository Structure

```text
QuantBtc/
├── QUANT_BOT_SPEC.md               # Master system specification & architectural blueprint
├── KURULUM_VE_TASIMA_REHBERI.md    # Server migration & deployment guide
└── quant-bot-live/
    ├── .env.example                # Sanitized environment variable template
    ├── install_ubuntu.sh           # One-command automated Ubuntu installer & systemd setup
    ├── preflight_check.py          # Pre-flight verification (DuckDB, NTP drift, Binance, OpenRouter)
    ├── main.py                     # Async orchestrator (Brain, Reflex, Reconciler, Dashboard, Telegram)
    ├── config/
    │   └── settings.py             # Pydantic v2 type-safe settings & validation
    ├── database/
    │   └── db_manager.py           # Thread-safe DuckDB persistence layer
    ├── engine/
    │   ├── exchange.py             # Async CCXT Binance Spot wrapper & NTP clock drift monitor
    │   ├── scanner.py              # Autonomous market scanner ($50M vol, 0.15% spread) & regime classifier
    │   ├── state_engine.py         # Causal technical indicator & orderbook state engine
    │   ├── decision_reflex.py      # Sub-50ms deterministic setup scorer
    │   ├── risk_engine.py          # Quarter-Kelly sizing, hard risk gate & emergency Kill-Switch
    │   ├── execution.py            # Idempotent order execution & mandatory server-side Stop-Loss
    │   ├── reconciler.py           # 30-second balance, Take-Profit & Stop-Loss state reconciler
    │   ├── openrouter_pool.py      # Multi-key rotating OpenRouter client with instant math fallback
    │   └── telegram_bot.py         # Non-blocking Telegram notifier & authenticated command handler
    ├── review/
    │   └── overnight_review.py     # 00:05 UTC Overnight Brier Score self-calibration engine
    ├── dashboard/
    │   ├── server.py               # HTTP Basic Auth protected FastAPI web server
    │   └── static/index.html       # TradingView Lightweight Charts real-time dashboard
    └── tests/                      # Comprehensive pytest unit & integration suite
```

### Quick Start (Ubuntu 22.04 / 24.04 LTS)

> **Important Server Region Note:** Binance Global (`api.binance.com`) blocks private trading API requests originating from US datacenter IP addresses (`us-*`) with `HTTP 451`. Deploy your server in **Europe (`europe-west1`, `europe-west3`, `europe-west12`)** or **Asia (`asia-northeast1`, `asia-southeast1`)** for live order execution.

#### 1. Clone the Repository & Configure `.env`

```bash
git clone https://github.com/emrekayahan/QuantBtc.git
cd QuantBtc/quant-bot-live
cp .env.example .env
nano .env
```

Fill in your credentials in `.env`:

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

#### 2. One-Command Automated Setup (`install_ubuntu.sh`)

The installer automatically provisions `chrony` (NTP clock sync), `uv`, Python 3.12 (`.venv`), dependencies, runs the `pytest` suite and `preflight_check.py`, and registers/starts the `quant-bot.service` systemd unit:

```bash
chmod +x install_ubuntu.sh
bash install_ubuntu.sh
```

#### 3. Alternative: Run with Docker Compose

```bash
cd QuantBtc/quant-bot-live
docker compose run --rm quant-bot python preflight_check.py
docker compose up -d --build
```

### Web Dashboard & Telegram Commands

#### Accessing the Web Dashboard (`Port 8000`)
To access the dashboard securely without exposing port `8000` to the public internet, open an SSH tunnel from your local machine:

```bash
ssh -L 8000:127.0.0.1:8000 <user>@<server-ip>
```
Then open `http://localhost:8000` in your browser and sign in with `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`.

#### Telegram Bot Commands
- `/status` or `/durum` — Live total equity, free USDT, daily PnL, drawdown %, and macro regime summary.
- `/watchlist` or `/liste` — Top liquid pairs on the active watchlist with price, RSI(14), setup quality, and confidence %.
- `/pos` or `/pozisyonlar` — Open positions, entry price, Take-Profit, and Server-Side Stop-Loss levels.
- `/brier` or `/audit` — Latest 00:05 UTC Overnight Brier calibration metrics.
- `/panic` or `/stop` — Cancels all open orders on Binance, market-sells open positions, and locks the engine (`HALTED`).
- `/resume` or `/baslat` — Resumes autonomous trading after a manual halt.

### Running Tests

```bash
cd QuantBtc/quant-bot-live
./.venv/bin/pytest -v
./.venv/bin/python preflight_check.py
```

---

<a name="türkçe"></a>

## 🇹🇷 Türkçe Dokümantasyon

**QuantBtc (`quant-bot-live`)**, Binance Spot piyasası için tasarlanmış, 7/24 otonom çalışan, çift katmanlı (Dual-Layer) kantitatif algoritmik alım-satım motoru, risk yönetim sistemi, yerel/uzak Web Dashboard ve çift yönlü Telegram kontrol platformudur.

### Temel Özellikler ve Mimari

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

### Öne Çıkan Güvenlik ve Koruma Mekanizmaları

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

### Hızlı Kurulum (Ubuntu 22.04 / 24.04 LTS)

> **Önemli Sunucu Bölgesi Notu:** Binance Global (`api.binance.com`), ABD (`us-*`) veri merkezi IP'lerinden gelen özel API isteklerini `HTTP 451` ile engellemektedir. Canlı emir iletimi için sunucunuzu **Avrupa (`europe-west1`, `europe-west3`, `europe-west12`)** veya **Asya (`asia-northeast1`, `asia-southeast1`)** bölgelerinde konumlandırın.

#### 1. Depoyu Klonlayın ve `.env` Dosyasını Oluşturun

```bash
git clone https://github.com/emrekayahan/QuantBtc.git
cd QuantBtc/quant-bot-live
cp .env.example .env
nano .env
```

#### 2. Tek Komutla Otomatik Kurulum (`install_ubuntu.sh`)

```bash
chmod +x install_ubuntu.sh
bash install_ubuntu.sh
```

#### 3. Alternatif: Docker ile Çalıştırma

```bash
cd QuantBtc/quant-bot-live
docker compose run --rm quant-bot python preflight_check.py
docker compose up -d --build
```

### Web Dashboard ve Telegram Kullanımı

- **SSH Tüneli ile Web Dashboard (`Port 8000`):**
  ```bash
  ssh -L 8000:127.0.0.1:8000 <kullanici>@<sunucu-ip>
  ```
  Tarayıcınızdan `http://localhost:8000` adresine gidin ve `.env` dosyanızda belirlediğiniz `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` ile giriş yapın.
- **Telegram Komutları:** `/durum` (`/status`), `/liste` (`/watchlist`), `/pozisyonlar` (`/pos`), `/audit` (`/brier`), `/panic` (`/stop`), `/baslat` (`/resume`).

### Güvenlik İlkeleri (Public Repo Uyumluluğu)

- `.env`, `*.db`, `*.log`, `*.tar.gz` ve yedek dosyaları `.gitignore` ile versiyon kontrolü dışında tutulmaktadır.
- `.env.example` ve kaynak kod içerisinde hiçbir gerçek API anahtarı, parola, IP adresi veya kişisel Telegram ID'si yer almaz.
- Web arayüzü sabit zamanlı (`secrets.compare_digest`) HTTP Basic Auth ile, Telegram komut işleyicisi ise katı `TELEGRAM_CHAT_ID` doğrulaması ile korunmaktadır.
