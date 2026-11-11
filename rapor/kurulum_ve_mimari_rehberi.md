# QuantBot Live v3.1 — Baştan Sona Kurulum, Mimari ve Operasyon Kılavuzu

Bu belge, **QuantBot Live v3.1** otonom kantitatif alım-satım botunun sunucu altyapısı, kurulum adımları, borsa API entegrasyonu, yazılım mimarisi, hata çözümlemeleri ve operasyonel komutlarının eksiksiz dökümantasyonudur.

---

## 1. Sunucu ve Sistem Altyapısı

- **Sunucu Host / IP:** Google Cloud Platform (GCP) Compute Engine — `<SERVER_HOST>` (`<SERVER_IP>`)
- **İşletim Sistemi:** Ubuntu Linux 22.04 LTS (x86_64)
- **Kullanıcı:** `<USER>`
- **Uygulama Dizini:** `/home/<USER>/btc/quant-bot-live`
- **Sanal Ortam (Virtualenv):** `/home/<USER>/btc/quant-bot-live/.venv` (Python 3.12.16)
- **Servis Yöneticisi:** `systemd` (`quant-bot.service`)
- **Veritabanı:** DuckDB embedded dosya tabanlı veritabanı (`data/quant_bot.db`, `PRAGMA memory_limit='256MB'`)
- **Web Dashboard:** FastAPI / Uvicorn sunucusu (`127.0.0.1:8000` — SSH tüneli `ssh -L 8000:127.0.0.1:8000 <SERVER_HOST>` ve HTTP Basic Auth korumalı)
- **GitHub Deposu:** `git@github.com:<GITHUB_USER>/QuantBtc.git` (Dal: `main`)

---

## 2. Sıfırdan Sunucu Kurulum Adımları (Installation Runbook)

### 2.1. Temel Sistem Paketleri ve Python 3.12
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl wget build-essential python3-dev python3-pip python3-venv chrony
```

### 2.2. Saat Eşitlemesi (NTP / Chrony)
Binance API milisaniyelik zaman damgası (timestamp) kontrolü yaptığı için saat farkı kritik öneme sahiptir:
```bash
sudo systemctl enable --now chrony
chronyc tracking
```

### 2.3. Deponun Klonlanması ve Sanal Ortam Kurulumu
```bash
mkdir -p /home/<USER>/btc
cd /home/<USER>/btc
git clone git@github.com:<GITHUB_USER>/QuantBtc.git quant-bot-live
cd quant-bot-live

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.4. Ortam Değişkenleri (`.env`) Yapılandırması
`/home/<USER>/btc/quant-bot-live/.env` dosyasında aşağıdaki alanlar tanımlanmıştır:
```ini
# İşlem Modu (Güvenli Varsayılan: paper)
TRADING_MODE="paper"
ENABLE_FUTURES=false

# Borsa Kimlik Bilgileri
BINANCE_API_KEY="<OPERATOR_BINANCE_API_KEY>"
BINANCE_API_SECRET="<OPERATOR_BINANCE_API_SECRET>"

# LLM Derin Analiz ve Denetim (OpenRouter Ücretsiz Havuz)
OPENROUTER_API_KEY_1="<OPENROUTER_KEY_1>"
OPENROUTER_API_KEY_2=""
OPENROUTER_API_KEY_3=""
OPENROUTER_MODELS="nvidia/nemotron-3-ultra-550b-a55b:free,liquid/lfm-3-1.7b:free,google/gemma-4-31b-it:free"

# Telegram Bot Bildirim & Uzaktan Kontrol
TELEGRAM_BOT_TOKEN="<TELEGRAM_BOT_TOKEN>"
TELEGRAM_CHAT_ID="<TELEGRAM_AUTHORIZED_CHAT_ID>"

# Dashboard Güvenliği (Varsayılan 127.0.0.1 — SSH Tüneli İle Erişilir)
DASHBOARD_HOST="127.0.0.1"
DASHBOARD_PORT=8000
DASHBOARD_USERNAME="<DASHBOARD_OPERATOR_USER>"
DASHBOARD_PASSWORD="<STRONG_16_CHAR_PASSWORD>"

# Risk ve Strateji Parametreleri (Güvenli Varsayılanlar)
TIMEFRAME="15m"
MAX_OPEN_POSITIONS=2
MAX_POSITION_USD=25.0
SPOT_MIN_POSITION_USD=11.50
FUTURES_MIN_USDT=3.0
FUTURES_DEFAULT_LEVERAGE=3
FUTURES_MARGIN_USD=4.50
DAILY_MAX_TRADES=6
DAILY_MAX_LOSS_USD=5.0
MAX_DRAWDOWN_PCT=15.0
MIN_24H_VOLUME_USD=10000000.0
MAX_SPREAD_PCT=0.20
MIN_CONFIDENCE_THRESHOLD=0.68
MIN_SETUP_QUALITY=2
TOP_WATCHLIST_SIZE=20
```

