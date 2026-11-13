# QuantBot Live — 2. Tur: Doğrulanmış Hatalar + Otonom Broker Yol Haritası

> **Bu dosya Antigravity (AI kodlama ajanı) içindir.**
> Repo: `QuantBtc` → `quant-bot-live/` (Python 3.12, async CCXT, DuckDB, FastAPI, systemd).
> Girdi: `fix/safety-and-calibration` dalının incelenmesi (21 test geçiyor) + deneysel doğrulamalar.
> Önceki plan: `QUANTBOT_FIX_PLAN.md` (T0–T14). Bu dosya onun **devamıdır**; çakışmada bu dosya geçerlidir.

---

## 0. Ajan için çalışma kuralları (ZORUNLU)

1. **Canlı servise, sunucudaki `.env`'e ve gerçek emirlere dokunma.** Hiçbir testte gerçek borsa çağrısı yok; sahte (fake) exchange kullan.
2. **Gizli bilgi yazdırma/commit etme:** API anahtarı, token, şifre, **sunucu IP'si**, kullanıcı adı, ev dizini yolları.
3. Yeni branch: `git checkout -b fix/round2`. Her görev (`R1`, `R2`, …) ayrı commit: `fix(Rn): ...` / `feat(Rn): ...`.
4. **Önce testi yaz, hatayı kanıtla (kırmızı), sonra düzelt (yeşil).** Bölüm 9'daki regresyon testleri zorunlu.
5. Yeni davranışlar config bayrağıyla gelsin; **varsayılanlar güvenli taraf** olsun. Yeni ayarları `config/settings.py` ve `.env.example`'a ekle.
6. Ayarlarda tanımlı ama kodda **kullanılmayan** (ölü) hiçbir ayar kalmasın. Her ayar için en az bir kullanım noktası ve test olsun.
7. P0 → P1 → P2 sırasına uy. **P0 bitmeden P1'e geçme.** Her faz sonunda `pytest -q` yeşil, kısa özet ver ve onay bekle.
8. Emin olmadığın yerde varsayım yapma: `TODO(user):` bırak ve sonda listele.

---

## 1. Vizyon ve gerçekçi çerçeve

**Operatörün hedefi:** Sistem bir *broker gibi* kendi kendine çalışsın: spot **ve** kaldıraçlı (futures) pazarda pozisyon açsın/kapatsın, hatalarından öğrensin; operatör yalnızca bakiyeyi izlesin.

**Bu hedefi mümkün kılan ve plana işlenen ilkeler:**

