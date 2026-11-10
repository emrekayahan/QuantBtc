# QuantBot Live v3.1 — Hata Düzeltme ve Sağlamlaştırma Planı (Kaynak Kod & Canlı Borsa Doğrulamalı)

> **Bu dosya bir AI kodlama ajanı (Antigravity) için yazılmıştır ve kaynak kod + canlı Binance API sorgularıyla satır satır doğrulanarak güncellenmiştir.**
> Hedef repo: `QuantBtc/quant-bot-live` (Python 3.12, async CCXT, DuckDB, FastAPI, systemd).
> Veri kaynağı: 10 Kasım 2026 ~20:36–22:30 UTC tarihli canlı sunucu dışa aktarımları (`orders`, `positions`, `reflex_decisions`, `trade_lessons`, `brier_calibration`, `system_state`, `journalctl`, `logs/systemd.log`, Telegram çıktıları) ve doğrudan **canlı Binance Spot & USD-M Futures API denetimi**.

---

## 0. Ajan için çalışma kuralları (ZORUNLU)

1. **Canlı servise dokunma.** `quant-bot.service` gerçek parayla çalışıyor. Hiçbir komutla servisi durdurma/başlatma, `.env`'e yazma, Binance'e emir gönderme.
2. **Gizli bilgi yazdırma.** `.env`, API anahtarı, Telegram token, dashboard şifresi, sunucu IP'si loglara, commit'e, test çıktısına veya dokümana girmeyecek (`<SERVER_IP>`, `<USER>` yer tutucularını kullan).
3. **Yeni branch'te çalış:** `git checkout -b fix/safety-and-calibration`. Her görev (`T0`, `T1`, `T2`, …) ayrı commit olsun. Commit mesajı: `fix(Tn): kısa açıklama`.
4. **Testlerde gerçek borsa çağrısı yok.** Tüm borsa etkileşimi mock/fake exchange ile test edilecek. `pytest -q` yeşil olmadan görevi bitmiş sayma.
5. **Davranış değişikliklerini config bayrağıyla ver** (varsayılan = güvenli taraf). Yeni ayarları `config/settings.py` (Pydantic v2) içine ekle ve `.env.example`'a belirt.
6. **Öncelik sırasına uy:** P0 → P1 → P2 → P3. P0 bitmeden P1'e geçme.
7. **Varsayılan işlem modu `paper` olsun** (`T0`). Canlıya geçiş kullanıcının elle yaptığı bir adımdır.
8. Her görev sonunda: değişen dosyalar, eklenen testler ve "nasıl doğrulanır" özetini yaz.
9. Emin olmadığın yerde tahmin etme; `TODO(user):` yorumu bırak ve sonda listele.

---

## 1. Sistemin özeti (bağlam)

```
Brain (1sa): engine/scanner.py -> top 20 USDT paritesi + makro rejim (BTC/ETH EMA200 & ADX14)
State (60sn): engine/state_engine.py -> 15m OHLCV (iloc[-2] kapanmış mum) + 10 kademe Orderbook
Reflex (60sn): engine/decision_reflex.py -> 15m kapanmış mumlar, Setup Quality (1-3) + güven (0-1)
Pre-trade analyzer: engine/pre_trade_analyzer.py -> orderbook satış duvarı + hata hafızası (mistake_learner.py)
Risk Gate: engine/risk_engine.py -> Quarter-Kelly / MinNotional + günlük işlem limiti + maruziyet kontrolü
Execution: engine/execution.py -> Futures isolated (serbest bakiye >= $3) ya da Spot (min $11.50) + Server-Side SL
Reconciler (30sn): engine/reconciler.py -> çift cüzdan bakiye, Algo SL/TP ve yazılımsal SL mutabakatı
Review (00:05 UTC): review/overnight_review.py -> Brier kalibrasyonu
```

Canlı ayarlar (özet): `TIMEFRAME=15m`, `MAX_OPEN_POSITIONS=5` *(hedef: 2–3)*, `MAX_POSITION_USD=25`, `SPOT_MIN_POSITION_USD=11.5`, `FUTURES_MARGIN_USD=4.5`, `FUTURES_DEFAULT_LEVERAGE=20` *(hedef: 3x ve `ENABLE_FUTURES=False` varsayılan)*, `DAILY_MAX_TRADES=20` *(hedef: 6)*, `DAILY_MAX_LOSS_USD=5`, `MAX_DRAWDOWN_PCT=15`, `MIN_CONFIDENCE_THRESHOLD=0.68`, `MIN_SETUP_QUALITY=2`, `ROUND_TRIP_FEE_PCT=0.20`.
Gerçek toplam bakiye (Spot + Futures): **`$30.23 – $30.57`**.

---

## 2. Bulgu özeti ve Doğrulanmış Kök Nedenler (Kod + Borsa Kanıtlarıyla)