### 2.5. Systemd Arka Plan Servisi Kurulumu
Servis dosyasının oluşturulması (`/etc/systemd/system/quant-bot.service`):
```ini
[Unit]
Description=Autonomous Quantitative Trading Engine v3.1 & Protected Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<USER>
Group=<USER>
WorkingDirectory=/home/<USER>/btc/quant-bot-live
EnvironmentFile=/home/<USER>/btc/quant-bot-live/.env
ExecStart=/home/<USER>/btc/quant-bot-live/.venv/bin/python /home/<USER>/btc/quant-bot-live/main.py
Restart=always
RestartSec=5
TimeoutStopSec=90
MemoryHigh=380M
MemoryMax=450M
StandardOutput=append:/home/<USER>/btc/quant-bot-live/logs/systemd.log
StandardError=append:/home/<USER>/btc/quant-bot-live/logs/systemd.log

[Install]
WantedBy=multi-user.target
```

Servisin yetkilendirilmesi ve başlatılması:
```bash
sudo systemctl daemon-reload
sudo systemctl enable quant-bot.service
sudo systemctl start quant-bot.service
sudo systemctl status quant-bot.service
```

---

## 3. Sistem Mimarisi ve Çalışma Mantığı

QuantBot Live, 2 katmanlı hibrit karar mekanizması ve çift cüzdan yönetimiyle çalışır:

```
[Binance Ticker / OHLCV] -> [Layer 1: Brain Scanner (1h)] -> En Likit 20 Parite + Rejim Tespiti
                                       ↓
                           [Layer 2: Reflex Scorer (60s)] -> RSI, BB, Hacim (Q >= 2, Güven >= %68)
                                       ↓
                           [Pre-Trade Deep Analyzer] -> Tahta Derinliği (OB) + Hata Hafızası Kontrolü
                                       ↓
                           [Dual-Wallet Risk & Execution Gate]
                            ├── Futures Serbest Bakiye >= $3.00 -> ⚡ FUTURES 20x ISOLATED
                            └── Futures Bakiye < $3.00 (Teminat Dolu) -> 🪙 SPOT ($11.50 Min Notional)
                                       ↓
                           [Server-Side Algo Stop-Loss & TP Placement]
                                       ↓
                           [State Reconciler (30s Döngü)] -> Canlı Bakiye, Algo SL/TP Mutabakatı
                                       ↓
                           [Mistake Learner & Brier Reviewer] -> Hatalardan Ders Çıkarma & Otonom Kalibrasyon
```

### 3.1. Katman 1: Brain Scanner (`engine/scanner.py`)
- Saatlik döngüde çalışır.
- USDT paritelerinden 24 saatlik hacmi $10M üzerinde ve makası <%0.20 olan en likit 20 kripto pariteyi seçer.
- Sentetik hisse senetlerini (`NVDA`, `TSLA`, `B` uzantılı tokenized varlıklar) filtreler.
- BTC/ETH trend ve ADX yapısına göre piyasa rejimini belirler (`trending`, `mean_reverting`, `high_vol`).

### 3.2. Katman 2: Reflex Scorer (`engine/decision_reflex.py`)
- Her 60 saniyede bir 15 dakikalık yeni kapanan mumları tarar (<50ms gecikme).
- Kurulum Kalitesi (Q): 1-3 puan arasında skorlanır (RSI, Bollinger Alt Bandı, Alış Hacim Onayı).
- Minimum Güven Eşiği: %68 (`0.68`).

### 3.3. Çift Cüzdan Yürütme Motoru (Dual-Wallet Engine — `engine/execution.py` & `exchange.py`)
- **Otomatik Algılama:** Her döngüde hem Spot hem de USD-M Futures cüzdanı sorgulanır.
- Futures serbest bakiyesi >= $3.00 ise işlem otomatik olarak **20x Isolated Futures** modunda açılır (~$4.50 teminat ile ~$90 pozisyon büyüklüğü).
- Futures serbest bakiyesi < $3.00 ise (tüm teminat açık işlemlerde bağlıysa), bot durmak yerine Spot cüzdanındaki bakiyeden Binance'in minNotional filtrelerini aşacak şekilde **en az $11.50'lık Spot işlem** açar.
- Her işleme aynı anda borsada **Server-Side Stop-Loss (Algo/Conditional)** emri bağlanır.

### 3.4. Mutabakat Motoru (State Reconciler — `engine/reconciler.py`)
- Her 30 saniyede bir borsayla veritabanını senkronize eder.
- Binance Futures Algo emirlerini (`params={"stop": True}`) sorgulayarak SL gerçekleşmelerini anında yakalar.
- Yazılımsal acil SL yedek mekanizması barındırır.
- Kaldıraçlı pozisyonların çift sayılmasını önleyerek net kasa büyüklüğünü doğru hesaplar.