| İlke | Anlamı |
|---|---|
| **Otonom ≠ denetimsiz** | Bot karar verir; ama *sınırları kodu değiştirmeden aşamaz.* Operatör müdahalesi gerekmesin diye güvenlik katmanı otomatik ve katıdır. |
| **Kaldıraç risk değil, araçtır** | Pozisyon boyutu **riske göre** (bakiyenin %'si) hesaplanır; kaldıraç yalnızca gereken teminatı azaltır. 3x de 20x de aynı SL riskini taşır; fark likidasyon mesafesindedir (R3). |
| **Öğrenme = kanıta dayalı** | Tek işlemden ders çıkarma yok. Tüm sinyaller sonuçlandırılır, istatistiksel yeterlilik (n) sağlanmadan parametre/strateji değişmez (R10–R13). |
| **Otonomi merdiveni** | Sistem, kanıt biriktikçe kendi yetkisini artırır; performans/anomali bozulunca otomatik düşürür (R17). |
| **Kendi kendini izleme** | Watchdog, anomali dedektörleri, güvenli mod ve günlük "broker raporu" (R18). |

**Dürüst uyarı (koda da yansıyacak):** Edge (kârlı strateji) henüz **kanıtlanmadı**: bugüne dek kapanan tek işlem var. Hiçbir otomasyon kâr garantilemez. Bu yüzden otonomi merdiveninin ilk basamakları *paper* ve *mikro sermaye*dir; kaldıraç ve strateji genişlemesi yalnızca ölçülmüş sonuçla açılır. Ayrıca $30 bakiyede minNotional ve komisyon etkisi büyüktür (Bölüm 1.1).

### 1.1 $30 hesap gerçekleri (tasarımı belirleyen sınırlar)
- **Risk bütçesi:** işlem başına %1 = **$0.30**. SL mesafesi %1.5 ise izin verilen notional ≈ **$20**.
- **Spot minNotional** çoğu çiftte ~$5–10; bot şu an $11.50'ya yuvarlıyor.
- **Futures minNotional** sembole göre değişir (BTC/ETH gibi büyük kontratlarda daha yüksek olabilir). `ccxt` market `limits.cost.min` okunmalı; `minNotional × SL_mesafesi > risk bütçesi` ise **işlem atlanmalı**, boyut şişirilmemeli.
- **Komisyon:** spot ~%0.10/taraf, futures taker ~%0.05/taraf. Round-trip maliyeti R'ye oranla büyüktür (SL %1 ise komisyon ≈ 0.1–0.2R). Edge, maliyetten büyük olmalı.
- **Rastgele yürüyüşte** R:R 2.5 ile TP'nin SL'den önce vurma olasılığı ≈ %28.6; başa baş için ~%31–34 gerekir. Sinyalin rastgeleden **3–6 puan** iyi olduğu ölçülmeden canlı büyütme yapılmaz (R10, R17).

### 1.2 Küçük bakiye modu: kaldıraçlı işlem kurallarının yumuşatılması

İlk taslaktaki katı kurallar ($30 hesapta fiilen çoğu futures kurulumunu reddediyordu): futures yalnızca L2'de açılıyordu, kaldıraç tavanı 3x, likidasyon tamponu 3×SL, brüt maruziyet 1.0×equity (bu, kaldıracı fiilen imkânsız kılıyordu), işlem riski %1 ve minNotional bu bütçeyi aşarsa işlem reddediliyordu. **Bunlar aşağıdaki gibi yumuşatıldı:**

| Kural | Eski | Yeni (küçük hesap: equity < `SMALL_ACCOUNT_EQUITY_USD`=100) |
|---|---|---|
| Futures'a erişim | L2 | **L1'den itibaren** (izole mikro-futures) |
| Kaldıraç tavanı | 3x | **L1=5x, L2=10x, L3=20x** (`LEVERAGE_CAP_BY_LEVEL`) |
| Likidasyon tamponu (`LIQ_BUFFER_X`) | 3.0 | **2.0** |
| İşlem başına risk | %1 | **%1.5** (`SMALL_ACCOUNT_MAX_RISK_PCT`) |
| minNotional zorunluluğu | bütçe aşılırsa reddet | **%3'e kadar esnetilir** (`MIN_NOTIONAL_RISK_EXCEPTION_PCT`), ötesinde reddet |
| Brüt maruziyet (`MAX_GROSS_EXPOSURE_X`) | 1.0 | **3.0** (ikincil sınır; asıl sınır toplam açık risk ve margin tavanı) |
| Toplam açık risk | %3 | **%5** |
| Günlük kayıp limiti | %3 | **%5** |
| Açık pozisyon | 2 | **3** |
| Sembol seçimi | uygun olmayanı atla | scanner minNotional×SL ≤ risk bütçesi olan **"affordable" sembolleri** önceler |

**Pazarlık dışı kalanlar (yumuşatılmaz):**
1. Yalnızca **isolated margin**; her pozisyonda **borsa tarafında SL** (`reduceOnly`, `MARK_PRICE`) ve R4/R5 korumaları.
2. Boyut **riske göre** hesaplanır (sabit margin × kaldıraç yok). Kaldıraç yalnızca gereken teminatı azaltır.
3. **Likidasyon fiyatı ≥ `LIQ_BUFFER_X` × SL mesafesi**; pozisyon açıldıktan sonra borsadan dönen `liquidationPrice` ile tekrar doğrulanır, ihlalde pozisyon hemen kapatılır.
4. **Margin tavanı:** işlem başına izole margin ≤ equity × `MAX_MARGIN_PCT_PER_TRADE` (%25). İzole margin en kötü durum kaybını margin ile sınırlar.
5. Günlük kayıp, drawdown ve toplam açık risk limitleri; funding eşiği; reconcile/safe_mode; otonomi merdiveninde otomatik düşüş.

**Bedeli (kodda ve raporda açıkça gösterilsin):** Yumuşatma beklenen kaybı büyütür. $30'da %1.5 risk ≈ $0.45, minNotional istisnasında %3 ≈ $0.90 demektir. Art arda 5 SL ≈ %7–15 kayıp; `MAX_DRAWDOWN_PCT=15` bu noktada devreye girer. BTC gibi yüksek minNotional'lı kontratlar $30'da %3 istisnasına rağmen yine reddedilebilir (örn. $100 notional × %1.2 SL = $1.2 ≈ %4); bot bu sembolleri atlar, boyutu şişirmez.

---

## 2. Birinci turun durumu (kod incelemesi sonucu)

| Görev | Durum | Not |
|---|---|---|
| T0 paper/live | **Kısmen** | `execution` ve `reconciler` korumalı; **kill-switch / panic korumasız**; paper equity bozuk |
| T1 kısmi dolum | **Kısmen (iyi)** | TTL + iptal + son sorgu var; SL TTL döngüsünden sonra kuruluyor (≤90 sn korumasız) |
| T2 borsa↔DB mutabakat | **Yok** | Borsada olup DB'de olmayan hiçbir şey aranmıyor |
| T3 kaldıraç/maruziyet | **Kısmen** | `ENABLE_FUTURES`, `MAX_LEVERAGE` var; brüt sınır yeni işlemi saymıyor; risk % ayarları ölü |
| T4 güvenli kapanış | **Kozmetik** | "grace" = 1 sn `sleep`; devam eden emir akışını beklemiyor; `deploy.sh` yok |
| T5 Brier | **Kısmen** | N=0 doğru; `MIN_BRIER_SAMPLES` yok; N/A 0.0 olarak kaydediliyor |
| T6 mistake learner | **Kısmen** | Filtre var; min örnek/TTL/desen yok; entry özellikleri yanlış kaynaktan |
| T7 sinyal sonuçları/backtest | **Yok** | `tools/`, `outcome_resolver` yok |
| T8 sinyal sıkılaştırma | **Yok** | ayar yok |
| T9–T12 | **Yok** | SL tampon, zaman çıkışı, saat/bellek korumaları |
| T13 dashboard | **Kısmen** | varsayılan host `127.0.0.1` ✔; zayıf/boş şifre kabul; hız sınırı yok |
| T14 doküman | **Yok** | README v3.0/Spot; `.env.example` eski |

---

# BÖLÜM A — P0: Doğrulanmış güvenlik ve doğruluk hataları

> Her hata aşağıda deneyle doğrulandı. Yeniden üretim kodu Bölüm 9'da.

## R1 — Merkezi canlı-emir koruması ve `/panic` (T0 tamamlama)

**Problem (doğrulandı):** Paper korumaları yalnızca `ExecutionEngine.execute_approved_entry`, `close_single_position_market` ve reconciler'da. `BinanceExchangeWrapper.create_order / create_market_sell_order / cancel_order / cancel_all_orders / create_futures_order` **korumasız**. `RiskEngine.trigger_emergency_kill_switch` (Telegram `/panic`, dashboard `/api/kill-switch`) paper modda bile borsaya **gerçek** market sell gönderiyor. Testte `market_sell_calls=1` ölçüldü.

**Yapılacaklar**
1. `engine/exchange.py`: tüm **yazan** metotların başına tek bir `_assert_live_or_raise()` koy: `cfg.is_paper` ise `PaperModeViolation` istisnası fırlat (loglayıp **asla** gönderme). Okuma metotları serbest.
2. Kill-switch'i mod duyarlı yap: paper'da yalnızca DB'deki paper pozisyonları kapat; live'da borsa işlemlerini yap.
3. **Kill-switch futures'ı da kapatsın (R6 ile birlikte).**
4. Başlangıçta mod kilidi: `system_state.trading_mode` kaydet. Sonraki açılışta `TRADING_MODE` farklıysa **başlamayı reddet** (açık pozisyon varsa) ve Telegram'a "mod değişimi için pozisyonları kapat ve `scripts/switch_mode.py` çalıştır" mesajı gönder. Paper ve live için **ayrı DB dosyası** (`DB_PATH` mod son ekiyle: `quant_bot_paper.db`, `quant_bot_live.db`).
5. `.env`'de `TRADING_MODE` yoksa **sessizce paper'a düşme**; açılışta büyük uyarı logu + Telegram bildirimi (`MODE=PAPER (varsayılan)`).

**Testler:** `test_paper_never_writes_exchange` (tüm yazan metotlar için), `test_panic_in_paper_does_not_call_exchange`, `test_mode_mismatch_refuses_start`.
**Kabul:** Paper modda fake exchange'te toplam yazan çağrı sayısı 0.

## R2 — Paper hesap doğruluğu ve gerçekçi dolum modeli

**Problem (doğrulandı):** Paper equity, $11.50'luk pozisyon açılınca $30 yerine **$41.34** görünüyor (`total_equity = wallet + daily_pnl + positions_value`, nakit maliyeti düşülmüyor → çift sayım). Ayrıca `paper_wallet_balance_usdt` günlük PnL'yi biriktirmiyor (UTC gece yarısı sıfırlanıyor); paper `drawdown_pct` sabit 0.0; her emir anında tam doluyor.

**Yapılacaklar**
1. Paper cüzdanı kalıcı bir **ledger** olsun: `paper_ledger(ts, event, symbol, amount, balance_after)`. `paper_equity = nakit + açık_pozisyon_güncel_değeri (+ futures için unrealized PnL + margin)`. Günlük PnL'den türetme.
2. Peak equity ve drawdown paper'da da hesaplansın; `MAX_DRAWDOWN_PCT` paper'da da tetiklensin.
3. **Dolum modeli (`engine/paper_fill.py`):** limit emri yalnızca sonraki mumun low'u limit fiyatına değerse dolsun (muhafazakâr: aynı mumda SL ve TP görünürse **SL önce**). Komisyon (`SPOT_FEE_PCT`, `FUTURES_FEE_PCT`) ve `SLIPPAGE_PCT` uygulansın; kısmi dolum seçeneği (`PAPER_PARTIAL_FILL_PROB`) olsun. Stop dolumu stop'tan kayma ile (`SL_LIMIT_BUFFER_PCT`) simüle edilsin.
4. Paper'da futures: margin, kaldıraç, **likidasyon fiyatı** ve funding (basit sabit oran) simüle edilsin.

**Testler:** `test_paper_equity_no_double_count` ($30 → pozisyon aç → equity ≈ $29.9, fee dahil), `test_paper_pnl_persists_across_utc_days`, `test_paper_limit_not_filled_if_low_not_touched`, `test_paper_sl_before_tp_same_candle`.
**Kabul:** Paper kasası ledger ile birebir tutuyor; fill oranı %100 değil.

## R3 — Riske dayalı boyutlandırma, brüt maruziyet ve kaldıraç mühendisliği (T3 tamamlama)

**Problem (doğrulandı):** `evaluate_gate` mevcut notional'ı `max_gross` ile kıyaslıyor ama **yeni pozisyonu eklemiyor**: equity $30, açık notional $29 iken yeni $11.50 onaylandı (1.35×). `MAX_RISK_PER_TRADE_PCT`, `MAX_TOTAL_OPEN_RISK_PCT`, `SLIPPAGE_PCT`, `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED` ayarları kodda **kullanılmıyor**. Spot boyutu minNotional'a, futures boyutu `FUTURES_MARGIN_USD × kaldıraç`a bağlı; risk hesaba girmiyor.

**Yapılacaklar — tek bir `RiskSizer` (saf fonksiyon, test edilebilir):**
```
girdiler: equity, entry, sl, market_type(spot|futures), min_notional, fees, slippage,
          açık pozisyonların (notional, sl_riski) listesi, config
risk_usd_bütçe   = equity * etkin_risk_pct/100      # küçük hesapta SMALL_ACCOUNT_MAX_RISK_PCT (bkz. 1.2)
birim_risk       = |entry-sl|/entry + 2*fee_pct + slippage_pct        # fiyat yüzdesi
notional         = min(risk_usd_bütçe / birim_risk, MAX_POSITION_USD)
# kısıtlar
1) notional >= min_notional*1.10   yoksa:  küçük hesap modunda risk, MIN_NOTIONAL_RISK_EXCEPTION_PCT'ye
   kadar esnetilir; bu sınırı da aşıyorsa REDDET ("minNotional risk bütçesini aşıyor")
2) (açık_notional + notional) <= equity * MAX_GROSS_EXPOSURE_X       # YENİ DAHİL
3) (açık_risk + risk(notional)) <= equity * MAX_TOTAL_OPEN_RISK_PCT/100
4) spot: nakit >= notional*1.001
5) futures: kaldıraç L seç:  L = clamp( ceil(notional / kullanılabilir_margin_payı), 1, MAX_LEVERAGE )
            margin = notional / L
            likidasyon_mesafesi ≈ (1/L) - bakım_marjini                # long için
            ŞART: likidasyon_mesafesi >= LIQ_BUFFER_X * SL_mesafesi    # varsayılan 2x (bkz. 1.2)
            şart sağlanmazsa L düşür; L=1'de de sağlanmıyorsa REDDET
            ŞART: margin <= equity * MAX_MARGIN_PCT_PER_TRADE/100     # izole margin = en kötü kayıp sınırı
            L <= LEVERAGE_CAP_BY_LEVEL[autonomy_level]
            açılıştan sonra borsadaki liquidationPrice tekrar doğrulanır; tamponu ihlal ediyorsa pozisyon hemen kapatılır
```
- Futures boyutu **sabit $4.50 margin × kaldıraç** mantığından çıkacak; yukarıdaki risk-tabanlı formülden gelecek. Aynı SL riski, daha az margin.
- **Dinamik günlük kayıp limiti:** `DAILY_MAX_LOSS_PCT` (varsayılan %3; küçük hesap modunda %5); mevcut sabit `DAILY_MAX_LOSS_USD=5` ($30'da %16!) yalnızca üst sınır olarak kalsın (`min`).
- `MAX_CORRELATED_LONGS` ayrı sayılsın: aynı yönde ve korele (USDT çiftleri) pozisyonlar; `MAX_OPEN_POSITIONS`'tan bağımsız doğrulansın.
- Gate sonucuna `risk_usd`, `risk_pct`, `leverage`, `liq_distance_pct`, `reject_reason` (makine okunur kod) ekle; `reflex_decisions`'a yaz.
- Kelly'yi ya kaldır ya da yalnızca **kalibre edilmiş** olasılıkla (R11, n≥100) kullan; kalibre değilse sabit %1 risk.

**Testler:** `test_gross_exposure_includes_new_trade` (`MAX_GROSS_EXPOSURE_X=1.0` ile equity 30, açık 29 → yeni işlem **reddedilir**), `test_total_open_risk_cap`, `test_futures_leverage_respects_liq_buffer`, `test_skip_when_min_notional_exceeds_risk_budget`, `test_small_account_min_notional_exception` (risk ≤ %3 ise kabul, üstü red), `test_margin_cap_per_trade`, `test_liq_price_verified_after_open`, `test_leverage_cap_by_level`, `test_no_dead_settings` (tüm `Settings` alanlarının koda referansı var mı — basit grep testi).
**Kabul:** Hiçbir kombinasyonda brüt notional/equity > `MAX_GROSS_EXPOSURE_X`, tek işlem riski > etkin risk yüzdesi (küçük hesapta `SMALL_ACCOUNT_MAX_RISK_PCT`, minNotional istisnasında en çok `MIN_NOTIONAL_RISK_EXCEPTION_PCT`), izole margin > `MAX_MARGIN_PCT_PER_TRADE` ve likidasyon tamponu < `LIQ_BUFFER_X` olmuyor.

## R4 — Borsa genelinde mutabakat (T2 tamamlama)

**Problem:** Reconciler yalnızca DB'deki pozisyonlara bakıyor. Borsada olup DB'de olmayan emir/pozisyon (restart anında yarım kalan, elle açılan, kayıt hatası) **hiç görülmüyor**. Ayrıca DB'deki pozisyonun SL emrinin **hâlâ aktif olduğu** doğrulanmıyor.

**Yapılacaklar (`engine/reconciler.py`, her 30 sn):**
1. **Borsadan çek:** spot bakiyeler (USDT dışı varlıklar), spot açık emirler, futures pozisyonları (`positionAmt`, `entryPrice`, `isolatedMargin`, `liquidationPrice`), futures açık/algo emirler.
2. **Eşleştir** (sembol+miktar+yön) ve şu invariantları kontrol et:
   - **I1:** Borsadaki her pozisyon DB'de olmalı. Yoksa → `ORPHAN_POSITION`.
   - **I2:** DB'deki her açık pozisyon borsada olmalı. Yoksa → `PHANTOM_POSITION` (kapat/işaretle).
   - **I3:** Her pozisyonun **aktif** SL emri olmalı ve miktarı pozisyonu kapsamalı (≥ %99). Yoksa → SL'yi yeniden kur.
   - **I4:** SL durumu `canceled/expired/rejected` ise → hemen yeniden kur.
   - **I5:** Entry emri `open/partially_filled` ve `filled>0` ise → o miktar için SL kur.
3. **Otomatik iyileştirme önceliği:** korumasız/yetim pozisyon → önce SL kur; kuramazsan market ile kapat. Her ihlal `reconcile_events` tablosuna ve Telegram'a (**acil**) gitsin.
4. **Giriş bloklama:** çözülmemiş ihlal varken `entries_blocked=true` (yeni giriş yok, korumalar çalışır). Çözülünce otomatik açılır.
5. **Başlangıç kurtarma:** `run_startup_recovery` aynı mantığı **borsa taramasıyla** çalıştırsın (yalnızca DB değil). Şu an `OPEN_UNPROTECTED` için SL kurmayı denemeden doğrudan kapatıyor; önce SL dene, olmazsa kapat.
6. `/status` ve dashboard: pozisyon başına **tür (spot/futures), kaldıraç, margin, notional, likidasyon fiyatı, SL emri durumu**; `reconcile_ok` ve son kontrol zamanı. (Bugün `/status` futures satırı "$0.86 toplam / $0 işlemde" gösterirken borsada margin kullanımı vardı; bakiye kalemleri borsa API'siyle (`totalMarginBalance`, `availableBalance`) birebir hizalanmalı.)

**Testler:** fake exchange ile I1…I5'in her biri; `test_orphan_position_gets_sl_or_closed`, `test_canceled_sl_is_rearmed`, `test_entries_blocked_until_reconciled`.
**Kabul:** `kill -9` + yarım kalmış entry senaryosunda 30 sn içinde yetim pozisyon korunuyor veya kapanıyor.

## R5 — SL garantisi: kısmi dolum, kapatma sırası, devam eden emirler (T1/T4 tamamlama)

**Problemler (kodda doğrulandı):**
- Kısmi dolumda SL, TTL döngüsü bittikten sonra kuruluyor → ≤`ENTRY_ORDER_TTL_SEC` (90 sn) korumasız.
- `close_single_position_market`: önce SL iptal ediliyor, sonra market satış; **satış hata verirse istisna yutuluyor ve DB'de pozisyon "kapandı" işaretleniyor** (borsada pozisyon SL'siz açık kalıyor).
- `main.py` kapanışında "in-flight görevleri bekle" yerine `asyncio.sleep(1.0)`; TTL döngüsündeki entry kesilirse yarım kalan pozisyon korumasız bırakılabilir.

**Yapılacaklar**
1. **Dolum anında koruma:** Her polling turunda `filled` arttıkça (ilk >0 dolumda) **hemen** o miktar için SL kur/güncelle. TTL sonunda iptal + son miktara göre SL miktarını düzelt. Hedef: dolumdan SL'ye ≤ 5 sn.
2. **Kapatma sırası:**
   - **Futures:** önce `reduceOnly` market sat; dolumu **doğrula**; sonra SL iptal et.
   - **Spot:** SL kilitli bakiyeyi tuttuğu için iptal gerekir → iptal et → satışı yap; satış başarısızsa **SL'yi derhal yeniden kur** ve alarm ver. Satış dolumu doğrulanmadan DB'de kapatma **yok**.
   - Sonuç belirsizse (timeout) `clientOrderId` ile sorgula; yine belirsizse `CLOSE_UNCERTAIN` durumu + reconciler çözsün.
3. **Devam eden işlemleri izle:** `ExecutionEngine` aktif entry/close görevlerini bir `TaskRegistry`'de tutsun. SIGTERM'de: yeni giriş durdur → registry'deki görevleri `SHUTDOWN_GRACE_SEC`'e kadar bekle → hâlâ bitmediyse **korumasız kalmaması için** SL kur/kapat → sonra çık. `quant-bot.service`'e `TimeoutStopSec=90` ekle.
4. **Entry kayıt hatası regresyonu:** `filled` değerinde `or amount` gibi yedekler **yasak**; borsadan gelmeyen değer 0 sayılır. (Önceki BNB "hayalet dolum" hatasının kalıcı kilidi.)
5. **Futures→spot yedek yolu (`execution.py`):** kodda sabit `11.50` ve `FALLBACK…` bayrağını yok sayan geri düşüş var; ayrıca futures emri **zaman aşımı** ile belirsiz kaldığında spot'a geçmek çift maruziyet yaratabilir. Düzelt: yedek yol yalnızca `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED=true` ise ve **risk kapısından yeniden geçerek** çalışsın; zaman aşımında önce `fetch_order_by_client_order_id` ile futures emrinin durumunu kesinleştir.
6. `scripts/deploy.sh`: testler → preflight → (açık/bekleyen emir veya korumasız pozisyon yoksa) DB yedeği → restart. Varsa uyar ve çık.

**Testler:** `test_partial_fill_gets_sl_within_poll`, `test_close_failure_does_not_mark_closed`, `test_spot_close_failure_rearms_sl`, `test_sigterm_waits_for_inflight_entry`, `test_no_filled_fallback_to_amount`, `test_timeout_does_not_trigger_double_exposure`.

## R6 — Kill-switch ve manuel kapatma futures'ı da kapatsın

**Problem:** Kill-switch yalnızca spot `cancel_all_orders` + spot market sell kullanıyor. Futures pozisyonlarında spot satış denemesi hata veriyor (veya kullanıcının gerçek spot varlığını satabilir); pozisyon açık kalıyor.

**Yapılacaklar:** Kill-switch pozisyonu **`market_type`'a göre** kapatsın: futures → `reduceOnly` market + algo/SL iptal; spot → SL iptal + market sat. Önce kapat-doğrula, sonra DB. Hataları listele, kapatılamayanları `CLOSE_UNCERTAIN` bırak ve `entries_blocked` tut.
**Test:** `test_panic_closes_futures_reduce_only`, `test_panic_never_sells_unrelated_spot_balance`.

## R7 — Gizli bilgi ve dashboard sertleştirme (T13 tamamlama)

- `engine/execution.py` içindeki Telegram hata metninde **sabit sunucu IP'si** var (public repo!). Kaldır; IP'yi mesajda gösterme. Repo geçmişinde kalacağı için **IP'yi Binance'te yenilemeyi/kısıtı gözden geçirmeyi** kullanıcıya hatırlat; geçmişten temizleme için `git filter-repo`/BFG notu ekle.
- Repo'da **üç kopya** `QUANTBOT_FIX_PLAN.md` var (kök, `rapor/`, `quant-bot-live/`) ve `/home/<kullanıcı>/…` yolları içeriyor. Tek kopya bırak, yolları `<USER>` yer tutucusuna çevir.
- Dashboard: açılışta şu durumlarda **başlamayı reddet:** boş şifre, `change_me_*` veya <16 karakter, kullanıcı adı `admin`. IP başına dakikada 5 başarısız giriş sınırı + gecikme. Yazma uç noktaları (`kill-switch`, `resume`, `close`) için ek `X-Confirm` başlığı/CSRF jetonu. Dış erişim varsa HTTPS (ters vekil) zorunluluğu uyarısı; varsayılan `127.0.0.1` kalsın. Başarısız-giriş logu.
- Log filtresi: anahtar/secret/token maskeleme (test ile).
- `.env.example`'ı **tüm yeni ayarlarla** güncelle (aşağıdaki Bölüm 8 tablosu).

---

# BÖLÜM B — P1/P2: Otonom broker mimarisi

## R10 — Tüm sinyaller için sonuç takibi ve backtest (T7)

**Amaç:** Edge'i ölçmek ve öğrenmenin veri temelini kurmak. Şu an 19 sinyalden yalnızca yürütülenlerin (çoğunda boş) sonucu var.

1. Tablo `signal_outcomes`: `decision_id, symbol, strategy_id, side(long|short), entry, sl, tp, horizon_bars, result(TP|SL|TIMEOUT), r_gross, r_net, bars_to_exit, mae_r, mfe_r, regime, features_json, resolved_at_ms`.
2. `engine/outcome_resolver.py` (30 sn–5 dk arayla): her çözülmemiş sinyali sonraki 15m mumlarıyla çözsün. **Aynı mumda SL ve TP görülürse SL önce.** Zaman aşımı `SIGNAL_HORIZON_BARS` (32). Net R = brüt R − komisyon − `SLIPPAGE_PCT`. Her `Q≥1` sinyal (yürütülen ya da değil) kaydedilsin.
3. `tools/backtest_signals.py --csv … | --fetch-symbol BTC/USDT --days 30`: **aynı** saf `score()` fonksiyonunu çağırsın (canlı ve backtest tek kod). Yalnızca kapanmış mumlar; walk-forward bölme (`--train 20d --test 10d`); çıktı: n, isabet, ortalama net R, beklenen değer ± güven aralığı, maks. drawdown, strateji/rejim kırılımı.
4. `tools/signal_report.py --since 7d`: canlı/paper sinyal istatistikleri.

**Kabul:** 24 saatlik çalışma sonunda tüm sinyaller sonuçlanmış; rapor üretiliyor.

## R11 — Gerçek kalibrasyon (T5 tamamlama)

- `MIN_BRIER_SAMPLES` (varsayılan 30). `n < min` → durum `INSUFFICIENT_DATA`, **hiçbir parametre değişmez**; Brier DB'ye **NULL** yazılsın (şu an 0.0 yazılıyor ve `/brier` bunu skor gibi gösterebiliyor).
- Örnek kaynağı `signal_outcomes` (yalnızca yürütülenler değil). Çıktı: Brier, **güvenilirlik (reliability) tablosu** (0.6–0.7, 0.7–0.8, 0.8+ kovaları: n ve gerçek isabet), strateji başına ayrı skor.
- `direction_confidence`'ı **kural puanı** olarak yeniden adlandır (`signal_score`); n≥100 ve her kovada yeterli örnek olunca **isotonic/Platt** kalibrasyonla `calibrated_p` üret ve Kelly/eşik bu değeri kullansın. Kalibre değilken boyutlandırmada olasılık **kullanılmasın** (R3).
- Eşik tek doğruluk kaynağı `system_state.min_confidence_threshold`; her değişiklik `calibration_log` tablosuna (eski, yeni, sebep, n) yazılsın, Telegram'a bildirilsin. Otomatik adım ±0.02, aralık [0.60, 0.85]; `MAX_POSITION_USD` otomatik **artırılamaz**.
- "Well-calibrated" dalı bugün eşiği baz değere sıfırlıyor; tek şanslı işlem eşiği düşürmesin (min örnek + histerezis).

## R12 — Hata öğrenme motoru v2 (T6 tamamlama)

**Problem:** `entry_rsi`/`orderbook_imbalance` işlem **kapanırken** alınan "son snapshot"tan okunuyor (pozisyonda giriş özellikleri yok) → "giriş hatası" yanlış etiketleniyor. Tek kayıt ceza üretiyor; TTL/desen yok. Yalnızca SL'ler analiz ediliyor, kazançlar öğrenmeye girmiyor.

1. **Giriş anında özellik kaydı:** pozisyon açılırken `trade_features(position_id, strategy_id, regime, rsi, prev_rsi, adx, atr_pct, bb_dist_pct, ema_dist_pct, vol_ratio, ob_imbalance, spread_bps, btc_trend_state, hour_utc, side, leverage)` yaz. Öğrenme **yalnızca** bu tablodan.
2. **Tüm kapanan işlemler** (kazanç+kayıp) ve **sinyal sonuçları** öğrenme verisi. Her işlem için: `r_net, mae_r, mfe_r, çıkış nedeni, slippage`.
3. **İstatistik temelli kural çıkarımı (LLM'siz):** `pattern_key = (strategy_id, regime, rsi_bin, ob_sign, side)`. Bir desen **n ≥ `MIN_LESSONS_FOR_PENALTY` (örn. 8)** ve ortalama net R'nin güven aralığı üst sınırı < 0 ise **"kara liste/ceza"**; TTL (7 gün) sonunda yeniden **paper/shadow**'da test edilir, iyileşirse serbest kalır. Ceza üst sınırı toplam −0.10; tek işlem ceza **üretmez**.
4. **LLM yalnızca yorumcu:** açıklama metni üretir, **hiçbir karar veya parametreyi değiştirmez**. Çıktı doğrulaması: tek cümle, ≤200 karakter, Türkçe karakter oranı, prompt kalıpları yok; geçmezse şablon metin. `source` (`llm|template`) ve `validated` alanları. Mevcut bozuk kayıtlar migration ile `validated=false`, cezaları sıfır.
5. `/dersler` çıktısı: desen, n, ortalama R, güven aralığı, durum (aktif/askıda/serbest).

## R13 — Strateji kayıt defteri, şampiyon/meydan okuyucu ve otomatik devre dışı bırakma

Otonom sistemin "kendi kendine yönetimi" burada gerçekleşir.

1. `engine/strategies/` altında eklenti arayüzü: `Strategy.score(state) -> Signal` (saf, deterministik). İlk strateji seti: `trend_momentum`, `mean_reversion_dip`, `intraday_bounce` (mevcut), ve **R14'teki short stratejiler**.
2. Her strateji için **rolling performans:** son N=30/60/100 sinyal/işlem için net R ortalaması, isabet, drawdown, güven aralığı (`strategy_stats` tablosu).
3. **Otomatik yaşam döngüsü:** `shadow` (yalnızca sinyal kaydı) → `micro` (en küçük risk) → `active` (normal risk) → `suspended`. Geçişler **yalnızca metrikle:** örn. `shadow→micro`: n≥60 ve net R güven aralığı alt sınırı > 0; `active→suspended`: rolling-30 net R < 0 **ve** Brier bozuk. Askıdaki strateji shadow'da izlenir, kanıt dönünce kendiliğinden geri gelir.
4. **Sermaye tahsisi kanıtla orantılı:** strateji başına risk çarpanı (0.0–1.0), başlangıçta düşük; kanıt arttıkça yükselir (üst sınır `MAX_RISK_PER_TRADE_PCT`).
5. **Şampiyon/meydan okuyucu:** parametre değişikliği (örn. BB toleransı) yalnızca **shadow'da** paralel çalışır; walk-forward + canlı shadow'da şampiyonu geçerse terfi eder. Otomatik parametre taraması **overfit korumalı** (n, çoklu test düzeltmesi, `MAX_PARAM_CHANGE_PER_WEEK`).

## R14 — Sinyal sıkılaştırma, rejim filtresi ve short desteği (T8 + futures çift yön)

**Neden:** BTC EMA200 altındayken (10 Kasım: 73,473 < 76,258) bot yalnızca long arıyor; spec'e göre mean-reversion `mean_reverting` rejiminde ve EMA200 üstünde, trend momentum `trending` rejiminde çalışmalı, kodda ise `high_vol` rejiminde de çalışıyor.

1. **BB dip koşulu:** `low <= bb_lower*(1+BB_TOUCH_TOL)` (`BB_TOUCH_TOL≤0.002` ya da `0.25×ATR`). Mevcut kod `1.012/1.015` toleransı kullanıyor (ZEC `Low 1024` vs `BB_Lower 953` vakası artık sinyal üretmez; bunu test kilitlesin). `high_vol`'da `MIN_SETUP_QUALITY=3`.
2. **Momentum bounce:** `close − ema21 ≥ MOMENTUM_MIN_ATR_FRAC×ATR14` (0.3), `ADX ≥ ADX_MIN` (20), hacim teyidi zorunlu. BTC `%0.09`, XAUT `%0.05` gibi EMA21 farkıyla tetiklenen sinyaller elenir.
3. **Rejim filtresi:** `BLOCK_LONGS_BELOW_BTC_EMA200=true`; long mean-reversion yalnızca `RSI<30` + OB>0. "Düşen bıçak" filtresi (`24s değişim < −10%` → Q≥3 + soğuma). Stabil/altın-endeksli ve aşırı oynak (`>%40`) semboller hariç (config listesi).
4. **Short (yalnızca futures):** aşağı trendde (`BTC<EMA200`, `ADX≥20`, EMA9<EMA21) `trend_breakdown_short` ve aşırı alım `mean_reversion_fade_short` stratejileri (R13 kayıt defteri, başlangıç `shadow`). Short için aynı risk kapısı, likidasyon tamponu (`short` için üst taraf), funding maliyeti ve **spot'ta short yok** kuralı.
5. `Signal` şemasına `side` ve `strategy_id` ekle (`ReflexDecisionSchema` genişlet; `direction` zaten `short` içeriyor).

**Testler:** `test_zec_case_no_signal`, `test_btc_ema21_tiny_gap_no_signal`, `test_long_blocked_below_btc_ema200`, `test_short_signal_in_bear_regime`, `test_short_not_allowed_on_spot`.

## R15 — Pazar yönlendirici (spot mu, futures mı?) ve kaldıraç seçimi

Operatörün "hem spot hem kaldıraç" isteğinin deterministik karşılığı. `engine/market_router.py`:

```
girdi: signal(side, strategy_id, sl_dist, tp_dist), equity, spot/futures bakiye, minNotional'lar, ATR, funding
kurallar (sırayla):
1) side == short                       -> yalnızca FUTURES (spot short yok)
2) futures kapalı (ENABLE_FUTURES=false) -> SPOT (long)
3) RiskSizer (R3) iki pazarda da çalıştır:
     spot_ok     = notional >= spot_min  &&  spot nakdi yeterli
     futures_ok  = notional >= fut_min   &&  likidasyon tamponu sağlanıyor && margin yeterli
4) ikisi de uygunsa: maliyeti düşük olanı seç (komisyon + funding + slippage),
   kaldıraç gerektirmeyen (spot) çözümü tercih et
5) hiçbiri uygun değilse: işlemi ATLA ve nedenini yaz (reject_reason)
6) STRATEJİ risk çarpanı ve autonomy_level (R17) tavanını uygula
```
- Kaldıraç `1..LEVERAGE_CAP_BY_LEVEL[seviye]` aralığında RiskSizer'dan gelir; **sabit 20x yok**, kaldıraç her işlemde riskten türetilir. Tavanlar: **L1=5x, L2=10x, L3=20x** (mutlak tavan `MAX_LEVERAGE=20`). Fiili sınırlayıcı kaldıraç tavanı değil, **likidasyon tamponu (`LIQ_BUFFER_X=2.0`) ve margin tavanıdır** (bkz. 1.2).
- **Affordable sembol önceliği:** scanner her sembol için `min_notional × SL_dist ≤ risk_bütçesi` kontrolü yapıp `affordable` işaretler; küçük hesapta yönlendirici önce bu sembollere bakar. Hiçbiri uygun değilse işlem atlanır ve `reject_reason` yazılır.
- Futures için **ek canlı kontroller:** isolated margin, `reduceOnly` SL/TP, `MARK_PRICE` tetik, funding oranı eşiği (`MAX_FUNDING_PCT`), borsada sembol işlem durumu (`TRADING`), kaldıraç/margin ayarı doğrulaması (isteneni borsa kabul etti mi).
- Çıktı, `reflex_decisions`'a `market_type, leverage, risk_usd, router_reason` olarak yazılır.

## R16 — Çıkış yönetimi

- **Zaman çıkışı:** `MAX_HOLD_BARS` (32 bar). Dolunca pozisyon güvenli sırayla (R5) kapanır.
- **Başa-baş/iz süren stop:** `BREAKEVEN_TRIGGER_R=1.0` → SL giriş+komisyon seviyesine; opsiyonel `TRAIL_ATR_MULT`.
- **Kısmi TP:** `PARTIAL_TP_R=1.5` → pozisyonun yarısı; kalan iz süren stopla.
- **SL tipi:** spot `STOP_LOSS_LIMIT` limit tamponu `SL_LIMIT_BUFFER_PCT` (varsayılan 0.20) ayarlı; tetiklendi ama dolmadı ve fiyat limitin altındaysa `SL_ESCALATION_SEC` sonra **market**. Futures'ta `STOP_MARKET` (`MARK_PRICE`). Gerçekleşen kayma `positions.sl_slippage_pct`'e yazılsın ve risk hesabındaki `SLIPPAGE_PCT` kalibrasyonu buradan beslensin.
- Tüm çıkış kararları deterministik; LLM yok.

## R17 — Otonomi merdiveni (otomatik terfi/düşüş)

`system_state.autonomy_level` (0–3), seviye değişimleri `autonomy_log`'a ve Telegram'a yazılır. **Yalnızca ölçülmüş metrikle** yükselir; ihlalde otomatik düşer.

| Seviye | Yetki | Terfi koşulu (örnek eşikler, config) |
|---|---|---|
| **L0 Paper** | Tüm pazarlar simülasyon | Başlangıç |
| **L1 Mikro-canlı spot + mikro futures** | Gerçek para; spot long ve **izole futures** (long canlı, short shadow), kaldıraç ≤5x, işlem başına risk %0.5–1, `MAX_OPEN=2` | L0'da ≥7 gün ve ≥100 sinyal sonucu, net beklenen R > 0 (güven aralığı alt sınırı ≥ 0), `RECONCILE` ihlali = 0, "SL'siz" alarmı = 0 |
| **L2 Spot + futures (long/short)** | Risk %1–1.5, kaldıraç ≤10x, short canlı (shadow'da kanıtlandıysa), `MAX_OPEN=3` | L1'de ≥30 gerçek işlem, net R > 0, maks. drawdown < %8, Brier makul (n≥30) |
| **L3 Tam otonom** | Spot+futures, long/short, kaldıraç ≤20x (`LEVERAGE_CAP_BY_LEVEL`), strateji çarpanları | L2'de ≥60 işlem, rolling net R > 0, drawdown < %10, 14 gün anomalisiz |

**Otomatik düşürme:** drawdown eşiği, günlük kayıp limiti, reconcile ihlali, art arda hata/anomali (R18) veya strateji askıya alma → bir seviye düş / `safe_mode`. Terfi için **cooldown** (en az N gün) ve her terfide Telegram bildirimi.
> Başlangıç değeri **L0**'dır. Operatörün canlıya ilk geçişi tek elle adımdır: `TRADING_MODE=live` + `AUTONOMY_START_LEVEL=1`. Sonrası sistem kendi kanıtıyla ilerler.

## R18 — Kendi kendini izleme ve "broker raporu"

Operatör yalnızca bakiyeye baksın diye, sistem **kendi sağlığını** izler ve sorun çıkarsa **kendini güvenli moda** alır.

1. **Watchdog/heartbeat:** her döngü `last_heartbeat_ms`; harici basit kontrol (systemd `WatchdogSec` veya ayrı timer) 120 sn'den uzun sessizlikte servisi yeniden başlatır **yalnızca açık/bekleyen emir yoksa** (R5 deploy mantığı).
2. **Anomali dedektörleri:** saat kayması (`CLOCK_WARN_MS=250` uyarı, `CLOCK_BLOCK_MS=800` giriş yok), borsa hata oranı, emir reddi serisi, reconcile ihlali, LLM başarısızlığı serisi, bellek/swap eşiği (`MemoryHigh/Max` önerisi), equity ani sıçraması.
3. **Güvenli mod (`safe_mode`):** yeni giriş yok, korumalar ve çıkışlar çalışır; neden + çözüm Telegram'a. Anomali geçince otomatik normale döner (kalıcı kilit yalnızca `HALTED`).
4. **Günlük broker raporu (Telegram, 21:00 UTC + haftalık):** bakiye (spot/futures/toplam), günlük/haftalık net PnL (komisyon dahil), açık pozisyonlar (tür, kaldıraç, likidasyon mesafesi, SL durumu), dünün işlemleri, **öğrenme özeti** (yeni kural/ceza, askıya alınan/terfi eden strateji, Brier n=…), otonomi seviyesi, sistem sağlığı. Operatör için tek ekranlık özet.
5. `/durum` gerçek-zamanlı hâlâ var; `/saglik` sistem sağlığını verir.
6. Ayrıntılı olay günlüğü `events` tablosunda (karar → neden → sonuç), dashboard'da zaman çizgisi.

## R19 — Operasyon ve CI

- `scripts/deploy.sh`, `scripts/switch_mode.py`, `deploy/quant-bot.service.example` (`TimeoutStopSec=90`, `MemoryHigh=380M`, `MemoryMax=450M`, `WatchdogSec`).
- `preflight_check.py`: NTP kayması, RAM/swap, DB kilidi, zorunlu env, `TRADING_MODE`, API izin (withdraw kapalı mı), IP kısıtı.
- Bellek: OHLCV tamponları sembol başına sabit 500 bar; DuckDB `memory_limit`.
- Log rotasyonu; DB günlük yedeği (snapshot kopyası, kilit çakışması olmadan).
- GitHub Actions: `pytest -q` + `ruff`.
- README/SPEC/`.env.example` v3.1+ gerçeğiyle eşitle (futures, paper/live, otonomi seviyeleri). `CHANGELOG.md`.

---

# 8. Önerilen yeni/güncel ayarlar (`settings.py` + `.env.example`)

| Ayar | Varsayılan | Not |
|---|---|---|
| `TRADING_MODE` | `paper` | Canlı = elle; mod kilidi (R1) |
| `AUTONOMY_START_LEVEL` | `0` | R17 |
| `ENABLE_FUTURES` | `true` | `FUTURES_MIN_LEVEL` altında devreye girmez; L0'da (paper) simüle edilir |
| `FUTURES_MIN_LEVEL` | `1` | futures için gereken otonomi seviyesi |
| `MAX_LEVERAGE` | `20` | mutlak tavan; fiili tavan `LEVERAGE_CAP_BY_LEVEL` |
| `LEVERAGE_CAP_BY_LEVEL` | `{1:5, 2:10, 3:20}` | asıl sınır likidasyon tamponu |
| `LIQ_BUFFER_X` | `2.0` | likidasyon mesafesi ≥ 2× SL mesafesi (pazarlık dışı alt sınır: 2.0) |
| `MAX_RISK_PER_TRADE_PCT` | `1.0` | **kullanılacak**; L1'de ×0.5 çarpan |
| `SMALL_ACCOUNT_EQUITY_USD` | `100` | altında küçük hesap modu (bkz. 1.2) |
| `SMALL_ACCOUNT_MAX_RISK_PCT` | `1.5` | küçük modda etkin işlem riski |
| `MIN_NOTIONAL_RISK_EXCEPTION_PCT` | `3.0` | minNotional zorunluluğunda üst risk sınırı |
| `MAX_MARGIN_PCT_PER_TRADE` | `25` | izole margin / equity (en kötü kayıp sınırı) |
| `MAX_TOTAL_OPEN_RISK_PCT` | `3.0` (küçük modda `5.0`) | **kullanılacak** |
| `MAX_GROSS_EXPOSURE_X` | `3.0` | yeni notional dahil; ikincil sınır (spot-only'de nakit zaten 1.0'ı sınırlar) |
| `DAILY_MAX_LOSS_PCT` | `3.0` (küçük modda `5.0`) | USD limiti yalnızca üst sınır |
| `MAX_OPEN_POSITIONS` | `2` (küçük modda `3`) | |
| `MAX_CORRELATED_LONGS` | `2` | |
| `DAILY_MAX_TRADES` | `6` | **sunucu .env'inde 20 yazıyor, güncelle** |
| `SPOT_FEE_PCT` / `FUTURES_FEE_PCT` | `0.10` / `0.05` | taraf başına |
| `SLIPPAGE_PCT` | `0.05` | **kullanılacak**, R16 ile kalibre |
| `ENTRY_ORDER_TTL_SEC` | `90` | |
| `SL_LIMIT_BUFFER_PCT` | `0.20` | |
| `SL_ESCALATION_SEC` | `20` | |
| `MAX_HOLD_BARS` | `32` | |
| `MIN_BRIER_SAMPLES` | `30` | |
| `MIN_LESSONS_FOR_PENALTY` | `8` | |
| `SIGNAL_HORIZON_BARS` | `32` | |
| `BB_TOUCH_TOL` | `0.002` | |
| `MOMENTUM_MIN_ATR_FRAC` | `0.3` | |
| `ADX_MIN` | `20` | |
| `BLOCK_LONGS_BELOW_BTC_EMA200` | `true` | |
| `MAX_FUNDING_PCT` | `0.05` | 8s funding eşiği |
| `CLOCK_WARN_MS` / `CLOCK_BLOCK_MS` | `250` / `800` | |
| `DASHBOARD_HOST` | `127.0.0.1` | |
| `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED` | `false` | |

> **Sunucu uyarısı (kod dışı):** Sunucudaki mevcut `.env` `MAX_OPEN_POSITIONS=5`, `DAILY_MAX_TRADES=20`, `FUTURES_DEFAULT_LEVERAGE=20`, `FUTURES_MARGIN_USD=4.50` gibi **açık değerler içeriyor ve kod varsayılanlarını ezer.** Dağıtımdan önce bu satırları silin veya yukarıdaki değerlerle güncelleyin ve `TRADING_MODE`'u **açıkça** yazın.

---

# 9. Zorunlu regresyon ve davranış testleri

Aşağıdaki üç test, incelemede doğrulanan hataları kilitler. (Fake exchange: `tests/test_idempotency.py::StubCCXTClient`; `cancel_order` ve `fetch_tickers` eklenmeli.)

```python
# R1: paper modda panic gerçek emir göndermemeli
async def test_panic_in_paper_does_not_call_exchange(tmp_path):
    cfg = Settings(TRADING_MODE="paper", BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                   DB_PATH=str(tmp_path/"p.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    stub = Stub()                                  # market_sell_calls / cancels sayar
    ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
    risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)
    eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
    await eng.execute_approved_entry(decision=make_valid_decision("BTC/USDT"), free_usdt=30.0)
    await risk.trigger_emergency_kill_switch("test")
    assert stub.market_sell_calls == 0 and stub.cancels == 0      # ŞU AN: market_sell_calls == 1

# R2: paper equity çift sayılmamalı
    out = await reconciler.reconcile_cycle()
    assert 29.0 <= out["total_equity_usd"] <= 30.1                # ŞU AN: 41.34

# R3: brüt maruziyet yeni işlemi de saymalı
    db.set_state("total_equity_usd", "30.0")
    # cfg: MAX_GROSS_EXPOSURE_X=1.0 (spot-only senaryo); açık notional = 29.0 olan bir pozisyon ekle
    g = risk.evaluate_gate(decision=dec, free_usdt=30.0, min_notional_usd=10.0)
    assert g["approved"] is False                                  # ŞU AN: approved=True, 11.5 USD
```

Ek test listesi (özet): `test_paper_never_writes_exchange`, `test_mode_mismatch_refuses_start`, `test_paper_limit_not_filled_if_low_not_touched`, `test_total_open_risk_cap`, `test_futures_leverage_respects_liq_buffer`, `test_skip_when_min_notional_exceeds_risk_budget`, `test_no_dead_settings`, `test_orphan_position_gets_sl_or_closed`, `test_canceled_sl_is_rearmed`, `test_entries_blocked_until_reconciled`, `test_partial_fill_gets_sl_within_poll`, `test_close_failure_does_not_mark_closed`, `test_spot_close_failure_rearms_sl`, `test_sigterm_waits_for_inflight_entry`, `test_no_filled_fallback_to_amount`, `test_timeout_does_not_trigger_double_exposure`, `test_panic_closes_futures_reduce_only`, `test_brier_insufficient_data_changes_nothing`, `test_signal_outcomes_sl_first_same_candle`, `test_strategy_suspends_on_negative_rolling_r`, `test_autonomy_demotes_on_drawdown`, `test_zec_case_no_signal`, `test_long_blocked_below_btc_ema200`, `test_short_not_allowed_on_spot`, `test_market_router_picks_spot_when_futures_not_better`.

---

# 10. Uygulama sırası

| Faz | Görevler | Çıkış kriteri |
|---|---|---|
| **F1 (P0 güvenlik)** | R1, R2, R3, R5, R6, R7 | Bölüm 9 testleri yeşil; paper'da sıfır yazan çağrı; brüt/risk sınırları işliyor |
| **F2 (P0 mutabakat)** | R4 | `kill -9` senaryosunda yetim pozisyon ≤30 sn içinde korunuyor/kapanıyor |
| **F3 (ölçüm)** | R10, R11, R12 | `signal_outcomes` dolu, Brier gerçek, öğrenme giriş-anı özelliklerinden |
| **F4 (strateji/otonomi)** | R13, R14, R15, R16 | Strateji kayıt defteri, router, short (shadow), çıkış yönetimi |
| **F5 (otonomi/izleme)** | R17, R18, R19 | Merdiven + safe_mode + günlük rapor + CI |

**Her faz sonunda dur:** değişen dosyalar, eklenen testler, doğrulama adımları ve `TODO(user)` listesiyle özet ver; onay bekle.

---

# 11. Canlıya geçiş ve otonomi devri kriterleri (hepsi sağlanmalı)

1. `pytest -q` yeşil; Bölüm 9 testleri dahil.
2. L0 (paper) ≥ 7 gün: `signal_outcomes` n ≥ 100, net beklenen R > 0 (güven aralığı alt sınırı ≥ 0), reconcile ihlali = 0, "SL'siz pozisyon" = 0, `kill -9` kurtarma testi geçti.
3. Reliability tablosunda yüksek güven kovaları gerçekten daha yüksek isabet gösteriyor (n yeterli).
4. İlk canlı dönem: **L1** (spot + izole mikro-futures, kaldıraç ≤5x, işlem başına risk %0.5–1, en fazla 2 pozisyon). Terfiler R17 kurallarıyla **otomatik**.
5. Binance API anahtarında **withdraw kapalı**, IP kısıtı açık; dashboard dışarıya kapalı veya HTTPS arkasında.

---

# 12. Operatöre sorulacak açık noktalar (ajan çıktısının sonuna ekle)

1. Futures'ta short açılmasına izin veriliyor mu? (R14; varsayılan `shadow`)
2. Kaldıraç tavanları (L1=5x, L2=10x, L3=20x) ve küçük hesap risk değerleri (%1.5, minNotional istisnası %3) uygun mu? (1.2/R15/R17)
3. Günlük rapor saati ve kanalı (Telegram dışında e-posta?) (R18)
4. Hesapta BTC/ETH futures minNotional'ı risk bütçesini aşarsa o semboller atlanabilir mi? (R3)
5. Geçmiş 15m OHLCV CSV'leri sağlanabilir mi, yoksa `--fetch` ile public veri mi indirilsin? (R10)

---

# 13. Antigravity'ye yapıştırılacak başlangıç istemi

```
Repodaki FIX_PLAN_ROUND2.md dosyasını baştan sona oku ve Bölüm 0 kurallarına uy.
Yeni branch aç: fix/round2. Önce Bölüm 9'daki üç regresyon testini yaz ve kırmızı olduklarını göster
(paper panic gerçek emir gönderiyor, paper equity şişiyor, brüt maruziyet yeni işlemi saymıyor).
Sonra F1 fazını uygula: R1, R2, R3, R5, R6, R7 (sırayla). Her görev ayrı commit.
Canlı servise, sunucudaki .env'e ve gerçek emirlere ASLA dokunma; testlerde fake exchange kullan.
F1 bitince pytest -q çıktısını ve değişiklik özetini ver, TODO(user) listesini yaz ve onay bekle.
```