| # | Bulgu | Önem | Kanıt & Doğrulanmış Kök Neden (Kaynak Kod Satırı + Canlı Borsa Durumu) |
|---|-------|------|------------------------------------------------------------------------|
| **F1** | `orders.csv`'de BNB giriş emri `open` ve `filled=0.130` ($87.74) görünüyor; SL emri ve `positions` kaydı yok | **KRİTİK** | **Borsa Doğrulaması:** Binance Futures üzerinde `id=98217405153` (`QB_BNBUSDT_1794339000000`, `19:55:54 UTC`) emrinin gerçek durumu **`status=canceled, amount=0.13, filled=0.0, avg=None`**. Gerçekte **0.0 BNB** dolmuştur; korumasız BNB pozisyonu yoktur.<br>**Kod Kök Nedeni (`engine/execution.py:275`):** `filled_qty = float(entry_order.get("filled") or entry_order.get("amount") or qty)` satırında Python `or` operatörü yüzünden `filled=0.0` (falsy) geldiğinde `entry_order.get("amount")` (`0.13`) değerine düşülmüş ve DB'ye `status=open, filled=0.13` yazılmıştır! Ardından SL adımında `sl_qty=0.0` hatası alınınca acil satış (`reduceOnly`) denenmiş, pozisyon olmadığı için `-2022 ReduceOnly` hatası alınmış ve açık limit emri borsada iptal edilmiş ama DB'deki `status=open` kaydı güncellenmemiştir. |
| **F2** | Bakiye / pozisyon / mod tutarsız (`/status`: Futures `$0.86 toplam, $0 işlemde`, mod `SPOT`) | **KRİTİK** | **Borsa Doğrulaması:** Binance USD-M Futures gerçek bakiyesi: `totalMarginBalance=$9.96`, `used (initialMargin)=$9.05` (`SOL $4.49` + `ZEC $4.56`), `free (availableBalance)=$0.86`. Spot gerçek bakiyesi: `free USDT=$8.14–$9.56` + `0.00015 BTC ($11.03)`.<br>**Kod Kök Nedeni (`engine/exchange.py:245-250` & `engine/telegram_bot.py:251-252`):** `fetch_dual_wallet_balances()` metodu `system_state` tablosuna `futures_free_usdt` (`0.86`) yazarken `futures_total_usdt` yazmıyordu. Servis restartından hemen sonra (ilk 30sn `reconcile_cycle` çalışmadan önce) `/status` çağrıldığında `futures_total_usdt` anahtarı bulunamadığı için `telegram_bot.py` varsayılan olarak `fut_free` (`$0.86`) değerini toplam bakiye sanıp `$0.00 İşlemde` yazdı. |
| **F3** | 20x futures: notional ≈ 6–7× bakiye; `MAX_POSITION_USD=25` aşılıyor | **KRİTİK** | **Kod Kök Nedeni (`engine/risk_engine.py:117-120`):** `compute_position_size_usd()` içinde Spot dalında `min(..., max_pos_usd)` uygulanırken, Futures dalında `target_usd = round(margin_usd * leverage, 2)` (`4.50 * 20 = $90.00`) hesaplanıp `max_pos_usd` (`$25.00`) üst sınırı **hiç kontrol edilmemiştir**! Ayrıca `SOL/USDT` ($89.82) + `ZEC/USDT` ($13.67) + `BTC/USDT` ($11.03) toplam notional maruziyeti `$114.52`'ye (~3.8x kasa) ulaşmıştır. |
| **F4** | Servis çok sık yeniden başlatılıyor; 9 Kasım 19:14'te açılışta 2 kez crash (`status=1/FAILURE`) | **YÜKSEK** | **Log Doğrulaması (`logs/systemd.log:8399-8412` & `30872-30885`):**<br>1) 9 Kasım `19:14:08` ve `19:14:15` crash'lerinin tam traceback'i: `File "engine/reconciler.py", line 4: from __future__ import annotations -> SyntaxError: from __future__ imports must occur at the beginning of the file`.<br>2) İkinci kapanış hatası (`systemd.log:30872`): `SIGTERM` geldiğinde `main.py:298` içinde `scheduler.shutdown(wait=False)` çağrılıp hemen ardından `db.close()` ve `exchange.close()` çalıştırıldığı için, o sırada devam eden `evaluate_watchlist_reflex_cycle` görevi kapanmış CCXT/DuckDB nesnesine erişirken exception fırlatmıştır. |
| **F5** | Brier skoru sabit/sahte (`0.1200`) | **YÜKSEK** | **Kod Kök Nedeni (`review/overnight_review.py:69`):** `brier_score = self.compute_brier_score(samples) if sample_count > 0 else 0.12` satırında `sample_count == 0` olduğunda sabit `0.12` atanmış ve `0.12 <= 0.25` olduğu için `"Well-calibrated"` raporlanmıştır. |
| **F6** | Güven eşiği tutarsız (`brier_calibration`: `%80` vs `system_state`: `%68`) | **YÜKSEK** | **Kod Kök Nedeni (`review/overnight_review.py:72-96` & `database/db_manager.py:183`):** 10 Kasım `00:05 UTC` gece denetimi çalıştığında `MIN_CONFIDENCE_THRESHOLD` `0.80` idi ve `brier_calibration` tablosuna `0.80` yazıldı. Gün içinde ayar `0.68`'e düşürülüp `system_state` güncellendi, ancak `/brier` komutu (`telegram_bot.py:345`) eşiği `system_state` yerine `brier_calibration` tablosundaki son satırdan okuduğu için `%80` göstermeye devam etti. |
| **F7** | Mistake Learner çöp veriyle (`"The user wants..."`) ceza uyguluyor; çıkış fiyatı yanlış (`0.001794`) | **YÜKSEK** | **Kod Kök Nedeni 1 (`engine/openrouter_pool.py:130`):** `raw_text = msg_obj.get("content") or msg_obj.get("reasoning")` satırında, ücretsiz reasoning modeli (`nemotron`/`lfm`) `max_tokens=140` içinde düşünme adımını bitiremeyip `content=""` döndüğünde, kod modelin İngilizce içsel düşünme (`reasoning`) alanını yanıt olarak almıştır!<br>**Kod Kök Nedeni 2 (`engine/mistake_learner.py:102` & `database/db_manager.py:303`):** `MistakeLearner` çıkış fiyatını `closed_pos.get("current_price")` üzerinden okumuş; `reconciler.py:90` ise SL kontrolünden hemen önce `update_position_price(pos_id, live_price)` ile `current_price` alanını o anki ticker fiyatına (`0.001794`) ezmiştir (gerçek SL tetik/limit fiyatı `0.001778 / 0.001773` yerine). |
| **F8** | Sinyal koşulları gevşek (ZEC Low 1024 vs BB_Lower 953; BTC/XAUT EMA21 farkı %0.05–0.09) | **ORTA** | **Kod Kök Nedeni (`engine/decision_reflex.py:131-140`):**<br>1) Satır 140: `if cond_rsi_oversold and (cond_bb_support or state.rsi_14 < 33.5):` koşulundaki `or state.rsi_14 < 33.5` ifadesi, `cond_bb_support` `False` olsa bile (ZEC banda %7 uzakken) dip sinyalini geçirmiş ve rationale metnine sabit `Low(...)~BB_Lower(...)` yazmıştır.<br>2) Satır 132: `cond_intraday_momentum` içinde `state.closed_close > state.ema_21` kontrolünde hiçbir minimum ATR/yüzde mesafesi, ADX veya hacim (`vol_sma_20`) şartı bulunmamaktaydı. |
| **F9** | Düşüş rejiminde (`HIGH_VOL`, BTC < EMA200) sadece long açılıyor | **ORTA** | **Kod Kök Nedeni (`engine/decision_reflex.py:115-169`):** `high_vol` ve `mean_reverting` rejimleri aynı `elif state.regime in ("mean_reverting", "high_vol"):` bloğunda birleştirilmiş; BTC `EMA200` altındayken `cond_intraday_momentum` long girişlerini kısıtlayan hiçbir makro filtre uygulanmamıştır. |
| **F10** | Sinyal sonuçları ölçülmüyor (`reflex_decisions`: 19 sinyal, `outcome` sadece 1 satırda dolu) | **ORTA** | **Kod Kök Nedeni (`database/db_manager.py:320`):** `reflex_decisions.outcome` yalnızca `execute_approved_entry` ile açılıp daha sonra `close_position` ile kapanan işlemler için güncellenmekte; açılmayan veya reddedilen sinyallerin sonraki mumlarda TP/SL olup olmadığını takip eden bir `OutcomeResolver` bulunmamaktadır. |
| **F11** | Stop-limit kayması planı aşıyor (ONE: planlanan %1.98, gerçekleşen ~%2.5) | **ORTA** | **Kod Kök Nedeni (`engine/execution.py:305, 342`):** Vadeli işlemlerde `order_type="STOP"` (limitli stop) ve `limit_sl_price = invalidation_price * 0.997` (%0.30 düşük limit) kullanıldığı için, hızlı düşüşte emir `0.001773` limit fiyatından eşleşmiş ve kaldıraç + taker komisyonu ile kayıp büyümüştür. |
| **F12** | ZEC SL miktarı (`0.012`) alınan miktardan (`0.013`) az (~$1 korumasız toz) | **DÜŞÜK** | **Kod Kök Nedeni (`engine/execution.py:315`):** `ZEC/USDT:USDT` açıldığı sırada Futures için de Spot komisyon kesintisi varsayımı (`filled_qty * 0.999 = 0.012987`) çalışmış ve `ROUND_DOWN` (`0.001` step) ile `0.012`'ye yuvarlanmıştır. Borsa üzerinde `0.001 ZEC` (~$1.05) açıkta kalmıştır. |
| **F13** | Saat kayması `512 ms` görünüyor | **ORTA** | **Borsa & OS Doğrulaması:** Sunucuda `chronyc tracking` ile ölçülen gerçek NTP sapması **`0.00000137 sn` (`0.0013 ms` = 1.3 mikrosaniye)**'dir.<br>**Kod Kök Nedeni (`engine/exchange.py:136-143`):** `verify_clock_drift()` servis açılışında ilk HTTP çağrısı olarak çalıştığında, soğuk DNS + TCP + TLS el sıkışması (~750ms) `local_before` sonrasında gerçekleştiği için `(local_before + local_after) // 2` orta nokta hesabı asimetrik bağlantı gecikmesi yüzünden ~400–500ms sapmaktadır. |
| **F14** | Bellek sıkışıklığı (150–250 MB swap peak) | **ORTA** | **Sunucu Doğrulaması:** GCP VM `e2-micro` (`951 MB` RAM, `2047 MB` swap) üzerinde çalışıyor (`free -m`: used=633MB, available=317MB, swap_used=172MB). `database/db_manager.py` içinde DuckDB için `PRAGMA memory_limit` sınırlandırılmamıştır. |
| **F15** | Dashboard HTTP + Basic Auth ile internete açık & loglarda Telegram token sızıntısı | **YÜKSEK** | **Kod Kök Nedeni (`config/settings.py:66` & `main.py:38`):** `DASHBOARD_HOST="0.0.0.0"` varsayılanı ve kök logger'da `httpx` seviyesinin `INFO` olması nedeniyle her 5 saniyede bir `GET https://api.telegram.org/bot<TOKEN>/getUpdates` tam URL'si `quant_bot.log` ve `systemd.log` dosyalarına düz metin olarak yazılmaktadır! |
| **F16** | README canlı sistemle uyumsuz & kılavuzlarda gerçek IP/kullanıcı adı var | **DÜŞÜK** | `README.md` ve `QUANT_BOT_SPEC.md` v3.0 (Spot-only, $50M hacim) döneminden kalmıştır; `KURULUM_VE_TASIMA_REHBERI.md` ve `rapor/kurulum_ve_mimari_rehberi.md` içinde gerçek IP/kullanıcı bilgileri yer tutucuya çevrilmelidir. |