### 3.5. Hata Öğrenme Döngüsü (Mistake Learner — `engine/mistake_learner.py`)
- Stop-Loss ile kapanan her pozisyonun giriş RSI'ı, rejim durumu ve emir defteri dengesizliği kaydedilir (`trade_lessons` tablosu).
- Benzer hatalı kurulumlar tekrarlandığında sisteme otonom güven cezası uygulanır veya 2 saatlik geçici soğuma kuralı devreye girer.

---

## 4. Karşılaşılan Kritik Sorunlar ve Çözümleri

| Sorun | Kök Neden | Uygulanan Çözüm |
| :--- | :--- | :--- |
| **Binance API `-2015 Invalid API-key`** | API anahtarında IP kısıtlaması olmaması veya Futures izninin kapalı olması. | IP adresi `<SERVER_IP>` olarak sabitlendi; Spot & Margin ve Enable Futures izinleri aktifleştirildi (Withdraw kapalı). |
| **Futures `market symbol not found`** | CCXT Vadeli İşlemler kütüphanesinin lineer sürekli kontratlar için `:USDT` formatı beklemesi. | `resolve_futures_symbol()` fonksiyonu eklenerek `SOL/USDT` -> `SOL/USDT:USDT` eşlemesi yapıldı. |
| **Kaldıraçlı Kasa Çift Sayımı ($51.59 vs $30.47)** | `reconciler.py`'nin toplam nakit bakiyenin üzerine kaldıraçlı kontrat büyüklüklerini de eklemesi. | Futures cüzdan toplamının (`futures_total_usdt`) zaten kâr/zarar ve teminatı içerdiği dikkate alınarak pozisyon değeri eklemesi sadece Spot varlıklarla sınırlandırıldı. |
| **Vadeli SL Emirlerinin Kapanmaması** | Binance Futures'ın `STOP_MARKET` emirlerini standart emir değil, **Algo Order** olarak saklaması. | `fetch_futures_order` fonksiyonuna `params={"stop": True}` desteği eklendi; tetiklenen SL'ler anında yakalanıp kapatıldı. |
| **BNB `orders.csv` Sahte `filled=0.13` Kaydı** | `execution.py:275` içinde `0.0 or entry_order.get("amount")` ifadesinin `filled=0.0` iken emir miktarına düşmesi. | Gerçek `filled` okuması ayrıştırıldı; dolmayan limit emirlerin TTL sonunda iptal edilip `status=canceled, filled=0.0` kaydedilmesi sağlandı. |
| **Mistake Learner Prompt Echo (`"The user wants..."`)** | `openrouter_pool.py:130` içinde `msg_obj.get("content") or msg_obj.get("reasoning")` kullanılması. | `reasoning` fallback'i kaldırıldı; yalnızca doğrulanmış tek cümle Türkçe `content` veya deterministik şablon kabul ediliyor. |
| **DuckDB Lock Çakışması (`IOException`)** | DuckDB'nin tek process erişimine izin vermesi nedeniyle dış script'lerin servis çalışırken kilitlenmesi. | Script'lerin veritabanının anlık kopyası (`/tmp/quant_bot_snap.db`) üzerinden çalışması sağlandı. |

---

## 5. Operasyon ve Telegram Kontrol Komutları

Botla Telegram üzerinden 7/24 etkileşim kurulabilir:

- `/durum` veya `/status`: Kasa bakiyesi (Spot/Futures), aktif işlem modu, açık pozisyon sayısı, drawdown ve rejim özeti.
- `/pozisyonlar` veya `/pos`: Açık işlemler, işlem türü (`⚡ FUTURES` / `🪙 SPOT`), giriş fiyatı, anlık fiyat ve K/Z durumu.
- `/liste` veya `/watchlist`: Taranan pariteler, anlık RSI değerleri ve kalite puanları.
- `/dersler` veya `/lessons`: Mistake Learner tarafından kaydedilen hata dersleri ve ceza puanları.
- `/brier` veya `/audit`: 00:05 UTC gece Brier kalibrasyon denetim raporu.
- `/panic` veya `/stop`: Acil Durum — Tüm açık pozisyonları piyasa fiyatından kapatır, SL emirlerini iptal eder ve motoru kilitler.
- `/baslat` veya `/resume`: Durdurulmuş botu yeniden aktif eder.

---

## 6. Sık Kullanılan Sunucu Bakım Komutları

```bash
# Canlı logları anlık takip etme
sudo journalctl -u quant-bot.service -f

# Servisi yeniden başlatma
sudo systemctl restart quant-bot.service

# Servis durumunu kontrol etme
sudo systemctl status quant-bot.service

# Birim testleri çalıştırma
cd /home/<USER>/btc/quant-bot-live && ./.venv/bin/pytest -q

# Veritabanını yedekleme
cp /home/<USER>/btc/quant-bot-live/data/quant_bot.db ~/quant_bot_backup_$(date +%F).db
```