---

## 3. Görevler

### T0 — Paper/Live mod bayrağı (önce bunu yap) — P0

**Amaç:** Diğer tüm değişiklikler canlı paraya dokunmadan test edilebilsin.

**Yapılacaklar**
- `config/settings.py`: `TRADING_MODE: Literal["paper", "live"] = Field(default="paper")`.
- `engine/execution.py`: `paper` modunda hiçbir gerçek emir (`create_order`, `create_futures_order`, `cancel_order`, `create_market_sell_order`) gitmesin. Emirleri aynı DB tablolarına (`orders`, `positions`) `paper=True` (veya `PAPER_` client_order_id öneki / `market_type='paper'`) işaretiyle yaz; doluşları son kapanmış mum fiyatı + komisyon (`ROUND_TRIP_FEE_PCT / 2`) + kayma (`SLIPPAGE_PCT`) varsayımıyla simüle et.
- Telegram ve dashboard mesajlarında modu göster: `[PAPER]` / `[LIVE]`.
- `main.py` başlarken `TRADING_MODE=live` ise log'a büyük uyarı yaz (anahtarı yazdırma).

**Kabul kriterleri**
- `TRADING_MODE=paper` iken ağ çağrıları sadece veri okuma (`fetch_tickers`/`fetch_ohlcv`/`fetch_order_book`) yapıyor; `create_order` / `create_futures_order` hiç çağrılmıyor (testle kanıtla: fake exchange'te çağrı sayacı 0).
- Varsayılan `.env.example` ve `config/settings.py` değeri `paper`.

---

### T1 — "Sıfır korumasız pozisyon" garantisini gerçekten sağla (F1, F12) — P0

**Problem:** Kısmen dolmuş veya açık kalmış giriş emri SL almadan açık kalabiliyor; `filled=0.0` iken `or` hatasıyla DB'ye `filled=amount` yazılmış; ZEC'te SL miktarı (`0.012`) alınan miktardan (`0.013`) küçük.

**Doğrulanmış Kod Yanıtları (`engine/execution.py`, `engine/reconciler.py`, `engine/exchange.py`):**
- *Giriş limit emri `open`/`partially_filled` iken akış nerede bekliyor? Timeout var mı?*
  - Mevcut kodda (`execution.py:203-251`) `create_order` çağrısı biter bitmez **hiç beklemeden** satır 275'e geçiyordu (`filled_qty = float(entry_order.get("filled") or entry_order.get("amount") or qty)`). Timeout veya `fetch_order` ile dolum doğrulaması yoktu.
- *SL yalnızca `status == closed` olunca mı kuruluyor?*
  - Hayır, emir `open` (`filled=0.0`) dönse bile `0.0 or amount` yüzünden emir dolmuş sanılıp hemen SL kurulmaya çalışılıyordu.
- *Servis yeniden başlayınca yarım kalan akış kurtarılıyor mu?*
  - Hayır, `main.py:initial_bootstrap()` içinde yalnızca `reconciler.reconcile_cycle()` çağrılıyor; `reconcile_cycle()` ise sadece DB'deki `OPEN` pozisyonları tarıyor, borsada açık kalmış sahipsiz emirleri veya DB'de kaydı olmayan borsa pozisyonlarını kontrol etmiyordu.

**Yapılacaklar**
1. **Gerçek `filled` okuma ve Kısmi Dolum / TTL Yönetimi (`engine/execution.py`):**
   - `0.0 or amount` hatasını kaldır: `raw_filled = entry_order.get("filled")`; gerçek borsada `filled_qty = float(raw_filled if raw_filled is not None else 0.0)`.
   - Giriş emri `open` veya `partially_filled` ise `ENTRY_ORDER_TTL_SEC` (varsayılan `90` sn, testlerde kısa ayarlanabilir) boyunca periyodik kontrol et; `partially_filled` (`filled > 0`) olduğu anda mevcut `filled` miktar için hemen koruyucu SL kur.
   - `ENTRY_ORDER_TTL_SEC` sonunda emir hâlâ tam dolmadıysa kalan açık kısmı iptal et (`cancel_order`); **iptal ile dolum yarışını (race condition) önlemek için** iptal sonrasında mutlaka `fetch_order` / `fetch_futures_order` ile nihai `filled` miktarını tekrar oku ve SL miktarını nihai `filled` miktarına güncelle. Eğer nihai `filled == 0` ise `orders` tablosunu `status='canceled', filled=0.0` olarak güncelle ve pozisyon açmadan çık.
2. **Pozisyon kaydı (`database/db_manager.py` & `engine/execution.py`):**
   - `filled > 0` olan her giriş için, SL kurulumundan **önce** `positions` tablosuna `status='OPEN_UNPROTECTED'` olarak kayıt oluştur; SL başarıyla kurulunca `status='OPEN'` ve `sl_exchange_order_id` güncelle.
3. **SL kurulamazsa:**
   - 3 deneme sonunda SL kurulamazsa hemen market emriyle pozisyonu kapat (`emergency_flatten`); eğer market kapatma da başarısız olursa `system_halted=true`, `status='OPEN_UNPROTECTED'` bırak, Telegram'a **acil** alarm gönder ve `reconciler` her 30 sn'de kapatmayı/SL kurmayı tekrar denesin.
4. **Toz (dust) yönetimi (`DUST_MAX_USD`):**
   - Spot'ta komisyon base varlıktan kesiliyorsa (`BNB` indirimi yoksa) net miktar `min(filled_qty, free_base)` olarak borsadan okunur; Futures'ta ise komisyon USDT teminattan kesildiği için `sl_qty = futures_amount_to_precision(symbol, filled_qty)` ile **tam kontrat miktarı** (`0.999` çarpanı olmadan!) kullanılır. Böylece ZEC'teki `0.013` vs `0.012` farkı tamamen ortadan kalkar.
   - `(filled_qty - sl_qty) * entry_price > DUST_MAX_USD` (varsayılan `$0.50`) ise uyarı logla.
5. **Açılış kurtarma (`startup_recovery` — `main.py` & `engine/reconciler.py`):**
   - `main.py` başlarken, zamanlayıcıları (`scheduler.start()`) başlatmadan önce `reconciler.run_startup_recovery()` çalıştır:
     - Borsadaki tüm açık Spot/Futures emirlerini ve pozisyonlarını çek.
     - DB ile eşleştir: DB'de `OPEN_UNPROTECTED` olan veya borsada açık olup aktif SL emri bulunmayan pozisyonlara hemen SL kur (veya market kapat) ve Telegram'a bildir.
     - DB'de `status='open'` kalmış ama borsada `canceled`/`closed` olan emirleri (`QB_BNBUSDT_...` gibi) senkronize et.
6. **Reconciler 30 sn Invariant Kontrolü (`engine/reconciler.py`):**
   - Her 30 sn'de bir açık pozisyonların `sl_exchange_order_id` emrinin borsada gerçekten `open` olduğunu doğrula; SL emri yoksa/iptal edilmişse ve pozisyon hâlâ açıksa derhal yeniden SL kur veya market kapat + alarm üret.

**Testler (`tests/test_unprotected_exposure.py`)**
- Kısmi dolum → SL `filled` miktarıyla kurulur.
- Kısmi dolum + TTL → kalan iptal edilir, SL miktarı son `filled` ile eşleşir.
- İptal anında ek dolum gelirse (race) SL miktarı güncellenir.
- Restart sonrası SL'siz pozisyon (`OPEN_UNPROTECTED` veya borsada SL'siz) bulunur ve korunur.
- Dust: Futures'ta `0.013` tam miktar SL alır (`0.999` kesintisi yapılmaz); Spot'ta `free_base` ve `stepSize` uyumu doğrulanır.

**Kabul kriteri:** Hiçbir senaryoda `filled > 0` giriş, 30 sn'den uzun süre aktif SL'siz kalmıyor.

---

### T2 — Tek doğruluk kaynağı: borsa ↔ DB ↔ rapor mutabakatı (F2) — P0

**Problem:** `/status` Futures'ı `"$0.86 toplam, $0 işlemde"` gösterirken DB'de `$90`'lık SOL ve `$13.4`'lık ZEC var; `positions` tablosunda pozisyonun Spot mu Futures mı olduğu, kaldıracı ve teminatı (`margin_usd`) tutulmuyor.

**Doğrulanmış Kod Yanıtları (`reconciler.py`, `exchange.py`, `telegram_bot.py`, `db_manager.py`):**
- `positions` şemasında (`db_manager.py:124-146`) `market_type`, `leverage`, `margin_usd`, `notional_usd` sütunları yoktu; sadece `cost_usd` vardı.
- `exchange.py:245-250` içinde `fetch_dual_wallet_balances()` `futures_total_usdt` anahtarını `system_state`'e kaydetmiyordu.

**Yapılacaklar**
1. **Şema ve Migration (`database/db_manager.py`):**
   - `positions` tablosuna `market_type VARCHAR DEFAULT 'spot'`, `leverage INTEGER DEFAULT 1`, `margin_usd DOUBLE DEFAULT 0.0`, `notional_usd DOUBLE DEFAULT 0.0`, `sl_slippage_pct DOUBLE DEFAULT 0.0`, `paper BOOLEAN DEFAULT FALSE` sütunlarını idempotent `ALTER TABLE positions ADD COLUMN IF NOT EXISTS ...` ile ekle.
   - Mevcut kayıtlar için migration: `sl_exchange_order_id LIKE '2000%'` olanları `market_type='futures'` olarak işaretle.
2. **Reconciler Bakiye ve Equity Hesabı (`engine/reconciler.py`):**
   - Spot: `spot_equity = spot_free_usdt + sum(spot_open_pos_qty * live_price)`.
   - Futures: `futures_wallet_balance = totalMarginBalance` (`totalWalletBalance + totalUnrealizedProfit`).
   - `total_equity_usd = spot_equity + futures_wallet_balance` (Futures cüzdan toplamı zaten izole teminat ve gerçekleşmemiş PnL'i içerdiği için Futures `notional_usd` **asla** eklenmez).
3. **Tutarlılık Kontrolü (`RECONCILE_MISMATCH`):**
   - DB'de `status='OPEN'` ve `market_type='futures'` olan her pozisyonun borsada (`fetch_active_futures_positions`) aynı sembol ve uyumlu miktarda (`abs(db_qty - ex_qty) * price <= DUST_MAX_USD`) karşılığı olmalı (ve borsada `notional >= $1.0` olan her Futures pozisyonunun DB'de karşılığı olmalı).
   - Uyuşmazlık varsa `RECONCILE_MISMATCH` alarmı (Telegram + log) üret, `system_state`'te `entries_blocked='true'` ve `reconcile_mismatch_count` artır; yeni girişleri durdur.
4. **`/status` ve `/pozisyonlar` Güncellemesi (`engine/telegram_bot.py`):**
   - Her pozisyon için `market_type` (`SPOT` / `FUTURES`), `leverage`, `margin_usd`, `notional_usd`, `invalidation_price` (SL), SL emir durumu ve pozisyon yaşını göster.
5. `system_state` tablosunda `reconcile_last_ok_ms`, `reconcile_mismatch_count`, `entries_blocked` anahtarlarını tut.

**Testler:** Fake exchange ile (a) DB'de var borsada yok, (b) borsada var DB'de yok, (c) miktar farkı senaryoları; her biri alarm + `entries_blocked=true` üretmeli.

**Kabul kriteri:** `/status` toplamı, borsa API'sinden bağımsız hesaplanan toplamla `±$0.05` içinde.

---

### T3 — Kaldıraç ve toplam maruziyet sınırları (F3, F9-kısmen) — P0

**Problem:** `$30` bakiyede 20x futures ile toplam notional `~$114–$200`; `MAX_POSITION_USD` futures'ta uygulanmıyor; futures teminatı bitince `SPOT_MIN_POSITION_USD=$11.50` fallback'i botu sürekli yeni işlem açmaya zorluyor.

**Yapılacaklar**
1. **Yeni Ayarlar (`config/settings.py` & `.env.example`, varsayılan = güvenli):**
   - `ENABLE_FUTURES: bool = Field(default=False)`
   - `MAX_LEVERAGE: int = Field(default=3, ge=1, le=20)`
   - `MAX_GROSS_EXPOSURE_X: float = Field(default=1.0, gt=0.0)` (toplam açık notional / `total_equity_usd` üst sınırı)
   - `MAX_RISK_PER_TRADE_PCT: float = Field(default=1.0, gt=0.0)` (SL'ye kadar kayıp + komisyon + kayma / `total_equity_usd`)
   - `MAX_TOTAL_OPEN_RISK_PCT: float = Field(default=3.0, gt=0.0)` (tüm açık pozisyonların SL riski toplamı / `total_equity_usd`)
   - `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED: bool = Field(default=False)`
   - `MAX_CORRELATED_LONGS: int = Field(default=2, ge=1)`
   - `DAILY_MAX_TRADES: int = Field(default=6, ge=1)`
   - `DAILY_MAX_LOSS_PCT: float = Field(default=3.0, gt=0.0)` (`0.03 * peak_equity`)
   - `MAX_OPEN_POSITIONS: int = Field(default=2, ge=1)`
   - `SLIPPAGE_PCT: float = Field(default=0.05, ge=0.0)`
2. **Risk Kapısı (`engine/risk_engine.py`):**
   - `MAX_POSITION_USD` hem Spot hem Futures için **notional** üzerinden uygulanacak.
   - Her giriş öncesi `risk_usd = notional * (abs(entry - sl) / entry + (ROUND_TRIP_FEE_PCT + SLIPPAGE_PCT) / 100.0)` hesapla.
   - Eğer `risk_usd > (MAX_RISK_PER_TRADE_PCT / 100.0) * total_equity`: pozisyon boyutunu (`notional`) `max_allowed_risk_usd` sınırına küçültmeyi dene; küçültülmüş boyut `min_notional * 1.10` altına düşüyorsa **işlemi reddet**.
   - Açık pozisyonların toplam SL riski + yeni işlem riski `MAX_TOTAL_OPEN_RISK_PCT` sınırını aşıyorsa **reddet**.
   - Açık pozisyonların toplam notional değeri + yeni işlem notional değeri `MAX_GROSS_EXPOSURE_X * total_equity` sınırını aşıyorsa **reddet**.
   - Korelasyon sınırı: açık long USDT pozisyon sayısı `>= MAX_CORRELATED_LONGS` ise **reddet**.
3. **Spot Fallback Kontrolü:**
   - `ENABLE_FUTURES=True` iken Futures serbest bakiyesi yetersizse ve `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED=False` ise Spot'a düşme, işlemi reddet (`"Futures margin exhausted and Spot fallback disabled"`).

**Testler:** `$30` equity, SOL SL `%1.46`, notional `$90` → `risk_usd = $1.32 + fee > %1 * $30 ($0.30)` → reddedilmeli. Spot min-notional `$11.50`, SL `%0.8` → kabul (veya risk sınırına göre değerlendirilir). Toplam açık risk `%3`'ü aşarsa reddet.

**Kabul kriteri:** Varsayılan config ile hiçbir koşulda `notional / equity > 1.0` ve tek işlem riski `> %1` olmuyor.

---

### T4 — Güvenli yeniden başlatma ve dağıtım (F4) — P0/P1

**Problem:** ~18 restart/2 gün; 9 Kasım `19:14`'te 2 kez `status=1/FAILURE`; kapanışta `scheduler.shutdown(wait=False)` yüzünden aktif görevlerin kapanmış DB/CCXT'ye çarpması.

**Doğrulanmış Kök Neden (`logs/systemd.log:8399-8412` & `30872-30885`):**
- 9 Kasım `19:14:08` ve `19:14:15` crash'i: `engine/reconciler.py` satır 4'te `from __future__ import annotations` ifadesinin dosya başından aşağı kayması (`SyntaxError`).
- 10 Kasım `18:12:38` kapanış traceback'i: `main.py:298` içinde `self.scheduler.shutdown(wait=False)` sonrası devam eden `evaluate_watchlist_reflex_cycle` coroutine'inin kapanmış `self.exchange` / `self.db` bağlantısını kullanmaya çalışması.

**Yapılacaklar**
1. **Ast/Compile Regresyon Testi:** Tüm `.py` dosyalarının `py_compile` / `ast.parse` ile hatasız derlendiğini ve `from __future__ import annotations` kuralını doğrulayan birim testi ekle.
2. **Graceful Shutdown (`main.py`):**
   - `_shutting_down: bool = False` bayrağı ve `_active_cycle_lock = asyncio.Lock()` ekle.
   - `SIGTERM`/`SIGINT` geldiğinde `_shutting_down = True` yap (yeni girişleri derhal durdur), devam eden `execution` / `reflex` / `reconcile` adımının bitmesini `asyncio.wait_for` ile güvenli şekilde bekle, ardından `scheduler.shutdown(wait=False)`, `telegram.stop()`, `exchange.close()`, `db.close()` sırasıyla kapat.
   - `deploy/quant-bot.service.example` içinde `TimeoutStopSec=90` tanımla.
3. **Dağıtım Betiği (`scripts/deploy.sh`):**
   - (a) `pytest -q` çalıştır,
   - (b) `preflight_check.py` çalıştır,
   - (c) DB yedeği al (`data/quant_bot.db.bak`),
   - (d) Açık/bekleyen `OPEN_UNPROTECTED` pozisyon varsa uyar ve çık; yoksa kullanıcı onay 파라미터sı ile servisi yeniden başlat.
4. **`preflight_check.py` Genişletmesi:** NTP kayması (`CLOCK_WARN_MS` / `CLOCK_BLOCK_MS`), RAM/swap kontrolü, DB kilidi kontrolü, `.env` zorunlu alanları ve `TRADING_MODE` kontrolü.
5. **Sürüm Bilgisi:** `git describe --always` (veya commit hash) bilgisini başlangıç loguna ve `/status` çıktısına ekle.

**Kabul kriteri:** `SIGTERM` testinde süreç aktif döngüyü kırmadan temiz kapanıyor, DB bozulmuyor, yeniden açılışta `startup_recovery` (T1.5) çalışıyor.

---

### T5 — Gerçek Brier kalibrasyonu (F5, F6) — P1

**Problem:** `sample_count == 0` iken sabit `0.1200` Brier skoru yazılması ve `/brier` ile `/status` arasında `min_confidence_threshold` (`0.80` vs `0.68`) uyuşmazlığı.

**Yapılacaklar**
1. `config/settings.py`: `MIN_BRIER_SAMPLES: int = Field(default=30, ge=5)`.
2. `review/overnight_review.py`:
   - `sample_count < MIN_BRIER_SAMPLES` ise `brier_score = None` (`NULL`), durum `INSUFFICIENT_DATA`, **hiçbir parametre değiştirilmesin**. Rapor metni `"Yetersiz örnek (n=X < 30)"` desin. Sabit `0.12` değerini koddan tamamen kaldır.
   - Brier hesabını `signal_outcomes` (T7) ve kapanan pozisyonlarla ortaklaştır (`mean((p_i - y_i)^2)`).
   - Reliability tablosu (güven kovaları `0.60–0.70`, `0.70–0.80`, `0.80+` için örnek sayısı ve gerçekleşen isabet oranı), win rate, ortalama net R ve net PnL raporla.
3. **Tek Doğruluk Kaynağı (`system_state.min_confidence_threshold`):**
   - `/status` ve `/brier` komutlarının her ikisi de aktif eşiği `system_state.min_confidence_threshold` üzerinden okusun.
   - Parametre değiştiğinde `calibration_log` tablosuna (`id`, `review_date`, `old_min_conf`, `new_min_conf`, `old_max_pos`, `new_max_pos`, `brier_score`, `sample_count`, `reason`, `created_at_ms`) kaydet.
   - Otomatik ayar sınırları: eşik adımı `±0.02`, mutlak aralık `[0.60, 0.85]`, `MAX_POSITION_USD` otomatik artırılamaz (yalnızca azaltılabilir).

---

### T6 — Mistake Learner'ı doğrula ve sağlamlaştır (F7) — P1

**Problem:** `openrouter_pool.py:130` içindeki `or msg_obj.get("reasoning")` yüzünden modelin içsel İngilizce düşünme metni (`"The user wants..."`) ders olarak kaydedilmiş; `current_price` yüzünden çıkış fiyatı yanlış yazılmış; tek işlemden `-0.06` ceza uygulanmış.

**Yapılacaklar**
1. `engine/openrouter_pool.py`: `msg_obj.get("reasoning")` fallback'ini **kaldır**; yalnızca `msg_obj.get("content")` kullan.
2. `engine/mistake_learner.py`:
   - LLM yanıtını doğrula: tek cümle (`\n` yok), `15 <= len <= 200`, Türkçe karakter içeriyor, `"the user"`, `"analysis:"`, `"trade details"`, `"let's"` gibi kalıplar içermiyor. Geçmezse deterministik şablon kullan: `"{rejim} rejiminde RSI {rsi:.1f}, OB dengesizliği {ob:+.2f} ile girilen long SL ({exit_price:.4f}) ile kapandı."`
   - Çıkış fiyatını `closed_pos.get("exit_price") or closed_pos.get("invalidation_price")` üzerinden al (ticker ile ezilen `current_price`'tan değil).
3. **Ceza Politikası (`MIN_LESSONS_FOR_PENALTY`):**
   - `config/settings.py`: `MIN_LESSONS_FOR_PENALTY: int = Field(default=5, ge=1)`, `LESSON_TTL_DAYS: int = Field(default=7, ge=1)`.
   - Güven cezası yalnızca aynı `pattern_key` (`f"{regime}|rsi_{int(entry_rsi // 10)*10}|ob_{'neg' if ob < 0 else 'pos'}"`) için son `LESSON_TTL_DAYS` günde `validated=True` olan en az `MIN_LESSONS_FOR_PENALTY` (5) kayıt biriktiğinde devreye girsin; maksimum toplam ceza `-0.10`. Tek işlem hatası ceza üretmesin.
4. `trade_lessons` şemasına `pattern_key VARCHAR`, `validated BOOLEAN DEFAULT TRUE`, `source VARCHAR DEFAULT 'template'` ekle; mevcut bozuk kaydı (`failure_reason LIKE '%The user wants%'`) migration ile `validated=FALSE, confidence_penalty=0.0` yap.

---

### T7 — Tüm sinyaller için sonuç takibi ve geriye dönük değerlendirme (F10) — P1

**Yapılacaklar**
1. `database/db_manager.py`: `signal_outcomes` tablosunu oluştur (`decision_id PRIMARY KEY`, `symbol`, `regime`, `setup_quality`, `direction_confidence`, `entry`, `sl`, `tp`, `horizon_bars`, `result` (`TP`/`SL`/`TIMEOUT`), `r_multiple_gross`, `r_multiple_net`, `bars_to_exit`, `resolved_at_ms`).
2. `engine/outcome_resolver.py`:
   - Çözümlenmemiş `reflex_decisions` (`direction='long'`) kayıtlarını sonraki 15m mumlarla değerlendir.
   - Aynı mumda hem `low <= sl` hem `high >= tp` görülürse **muhafazakâr olarak `SL` önce** kabul et.
   - `SIGNAL_HORIZON_BARS` (varsayılan `32` bar = 8 saat) sonunda TP veya SL görülmediyse `TIMEOUT` olarak son kapanış fiyatından gross/net R (`ROUND_TRIP_FEE_PCT` + `SLIPPAGE_PCT` düşülmüş) hesapla ve `reflex_decisions.outcome` (`1` net R > 0 ise, `0` değilse) alanını güncelle.
3. CLI Rapor Aracı (`tools/signal_report.py`) ve Backtest Aracı (`tools/backtest_signals.py`) ekle.

---

### T8 — Sinyal koşullarını sıkılaştır ve rejim filtresi ekle (F8, F9) — P1

**Yapılacaklar**
1. **Bollinger Dip Koşulu (`engine/decision_reflex.py`):**
   - `BB_TOUCH_TOL: float = Field(default=0.002)` (veya `0.25 * atr_14`).
   - `cond_bb_support = state.closed_low <= state.bb_lower * (1.0 + bb_tol) or state.closed_low <= state.bb_lower + 0.25 * state.atr_14`.
   - `or state.rsi_14 < 33.5` bypass'ını **kaldır**; dip sinyali için `cond_bb_support` zorunlu olsun!
2. **Momentum Bounce Koşulu:**
   - `close - ema_21 >= MOMENTUM_MIN_ATR_FRAC * atr_14` (`MOMENTUM_MIN_ATR_FRAC = 0.30`), `rsi_14 > prev_rsi_14`, `adx_14 >= ADX_MIN` (`20.0`) ve `closed_volume >= 1.10 * vol_sma_20` şartlarını zorunlu kıl.
3. **Makro ve Rejim Filtreleri:**
   - `BLOCK_LONGS_BELOW_BTC_EMA200: bool = Field(default=True)`: BTC `EMA200` altındayken (`macro_btc_below_ema200=True`) momentum bounce girişlerini engelle; mean-reversion girişlerine yalnızca `rsi_14 < 30.0` ve `orderbook_imbalance > 0.0` ise izin ver.
   - `HIGH_VOL` rejiminde `MIN_SETUP_QUALITY_HIGH_VOL: int = Field(default=3)`.
   - Düşen bıçak filtresi (`FALLING_KNIFE_24H_PCT: float = Field(default=-10.0)`): 24s değişimi `< -10%` olan sembollerde `setup_quality < 3` ise giriş engelle.
4. **Sembol Evreni (`engine/scanner.py`):**
   - `RWUSD`, `XAUT`, `PAXG`, `WBTC`, `STETH` ve sentetik varlıkları hariç tut; `MAX_ABS_24H_CHANGE_PCT: float = Field(default=40.0)` üstündeki aşırı oynak pariteleri filtrele.

---

### T9 — SL gerçekleşme kalitesi (F11) — P2

**Yapılacaklar**
1. Spot için `SL_LIMIT_BUFFER_PCT: float = Field(default=0.20)` ayarlanabilir yap; tetiklenmiş ama `SL_ESCALATION_SEC` (varsayılan `15` sn) içinde dolmamış (`live_price < limit_sl_price` ve emir `open`) Spot SL emirlerini iptal edip **market ile kapat**.
2. Futures'ta `STOP_MARKET` (`workingType="MARK_PRICE"`, `reduceOnly=True`) kullan.
3. `positions.sl_slippage_pct` alanına gerçekleşen kaymayı kaydet.

---

### T10 — Zaman bazlı çıkış ve açık pozisyon yaşı — P2

**Yapılacaklar**
- `MAX_HOLD_BARS: int = Field(default=32)` (8 saat). Süresi dolan açık pozisyonu `reconciler` içinde `TIME_EXIT_MAX_HOLD` nedeniyle market kapat.
- `BREAKEVEN_TRIGGER_R: float = Field(default=1.0)`: Kâr `1.0R`'ye ulaştığında yazılımsal/sunucu SL seviyesini `entry_price * (1 + ROUND_TRIP_FEE_PCT / 100.0)` seviyesine çek.
- `/pozisyonlar` çıktısına pozisyon yaşını (`dk`/`saat`) ekle.

---

### T11 — Saat senkronu koruması (F13) — P2

**Yapılacaklar**
- `verify_clock_drift()` içinde soğuk TLS el sıkışmasını elemek için ilk ölçüm `RTT > 120ms` ise sıcak bağlantıyla ikinci ölçümü al.
- `abs(drift_ms) > CLOCK_WARN_MS` (`250`) → Telegram uyarısı; `abs(drift_ms) > CLOCK_BLOCK_MS` (`800`) → yeni girişleri blokla ve `load_time_difference()` çalıştır.
- CCXT `adjustForTimeDifference=True` ve `RECV_WINDOW_MS: int = Field(default=5000)` ayarlanabilir yap.

---

### T12 — Bellek ve kaynak kullanımı (F14) — P2

**Yapılacaklar**
- `database/db_manager.py`: Bağlantı açılışında `PRAGMA memory_limit='256MB'` ve `PRAGMA threads=2` uygula.
- `deploy/quant-bot.service.example` dosyasında `MemoryHigh=380M`, `MemoryMax=450M`, `TimeoutStopSec=90` tanımla.
- `/status` çıktısına bellek (RSS MB) bilgisini ekle.

---

### T13 — Dashboard ve gizli bilgi güvenliği (F15) — P1

**Yapılacaklar**
- `DASHBOARD_HOST` varsayılanını `"127.0.0.1"` yap.
- Zayıf şifre (`change_me_strong_password` veya `< 12` karakter) ile dış ağda (`0.0.0.0`) çalışmayı reddet; IP başına dakikada 5 başarısız giriş hız sınırı (rate limit) ekle.
- **Logger Gizli Bilgi Maskeleme Filtresi (`SecretRedactionFilter`):** Log mesajlarındaki Telegram bot token'larını (`bot\d+:[A-Za-z0-9_-]+` -> `bot<REDACTED>`), API anahtarlarını ve `httpx` URL sızıntılarını otomatik maskele (`httpx` log seviyesini `WARNING` yap + `SecretRedactionFilter` ekle).
- `.gitignore` içine `.env`, `*.db`, `*.duckdb`, `logs/`, `rapor/*.log`, `rapor/*.csv` ekle.

---

### T14 — Dokümantasyon eşitleme (F16) — P3

**Yapılacaklar**
- `README.md`, `QUANT_BOT_SPEC.md`, `KURULUM_VE_TASIMA_REHBERI.md` ve `rapor/kurulum_ve_mimari_rehberi.md` belgelerini v3.1 güvenlik mimarisiyle eşitle; tüm gerçek IP ve kullanıcı adlarını `<SERVER_IP>` ve `<USER>` yer tutucularına dönüştür; `CHANGELOG.md` ekle.

---

## 4. Önerilen güvenli varsayılan ayarlar (`.env.example`)

| Ayar | Önerilen | Not |
|------|----------|-----|
| `TRADING_MODE` | `paper` | Canlıya geçiş elle |
| `ENABLE_FUTURES` | `false` | T2/T3 doğrulanana kadar |
| `MAX_LEVERAGE` | `3` | Futures açılırsa |
| `MAX_GROSS_EXPOSURE_X` | `1.0` | notional/equity |
| `MAX_RISK_PER_TRADE_PCT` | `1.0` | SL riski |
| `MAX_TOTAL_OPEN_RISK_PCT` | `3.0` | Tüm pozisyonlar |
| `MAX_OPEN_POSITIONS` | `2` | |
| `MAX_CORRELATED_LONGS` | `2` | |
| `DAILY_MAX_TRADES` | `6` | |
| `DAILY_MAX_LOSS_PCT` | `3.0` | equity yüzdesi |
| `MIN_CONFIDENCE_THRESHOLD` | `0.68` | Tek kaynak: `system_state` |
| `MIN_SETUP_QUALITY` | `2` (`HIGH_VOL`'da `3`) | |
| `ENTRY_ORDER_TTL_SEC` | `90` | |
| `SL_LIMIT_BUFFER_PCT` | `0.20` | |
| `MAX_HOLD_BARS` | `32` | |
| `MIN_BRIER_SAMPLES` | `30` | |
| `CLOCK_WARN_MS` / `CLOCK_BLOCK_MS` | `250` / `800` | |
| `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED` | `false` | |

---

## 5. Matematiksel arka plan (neden bu kadar sıkı?)

- Her sinyalde R:R sabit 2.5 ve SL mesafesi ~%0.8–2. Fiyat rastgele yürüyüşte ise TP'nin SL'den önce vurma olasılığı `1/(1+2.5) ≈ %28.6`.
- %0.2 round-trip komisyonla başa baş isabet oranı SL %1 için ≈ `%34.3`, SL %2 için ≈ `%31.4`.
- Yani sinyalin rastgeleden **yaklaşık 3–6 puan** iyi olması gerekir; T7 (`OutcomeResolver` + `signal_report`) bu değeri gerçek veriden ölçer.
- Spot minNotional ($11.5) yüzünden $30 hesapta tek pozisyon equity'nin ~%38'i olur; bu yüzden aynı anda 2'den fazla pozisyon aşırı risktir.

---

## 6. Doğrulama ve devreye alma planı

1. T0 → T14 tamamlanınca: `pytest -q` yeşil.
2. `TRADING_MODE=paper` ile **en az 7 gün** çalıştır; haftalık `signal_report` üret.
3. Canlıya geçiş kriterleri (hepsi sağlanmalı):
   - `signal_outcomes` n ≥ 100 ve net beklenen R > 0.
   - Reliability tablosunda yüksek güven kovaları gerçekten daha yüksek isabet gösteriyor.
   - 7 gün boyunca `RECONCILE_MISMATCH` = 0 ve "SL'siz pozisyon" alarmı = 0.
   - Startup recovery testi geçti.
4. İlk canlı dönemde: `ENABLE_FUTURES=false`, işlem başına riski equity'nin %0.5–1.0'i, `MAX_OPEN_POSITIONS=1–2`.
5. Her yeniden başlatma `scripts/deploy.sh` üzerinden yapılsın.

---

## 7. Açık Soruların Doğrulanmış Yanıtları (Canlı Borsa & Log Denetimi Sonuçları)

1. **Soru 1: SOL ve BNB pozisyonları Binance'te gerçekten futures'ta mı açık, yoksa DB kaydı mı bayat?**
   - **Doğrulanmış Yanıt:**
     - **`SOL/USDT:USDT`:** Binance USD-M Futures üzerinde **gerçekten AÇIK** (`contracts=1.08`, `notional=$89.82`, `entry=83.27`, `initialMargin=$4.49`, `isolated`). Borsada aktif Algo Stop-Loss emri (`id=2000001642567612`, `stopPrice=82.11`, `amt=1.08`, `status=open`) mevcuttur.
     - **`ZEC/USDT:USDT`:** Binance USD-M Futures üzerinde **gerçekten AÇIK** (`contracts=0.013`, `notional=$13.67`, `entry=1029.01`, `initialMargin=$4.56`, `isolated`). Borsada aktif Algo Stop-Loss emri (`id=2000001642463465`, `stopPrice=1011.38`, `amt=0.012`, `status=open`) mevcuttur.
     - **`BTC/USDT`:** Binance Spot üzerinde **gerçekten AÇIK** (`0.00015 BTC`, `$11.03`). Borsada aktif Spot Stop-Loss Limit emri (`id=68339261149`, `stopPrice=72949.68`, `amount=0.00015`) mevcuttur.
     - **`BNB/USDT`:** Binance üzerinde (ne Spot ne Futures'ta) **açık pozisyon veya emir YOKTUR**. `orders.csv`'deki kayıt bayattır.
     - **Bakiye Doğrulaması:** Futures `totalMarginBalance=$9.96` (`used=$9.05`, `free=$0.86`) + Spot `USDT=$8.14–$9.56` + Spot `BTC=$11.03` = **`~$30.23–$30.57`**.

2. **Soru 2: BNB kısmi dolumu şu an açık mı, SL'li mi?**
   - **Doğrulanmış Yanıt:** Hayır, BNB emri (`id=98217405153`, `QB_BNBUSDT_1794339000000`) Binance Futures üzerinde **`status=canceled, filled=0.0`** durumundadır (hiç dolmamıştır). `orders.csv`'de `filled=0.13` görünmesi `execution.py:275`'teki `0.0 or entry_order.get("amount")` hatasından kaynaklanmıştır. Korumasız BNB pozisyonu yoktur.

3. **Soru 3: 9 Kasım 19:14'teki açılış crash'inin traceback'i var mı?**
   - **Doğrulanmış Yanıt:** Evet, `logs/systemd.log` satır `8399–8412`'de doğrulandı:
     ```text
     File "/home/emrekayahan/btc/quant-bot-live/main.py", line 24, in <module>
       from engine.reconciler import StateReconciler
     File "/home/emrekayahan/btc/quant-bot-live/engine/reconciler.py", line 4
       from __future__ import annotations
     SyntaxError: from __future__ imports must occur at the beginning of the file
     ```
     Canlı düzenleme sırasında `from __future__ import annotations` satırının 1. satırdan 4. satıra kayması nedeniyle oluşmuş ve 19:14:20'de düzeltilmiştir.

4. **Soru 4: Futures yolu kullanılmaya devam edilecek mi?**
   - **Doğrulanmış Yanıt:** Kod içinde `ENABLE_FUTURES: bool = False` güvenli varsayılan olarak ayarlanmaktadır (`MAX_LEVERAGE = 3`). Operatör `.env` üzerinden açıkça `ENABLE_FUTURES=true` yapmadıkça devreye girmez.

5. **Soru 5: Geçmiş 15m OHLCV CSV'leri sağlanabilir mi?**
   - **Doğrulanmış Yanıt:** `tools/backtest_signals.py` hem yerel CSV dosyalarını (`--csv`) okuyacak hem de istenirse public Binance veri uç noktasından (`data-api.binance.vision`) geçmiş 15m mumlarını indirip CSV olarak kaydedebilecek (`--fetch-symbol BTC/USDT --days 30`) şekilde tasarlanmıştır.
