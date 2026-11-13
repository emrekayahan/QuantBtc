# QuantBot — V4 Planı: Kaldıraç-Öncelikli, Kendi Kendine Öğrenen "Broker"

> **Bu dosya Antigravity (AI kodlama ajanı) içindir.** Repo: `QuantBtc/quant-bot-live` (Python 3.12, async CCXT, DuckDB, FastAPI, systemd).
> Önceki planlar (`QUANTBOT_FIX_PLAN.md`, `FIX_PLAN_ROUND2.md`, `FIX_PLAN_ROUND3.md`) altyapıyı sağlamlaştırdı. **Bu plan onların üstüne "para kazanma" katmanını kurar.** Çakışmada bu dosya geçerlidir.
> Tasarım kararları operatör tarafından devredilmiştir: bu dosyadaki kararlar (D1–D8) bağlayıcıdır, ajan yeniden tartışmaz; uygulayamıyorsa `TODO(user)` yazar.

---

## 0. Ajan için çalışma kuralları (ZORUNLU)

1. **Sunucudaki canlı servise, `.env`'e, `data/*.db`'ye ve gerçek emirlere dokunma.** Testlerde gerçek borsa çağrısı yok (fake exchange). Gerçek borsa yalnızca operatörün çalıştıracağı ayrı betiklerle (`tools/…`, salt-okunur) konuşur.
2. **Gizli bilgi yok:** API anahtarı, token, şifre, IP, ev dizini yolları loga/commit'e/teste girmez. Loglarda URL'ler dahil maskeleme testi zorunlu (K0-1).
3. Branch: `feat/v4-broker`. Her iş paketi (`K0`, `K1`, …) ayrı commit: `feat(Kn): …` / `fix(Kn): …`.
4. **Önce test, sonra kod.** Kırmızı test göster → düzelt → yeşil.
5. **Her ayar kodda kullanılmalı** (ölü ayar yasak; `test_no_dead_settings` zaten var, bozma).
6. **Hiçbir parametre/strateji "kanıtsız" canlıya çıkmaz.** Kanıt kapıları (Bölüm 6) koda gömülü olacak; ajan kapıyı gevşetmez.
7. Aşamalı ilerle: her faz sonunda `pytest -q`, değişiklik özeti, `TODO(user)` listesi, onay bekle.
8. Emin olmadığın yerde varsayım yapma; `TODO(user):` bırak.

---

## 1. Hedef, gerçekçi çerçeve ve tez

**Hedef:** Sistem bir *broker gibi* davransın: kendi başına işlem açsın/kapatsın, kaldıraç çarpanına **kendisi** karar versin, hatalarından ve başarılarından öğrensin, operatör yalnızca bakiyeyi izlesin. Öncelikli amaç **net (komisyon+funding sonrası) pozitif getiri**.

### 1.1 Veri ne diyor (son 3 gün, 376 sonuçlanmış sinyal, canlı+resolver)
| Metrik | Değer | Anlamı |
|---|---|---|
| TP / SL / Timeout | 12 / 108 / 256 | Win rate (TP/(TP+SL)) **%10**; rastgele beklenen ≈ %28.6 |
| Ortalama R (brüt / net) | −0.075 / **−0.320** | Edge yok; maliyet ≈ **0.245 R** yiyor |
| Güven kovaları | 0.80+: %7.9 · 0.70–0.80: %11.0 | Güven skoru **ayırt edici değil** |
| Rejim | `mean_reverting` −0.39 R (N=293) · `high_vol` −0.07 R (N=83) | Düşen piyasada long "dip al" bıçak yakalıyor |
| Canlı (son 10 işlem) | Net ≈ −$0.45 | Eski 5 kayıt PnL'siz taşındı; tablo eksik |

**Sonuç:** Mevcut strateji (15m, long-only, spot ağırlıklı, 2×ATR stop, sabit 2.5R hedef) **bu rejimde kârsız**. Ek işlem, daha fazla kaldıraç veya daha yüksek hacim bunu **büyütür**. Para kazanmak için gereken: (1) maliyeti kes, (2) çıkış geometrisini düzelt, (3) iki yönde (long/short) trend ile çalış, (4) kaldıracı ve riski **kanıtlanmış edge'e** göre ayarla, (5) canlıda *sınırlı bütçeyle* deneyip öğren.

### 1.2 Kaldıraç hakkında (kodun çözmesi gereken doğru bilgi)
- Kaldıraç edge **üretmez**; edge varsa kazancı ve kaybı **büyütür**. Hızlı büyüme ancak (a) pozitif beklenen değer, (b) doğru pozisyon boyutu (riskin bakiye yüzdesi) ile gelir.
- Boyut **riske göre** hesaplanır (`notional = risk_usd / stop_mesafesi`). Kaldıraç çarpanı yalnızca *gereken teminatı* (margin) belirler. Çarpanı yükseltmek **riski değil, serbest teminatı ve likidasyona yakınlığı** değiştirir.
- Büyümeyi hızlandıran asıl kol **işlem başına risk yüzdesi**dir ve bu, kanıtlanmış edge'in alt güven sınırına (LCB) bağlanır (K6). Bot çarpanı ve riski buna göre **kendisi** seçer.
- **Spot yerine futures önceliği maliyet avantajıdır:** spot ~%0.10/taraf, futures ~%0.02 (maker) – %0.05 (taker). Aynı stop mesafesinde round-trip maliyet R cinsinden **yarıdan fazla** düşer (K4). Ayrıca futures'ta short mümkün.

### 1.3 Beklenti yönetimi (kodda ve raporlarda gösterilecek)
$30 hesapta tipik risk %1.5 ≈ $0.45/işlem. Kanıtlanmış +0.15 R/işlem ve günde 2 işlemle bile yaklaşık +%0.45/gün (ayda ~+%10, bileşik). Büyük sıçrama yoktur; sistem **önce kaybetmeyi bırakmalı**, sonra büyütmelidir. Hiçbir otomasyon kâr garantilemez; sistem edge bulamazsa **paper'a dönüp işlem açmamayı** seçmelidir (bu da bir sonuçtur).

---

## 2. Teşhis (neden bu durumdayız)

### 2.1 Neden kaldıraçlı işlem açılmıyor? (kodda doğrulandı)
Üç ayrı kapı futures'ı kapatıyor:
1. `ENABLE_FUTURES` varsayılanı **`False`** (`config/settings.py:81`); sunucu `.env`'inde de açılmamış.
2. `market_router.py:48-51`: `autonomy_level < FUTURES_MIN_LEVEL (1)` ise futures reddediliyor; `AUTONOMY_START_LEVEL` varsayılanı **0**, `.env`'de ayarlı değil.
3. **Sermaye iki cüzdana bölünmüş:** spot ~$8, futures serbest bakiye düşük/kullanılamaz; `exchange.py`'de **cüzdanlar arası transfer fonksiyonu yok** (`transfer` araması boş). Futures yolu `futures_free_usdt >= FUTURES_MIN_USDT (3)` ve `margin` şartlarına takılıyor; spot ise $11.50 minimumu karşılayamıyor. Sonuç: yüzlerce sinyal "Available free USDT ($8.07) below required notional ($11.50)" ile elendi.
Ek: futures emri bir kez minNotional altında gönderildi (`-4164: notional must be no smaller than 5`) → ön kontrol yok.

### 2.2 Neden strateji kaybediyor?
- 15m'de stop = 2×ATR(14) ≈ %0.8–1.2; round-trip %0.2 spot maliyeti = riskin ~%20–25'i. Aşırı dar stop + yüksek maliyet.
- Sabit TP = 2.5R: 32 mumda (8 saat) ulaşılması nadir; sinyallerin %68'i Timeout.
- Rejim/yön filtresi yok: BTC düşüş trendindeyken long dip alımı.
- Güven skoru kural puanı; olasılık değil.

### 2.3 Veri kalitesi ve operasyon açıkları (K0'da kapanır)
Telegram token'ı loglarda açık (regex `\b` hatası); watchdog `NameError` ile ~41 saat çalışmadı; dust için dakikada 2 INFO satırı (26.000+ satır); PHANTOM kapanışlar (ETH, SOL×2, ARB) gerçek dolum yerine tahmin; `U/USDT` (peg) işlem edilmiş; eski 5 işlemin PnL'i taşınmamış; `signal_report`'ta iki farklı metrik aynı değeri (−0.0746) gösteriyor (kontrol); sunucuda testler eski ve 1 kırmızı; saat kayması 320–450 ms.

---

## 3. Bağlayıcı tasarım kararları

| # | Karar | Gerekçe |
|---|---|---|
| **D1** | **Futures-öncelikli, tek cüzdan.** Ana işlem USD-M perpetual (isolated margin, one-way mode). Spot yalnızca toz/istisna. Sermaye **futures cüzdanında** tutulur (`FUTURES_ONLY_MODE=true`). Spot→futures transferi için opsiyonel `WalletBalancer` (Universal Transfer izni varsa); yoksa operatör bir kez taşır. | Cüzdan bölünmesi işlem açılmamasının kök nedeni; futures maliyeti düşük; short mümkün |
| **D2** | **İki yönlü** (long + short). Yön, üst zaman dilimi trendiyle belirlenir; trendin tersine "dip al/tepe sat" yok (yalnızca yatay rejimde). | Düşen piyasada long-only kaybettiriyor |
| **D3** | **Çok zaman dilimi:** trend/rejim 1h–4h, giriş/çıkış 15m. Stop mesafesi **ATR(1h) veya ATR(15m) katı olarak maliyet kapısından** geçer (min `cost_R ≤ 0.12`). | Maliyet/risk oranını düşürür |
| **D4** | **Strateji portföyü + kanıta dayalı sermaye tahsisi** (bandit). Tek strateji yok; hepsi `shadow → learn → exploit` yaşam döngüsünde. | Hangi stratejinin çalıştığını veri söyler |
| **D5** | **Bot kaldıraç çarpanını ve işlem riskini kendisi seçer** (`LeverageAdvisor`, K6); sınırlar sertleşmez ama kanıtla genişler. | Operatörün istediği özerklik |
| **D6** | **"Test hesabı" modu = bütçeli canlı öğrenme.** Canlıda deneme yapılır, ama bir *öğrenme bütçesi* ve strateji başına *kanıt kapıları* vardır (K8). Kanıtsız/negatif stratejiler canlıya girmez. | Canlı veri en gerçekçi öğretmen; bütçe ruinden korur |
| **D7** | **Mevcut `mean_reversion_dip` long kuralı canlıdan çıkarılır** (3 gün veride net −0.39 R). Yalnızca yeni kapılardan (G1) geçerse geri döner. | Kanıt negatif |
| **D8** | **Maliyet önce:** maker-öncelikli giriş/çıkış, işlem sayısı sınırı, funding takibi. | Net R'nin ilk düzeltme kalemi |

---

## 4. İş paketleri

### K0 — Acil düzeltmeler (P0, ilk 1 gün)
1. **Token/gizli bilgi sızıntısı:** `config/log_filter.py` Telegram regex'ini `(?:bot)?\d{8,12}:[A-Za-z0-9_-]{30,}` yap; `httpx` ve `apscheduler` logger'larını `WARNING`'e çek. Test: `test_filter_masks_telegram_url_token` (URL içinde `…/bot123456789:AAA…/getUpdates` maskelenir). **Operatör notu:** token yenilenecek (Bölüm 8).
2. **APScheduler hata alarmı:** `main.py`'ye `EVENT_JOB_ERROR` dinleyicisi ekle; aynı iş art arda 3 hata verirse Telegram alarmı + `safe_mode`; hata başına tam traceback **bir kez**, sonra sayaç. Smoke test: her zamanlanmış iş (`safe_watchdog_job`, `safe_reconcile_job`, resolver, scanner) fake bağımlılıklarla **bir kez çalıştırılıp hata vermediği** doğrulanır (`test_all_scheduled_jobs_smoke`). *Neden:* `time` import'u eksikliği 41 saat fark edilmedi.
3. **Dust sessizliği:** reconciler dust'ı DB'ye `dust_ignored(symbol, value, first_seen, last_seen)` yazsın; INFO yalnızca ilk görülmede ve değer artınca. Satışta miktarı **lot adımına aşağı yuvarlayıp** serbest bakiyenin tamamını sat; ücret base asitten kesiliyorsa net miktarı hesapla (dust üretimi azalsın).
4. **Futures minNotional ön kontrolü:** emir öncesi `market['limits']['cost']['min']` ve `amount.min`; `notional < min×1.1` ise **gönderme**, `reject_reason='min_notional'` yaz (hata fırlatma).
5. **Peg/stabil ve istenmeyen semboller:** `U/USDT`, `USDC`, `FDUSD`, `XAUT`, `RWUSD` vb. için config'li kara liste; `abs(24h değişim) > MAX_ABS_24H_CHANGE_PCT` (40) ve fiyat aralığı < %0.3 olan sembolleri hariç tut (peg tespiti: son 100 mumun `(high−low)/close` ortalaması < 0.002).
6. **PHANTOM kapanışlar:** `PHANTOM_POSITION_RESOLVED_CLOSED` durumunda önce `fetch_my_trades`/`fetch_order` ile gerçek çıkış dolumunu bul (SL fill'i `SERVER_SIDE_STOP_LOSS_FILLED` olarak işle); bulunamazsa `pnl_estimated=true` ve istatistikten hariç.
7. **Eski kayıtların PnL'i:** migration'ın taşımadığı 5 işlem için gerçek dolum geçmişinden `realized_pnl_usd` doldur (`tools/backfill_pnl.py`, salt-okunur borsa çağrısı; operatör çalıştırır).
8. **`signal_report.py` doğrulaması:** brüt R ile `high_vol` net R'sinin aynı çıkması (−0.0746) için formülleri kontrol et; birim test (sentetik 10 sinyal, elle hesaplanmış değerler). `R_net = R_gross − cost_R`, cost_R işlem bazında hesaplansın.
9. **Resolver doğruluğu:** resolver'ın giriş modeli canlı girişle aynı olsun (limit giriş fiyatı, dolmama durumu). Dolmayan limit girişler `NOT_FILLED` sonucu alsın (R hesabına girmesin, ayrı sayılsın). Karşılaştırma testi: aynı sinyal için paper fill modeli ile resolver sonucu eşleşir.
10. **Test/deploy senkronu:** sunucudaki `tests/` güncel olsun; `test_r4_i1_orphan_position_gets_market_closed` testini "önce SL kur" davranışına göre güncelle. `scripts/deploy.sh` testleri koşturmadan restart etmesin; CI (GitHub Actions: `pytest -q`, `ruff`).
11. **Saat kayması:** `CLOCK_WARN_MS` aşıldığında 30 dk'da bir uyarı (spam yok); `chronyc tracking` çıktısını `/saglik`'a ekle; `adjustForTimeDifference=True` + `recvWindow=10000` doğrula.
12. **Log/ disk:** `RotatingFileHandler` (20 MB × 5), `systemd.log` için `logrotate` örneği (`deploy/logrotate.quant-bot.example`).

**Kabul:** token maskeli, tüm zamanlanmış işler smoke testte yeşil, dust spam yok, futures minNotional hatası yok.

### K1 — Araştırma altyapısı (backtest + doğrulama) — **bu planın merkezi**
Amaç: hangi strateji/parametre **komisyon, funding ve kayma sonrası** pozitif beklenen değer veriyor? Tahmin yerine ölç.

1. **Veri:** `tools/research/download.py` — Binance public USD-M perpetual verisi: 15m ve 1h mumlar, **funding rate geçmişi**, son **6–12 ay**, `top-N likit` semboller + BTC/ETH. Disk önbelleği (`research_data/`, git'e eklenmez).
2. **Backtester (`tools/research/backtest.py`)**: bar-bar, **yalnızca kapanmış mumlar**, aynı `score()` fonksiyonunu canlıyla paylaşır. Modeller:
   - **Giriş:** maker (limit) ve taker modları; maker için "fiyat limite dokunmazsa dolmaz" kuralı; zaman aşımı.
   - **Maliyet:** futures taker %0.05 / maker %0.02 (config), BNB indirimi opsiyonu, **funding** (8 saatte bir pozisyon süresince), **kayma** (`SLIPPAGE_PCT`, ATR'ye bağlı seçenek).
   - **Çıkış:** SL/TP/trailing/zaman çıkışı; aynı mumda SL ve TP görünürse **SL önce**.
   - **Likidasyon:** kaldıraç ve margin ile likidasyon fiyatı simüle edilir.
3. **İstatistik protokolü (zorunlu):**
   - **Purged walk-forward:** örn. 60 gün eğitim / 15 gün test, kaydır; testler arası boşluk (embargo) = pozisyon süresi.
   - **Etkin örnek:** işlemleri güne/sembole göre kümeleyerek **cluster bootstrap** (10.000 yeniden örnekleme) ile net R ortalamasının %90 güven aralığı.
   - **Çoklu test kontrolü:** denenen konfigürasyon sayısı `experiment_registry`'ye yazılır; kabul eşiği `alpha / K` (Bonferroni) veya **Deflated Sharpe**; kayıtsız deneme yasak.
   - **Kabul (G1):** test dönemlerinin **birleşik** sonucunda n ≥ 150 işlem, net R ortalaması > 0 ve alt güven sınırı (LCB, %80) ≥ −0.05, en az 3 ayrı piyasa bölgesinde (yükselen/düşen/yatay) negatif olmayan sonuç.
4. **Raporlar:** `tools/research/report.py` → strateji × yön × rejim × saat; equity eğrisi; maks. drawdown; maliyet kırılımı (komisyon, funding, kayma); parametre duyarlılığı (kalın bölge mi, tek nokta mı?).
5. **Parametre bütçesi:** her strateji için en çok **12 konfigürasyon** (grid) — fazlası yanlış pozitif üretir. Konfigürasyonlar `research/configs/*.yaml`'da sürümlenir.

**Testler:** `test_backtest_sl_first_same_candle`, `test_backtest_maker_fill_requires_touch`, `test_backtest_includes_funding`, `test_liquidation_before_stop_is_detected`, `test_cluster_bootstrap_ci_known_case`, `test_registry_counts_trials`, `test_no_lookahead` (gelecek veri ile skor değişmemeli).
**Kabul:** `python -m tools.research.run --strategy trend_pullback --days 270` tekrarlanabilir rapor üretir (aynı tohum aynı sonuç).

### K2 — Strateji seti v2 (long/short), hepsi başlangıçta `shadow`
Her strateji `engine/strategies/<ad>.py` içinde saf `score(state) -> Signal(side, entry, stop, tp_plan, features)`; canlı ve backtest aynı kodu çalıştırır. **Aşağıdakiler hipotezdir; K1 karar verir.**

| ID | Strateji | Fikir | Giriş (özet) | Çıkış |
|---|---|---|---|---|
| **S1** | `trend_pullback` | Güçlü trendde geri çekilme alımı/satışı | 1h trend: EMA50>EMA200 ve ADX≥22 (long) / tersi (short); 15m'de EMA21'e geri çekilme + RSI(14) dönüşü; hacim teyidi | Chandelier trailing (ATR×2.5), 1.5R'de yarım TP, break-even, zaman çıkışı 24 bar |
| **S2** | `vol_breakout` | Sıkışmadan sonra genişleme (düşük kazanma oranı, yüksek R) | 1h Bollinger bant genişliği son 20 mumun alt %20'sinde → Donchian(20) kırılımı + hacim ≥1.5× | Trailing ATR×3, TP yok (kısmi TP 2R) |
| **S3** | `range_fade` | Yalnızca yatay rejimde ucuz geri dönüş | ADX(1h)<18 ve BTC 4h EMA eğimi ≈0; fiyat BB(2σ) dışında + RSI uç | TP = orta bant (≈1R), SL = ATR×1.2, kısa süre |
| **S4** | `bear_breakdown_short` | Düşüş trendinde kırılım/tepki satışı | BTC<EMA200(4h), sembol 1h EMA50<EMA200; destek kırılımı veya EMA21 reddi | Trailing ATR×2.5, zaman çıkışı |
| **S5** | `funding_skew` (araştırma) | Aşırı funding'e karşı pozisyon | `|funding| ≥ eşik` ve fiyat momentumu tersine dönüş | TP/SL ATR bazlı |

Ortak kurallar:
- **Stop geometrisi (maliyet kapısı):** `stop_mesafesi = max(ATR_çarpanı × ATR, MIN_STOP_PCT)`; `cost_R = (round_trip_fee + slippage + beklenen_funding)/stop_mesafesi ≤ MAX_COST_R (0.12)`. Aşılırsa işlem **yok**.
- Sabit "TP = 2.5R" **kaldırılır**; çıkış planı stratejiye özgü (K3).
- **Rejim filtresi çekirdekte:** yön, 4h trend ve BTC durumuyla uyumlu olmayan sinyal üretilmez (S3 hariç).
- `Signal.confidence` yeniden adlandırılır: `signal_score`. **Olasılık gibi kullanılmaz** (K7 kalibrasyonu gelene dek boyutlandırmada yok).
- Mevcut `mean_reversion_dip` ve `intraday_bounce` stratejileri **`suspended`** olarak kayıtlıdır (D7).

**Testler:** her strateji için `test_<ad>_long_signal_case`, `test_<ad>_short_signal_case`, `test_<ad>_no_signal_against_trend`, `test_cost_gate_blocks_tight_stop`, `test_no_lookahead_<ad>`.

### K3 — Çıkış motoru v2
- **Çıkış planı nesnesi:** `ExitPlan{stop, trail_type(chandelier|atr|none), trail_mult, partial_tp[(R, frac)], breakeven_at_R, time_stop_bars, tp_maker:bool}`.
- **Borsa tarafı:** futures'ta SL `STOP_MARKET` (`MARK_PRICE`, `reduceOnly`/`closePosition`), TP `TAKE_PROFIT_MARKET` veya maker `LIMIT reduceOnly`; trailing için bot, SL'yi her 15m mum kapanışında **güncelleyen** emir (iptal-yeniden kur değil; mümkünse `modify`/yeni emri önce kur, sonra eskiyi iptal et — **hiçbir an korumasız kalma**).
- **Zaman çıkışı** stratejiye göre; `LEGACY_POSITION_GRACE_HOURS` mantığı korunur.
- Tüm çıkışlar `orders` tablosuna gerçek dolumla yazılır (S5 kuralı).
**Testler:** `test_trailing_never_leaves_position_unprotected`, `test_partial_tp_moves_stop_to_breakeven`, `test_chandelier_ratchets_only_in_favor`.

### K4 — Maliyet motoru ve işlem disiplini
1. `engine/cost_model.py`: market (spot/futures) ve emir tipine (maker/taker) göre ücret; **gerçek ücret kademesini** borsadan oku (`fetch_trading_fee`); BNB indirimi bayrağı; funding'i pozisyon süresi için tahmin et (`fetchFundingRate`).
2. **Maker-öncelikli giriş:** limit emri `ENTRY_MAKER_TIMEOUT_BARS` (1–2 bar) bekler; dolmazsa **kovalamak yok**, sinyal `MISSED` (öğrenme için kaydedilir: "kaçırılan sinyal sonucu").
3. **İşlem disiplini:** `MAX_TRADES_PER_DAY` (başlangıç 6), aynı sembolde **soğuma** (SL sonrası 4 saat), korele pozisyon sınırı, **Q ve strateji kanıt durumuna göre** önceliklendirme (en iyi `expected_net_R` olan önce).
4. `expected_net_R` = (strateji posterior ortalama R) − cost_R; ≤ 0 ise işlem yok.
**Testler:** `test_cost_gate_includes_funding`, `test_maker_unfilled_is_recorded_as_missed`, `test_trade_cap_prioritizes_highest_expected_r`.

### K5 — Futures yürütme v2 (kaldıraçlı işlem gerçekten açılsın)
1. **Kapıları kaldır/yeniden tanımla:**
   - `ENABLE_FUTURES` varsayılanı **`true`**, `FUTURES_ONLY_MODE=true`.
   - `FUTURES_MIN_LEVEL` kavramı **kaldırılır**; futures erişimi artık **strateji kanıt durumu ve öğrenme bütçesiyle** (K8) belirlenir, `autonomy_level` sayısıyla değil.
   - `market_router`: spot yalnızca `ALLOW_SPOT_FALLBACK=false` iken devre dışı; futures uygun değilse **işlem yok** (spot'a sessiz düşme yok).
2. **Cüzdan:** `WalletBalancer` (varsayılan `AUTO_TRANSFER=false`): spot→futures `universal transfer` yetkisi varsa spot USDT'yi `FUTURES_WALLET_TARGET_PCT` (95) hedefine taşır; yetki yoksa `/saglik`'ta "sermaye bölünmüş" uyarısı ve operatöre talimat. Transfer yalnızca açık pozisyon/emir yokken ve `MIN_TRANSFER_USD` üstünde.
3. **Emir akışı:** (a) kaldıraç ve isolated margin ayarı **doğrula** (borsa geri okuma), (b) giriş limit/maker, (c) dolum miktarına göre SL (`STOP_MARKET`, `MARK_PRICE`) **dolumdan ≤5 sn** içinde, (d) açılıştan sonra `liquidationPrice` doğrula (R6/R3 tamponu `LIQ_BUFFER_X≥2.0`), ihlalde hemen kapat, (e) TP/trailing planı kur.
4. **Short:** `side=sell` giriş, `reduceOnly buy` çıkışlar; short için likidasyon üst tarafta; **funding ödeyen** tarafta pozisyon tutma maliyeti hesaba girer.
5. **Hesap modu:** hedge mode kapalı (one-way); borsa ayarı açılışta doğrulanır, farklıysa işlem yok + uyarı.
6. Futures `fetch_positions` ve `fetch_open_orders(params={'stop':True})` reconciler ile entegre (yetim benimseme zaten var; **futures yetim = önce SL**).
**Testler:** `test_futures_entry_sets_sl_within_5s`, `test_short_entry_and_exit_reduce_only`, `test_liq_price_verified_after_open`, `test_no_silent_spot_fallback`, `test_wallet_split_warning`, `test_leverage_setting_readback_mismatch_blocks_entry`.

### K6 — `LeverageAdvisor` ve `RiskAllocator` (bot çarpanına kendisi karar verir)
Saf fonksiyon, yoğun testli. **Girdiler:** strateji posterior (K7), stop mesafesi (ATR), likidasyon tamponu, bakiye/margin, açık risk, drawdown durumu, funding, sembol oynaklığı, öğrenme/exploit modu.

```
# 1) İşlem başına risk (equity'nin yüzdesi)
mu_lcb      = strateji net R ortalamasının alt güven sınırı (posterior %80 LCB)
sigma_R     = strateji R standart sapması (örnek yoksa 1.3 varsayılan)
kelly_R     = max(0, mu_lcb) / sigma_R**2                 # 1R riske alınacak equity oranı (R birimli Kelly)
risk_pct    = clamp( KELLY_FRACTION * kelly_R * 100, RISK_MIN_PCT, RISK_MAX_PCT_BY_STATE )
              # KELLY_FRACTION=0.25 ; learn modunda risk_pct = LEARN_RISK_PCT sabit
risk_pct   *= drawdown_throttle(dd)        # drawdown büyüdükçe ×1 → ×0.25
risk_pct   *= vol_throttle(atr_pct_zscore) # aşırı oynaklıkta azalt

# 2) Boyut
notional    = risk_usd / (stop_dist + round_trip_cost_pct + slippage_pct)
notional    = min(notional, MAX_POSITION_NOTIONAL_X * equity)

# 3) Çarpan (kaldıraç) seçimi: bot kendisi seçer
L_min       = ceil(notional / (equity * MAX_MARGIN_PCT_PER_TRADE/100))   # margin tavanını karşılayan en küçük L
L_liq       = en büyük L:  liq_mesafesi(L) >= LIQ_BUFFER_X * stop_dist     # likidasyon tamponu
L_cap       = kanıt durumuna göre tavan (learn: 5, exploit-low: 8, exploit-high: 12, mutlak: MAX_LEVERAGE=20)
L           = clamp( ceil(L_min * 1.0), 1, min(L_liq, L_cap) )          # margin verimli en düşük güvenli L
              # aynı anda çok pozisyon için serbest teminat kalacak şekilde L artırılabilir: L <= min(L_liq, L_cap)
margin      = notional / L
```
- Çarpanı **yüksek seçmek bir hedef değil, serbest teminatı koruyan araç**tır. `notional` riskten gelir; "daha fazla kaldıraç = daha büyük pozisyon" yoktur.
- `RISK_MAX_PCT_BY_STATE`: `learn` 0.5 · `exploit_low` 1.5 · `exploit_high` 3.0 (tavan, `MAX_RISK_PER_TRADE_PCT_HARD=3.0`).
- Toplam açık risk `MAX_TOTAL_OPEN_RISK_PCT` (learn 3, exploit 6) ve brüt notional `MAX_GROSS_EXPOSURE_X` (learn 3, exploit 5) aşılamaz.
- Hesap: $30, risk %1.5 = $0.45, stop %1 → notional $45 (1.5× equity); margin tavanı %25 ($7.5) → L_min=6; likidasyon tamponu uygun → L=6, margin $7.5. Aynı risk, ikinci pozisyon için $22 serbest teminat kalır.
**Testler:** `test_risk_scales_with_mu_lcb`, `test_zero_edge_means_learn_risk_only`, `test_leverage_is_minimal_safe`, `test_leverage_never_breaches_liq_buffer`, `test_drawdown_throttle`, `test_notional_derives_from_risk_not_leverage`.

### K7 — Öğrenme döngüsü v3 (gerçek öğrenme)
1. **Giriş-anı özellik kaydı** (`trade_features`): her işlem ve **her sinyal** için strateji, yön, rejim, ADX/RSI/ATR%, BB/EMA mesafeleri, hacim oranı, OB dengesizliği, funding, saat, BTC durumu, `cost_R`, `signal_score`.
2. **Sonuç ve atıf:** her sinyal/işlem için `r_net`, `mae_r`, `mfe_r`, çıkış nedeni, kayma; `MISSED`/`NOT_FILLED` ayrı.
3. **Strateji posterior'ı:** her `(strateji, yön, rejim)` için net R'nin Normal–ters-gama veya bootstrap tabanlı posterior'u; `mu_mean`, `mu_lcb(80%)`, `P(mu>0)`, n. Küçük n'de **güçlü shrinkage** (öncül: μ=−0.05 R, zayıf negatif; çünkü maliyet kayıp yönünde).
4. **Kalibrasyon:** `signal_score` → gerçekleşen isabet (isotonic, n≥100). Kalibre değilse skor **boyutlandırmada kullanılmaz** (yalnızca sıralama).
5. **Otomatik parametre ayarı yalnızca "meydan okuyucu" ile:** parametre değişikliği `shadow`'da paralel çalışır; walk-forward + canlı shadow'da şampiyonu **istatistiksel olarak** geçerse terfi eder; haftalık yeniden araştırma işi (`research_weekly`) K1 boru hattını son veriyle çalıştırır ve `proposed_config` üretir; uygulama için G1/G2 kapıları aranır.
6. **Hata analizi (deterministik):** `pattern_key=(strateji, yön, rejim, rsi_bin, adx_bin, saat_bin)`; `n ≥ 12` ve LCB < 0 ise o desen **askıya alınır** (TTL 7 gün, sonra shadow'da yeniden test). LLM yalnızca insan-okur özet yazar, **karar vermez** (çıktı doğrulaması: tek cümle, ≤200 karakter, prompt kalıbı yok; aksi halde şablon).
7. Eski çöp dersler ve cezalar migration ile `validated=false` ve ceza 0.
8. **Raporlama:** `/ogrendim` ve günlük raporda "bugün ne öğrendim": strateji posterior değişimleri, askıya alınan/serbest kalan desenler, kaçırılan sinyaller.
**Testler:** `test_posterior_shrinks_small_n`, `test_pattern_suspended_only_with_enough_evidence`, `test_calibration_not_used_until_n100`, `test_challenger_promotes_only_with_evidence`, `test_features_recorded_for_every_signal`.

### K8 — Bütçeli canlı öğrenme ("test hesabı modu")
Operatör hesabı test hesabı gibi görüyor; bu, **canlıda deneyerek öğrenmeye** izin verir. Kural: *deneme serbest, ama sınırlı ve ölçülü.*
1. **Strateji yaşam döngüsü (otomatik):** `shadow` → `learn` → `exploit_low` → `exploit_high`; `suspended` (negatif kanıt). Geçişler **yalnızca kapılarla** (Bölüm 6).
2. **`learn` modu:** canlı, gerçek para; işlem başına risk `LEARN_RISK_PCT` (0.5), kaldıraç tavanı 5x, strateji başına eşzamanlı en çok 1 pozisyon.
3. **Öğrenme bütçesi:** `LEARNING_BUDGET_PCT` (15) = başlangıç equity'sinin %15'i **toplam realize edilmiş net kayıp** sınırı (rolling 14 gün). Aşılırsa tüm `learn` işlemleri **paper**'a döner; ek kanıt/araştırma gelene kadar kapalı. Her bütçe yenilemesi **operatör onayı** ister (Telegram `/onayla_ogrenme`).
4. **Bandit tahsisi:** `learn` işlemlerinin hangi stratejiye gideceği Thompson sampling ile (posterior belirsizliği yüksek ve beklenen değeri olası olan strateji daha çok deneme alır); toplam deneme sayısı `MAX_LEARN_TRADES_PER_DAY` (4).
5. **Giriş koşulu (G1):** Bir strateji `learn`'e ancak K1 offline taramasını geçince (Bölüm 6, G1) girer. **Mevcut `mean_reversion_dip`/`intraday_bounce` G1'i geçmediği için canlıda çalışmaz** (D7); paper'da çalışmaya devam eder (veri için).
6. **Kaçırılan ve gölge sinyaller** ayrı sayılır; öğrenme yalnızca gerçek dolum + simülasyon sonuçlarını **karıştırmadan** ayrı raporlar (`source=live|paper|shadow`).
**Testler:** `test_learning_budget_halts_live_learn`, `test_strategy_without_g1_never_goes_live`, `test_thompson_prefers_uncertain_promising`, `test_budget_renewal_requires_operator_approval`.

### K9 — Otonomi v2 (seviye yerine kanıt durumu)
- `autonomy_level` sayısı **bilgi amaçlı** kalır; yetkiyi belirleyen şey **strateji bazlı durum** (K8) ve **hesap sağlığı** (drawdown, anomali).
- Hesap durumları: `NORMAL`, `CAUTION` (drawdown ≥ %6 → risk×0.5), `HALT_DAY` (günlük kayıp), `COOLDOWN` (drawdown ≥ %12 → 24 saat işlem yok, tüm stratejiler `learn`'e), `SAFE_MODE` (anomali).
- Otomatik terfi/düşüş günlük işte; her değişiklik `state_log`'a ve Telegram'a gerekçeyle yazılır.
- `AUTONOMY_START_LEVEL` kaldırılır ya da yalnızca eski testler için bırakılır; canlı mod seviye 0'da başlatılamaz sorunu ortadan kalkar.
**Testler:** `test_drawdown_triggers_caution_and_cooldown`, `test_state_changes_are_logged`.

### K10 — Güvenlik katmanı (değişmez çekirdek)
Bunlar **gevşetilmez**; yumuşatma yalnızca risk yüzdeleri ve kaldıraç tavanlarındadır:
1. Her pozisyonda borsa tarafı SL (dolumdan ≤5 sn), isolated margin, `reduceOnly` çıkışlar.
2. Likidasyon fiyatı ≥ `LIQ_BUFFER_X`(2.0)×stop mesafesi; açılış sonrası borsadan doğrulama.
3. Günlük kayıp (`DAILY_MAX_LOSS_PCT` 4), maks. drawdown (`MAX_DRAWDOWN_PCT` 15) ve cooldown.
4. Reconciler (yetim benimseme, SL varlık kontrolü), `entries_blocked`.
5. **Volatilite devre kesici:** BTC 5 dakikada >%1.5 hareket veya ATR% z-skoru >3 iken yeni giriş yok, açık pozisyonlarda SL sıkılaştırma opsiyonu.
6. Funding tavanı (`MAX_FUNDING_PCT`), borsa durumu ve sembol `TRADING` kontrolü, saat kayması blokajı (`CLOCK_BLOCK_MS`).
7. Kill-switch (`/panic`) futures `reduceOnly` kapatma, paper'da borsaya hiçbir yazma.
8. API anahtarı: **withdraw kapalı**, IP kısıtı açık; gizli bilgi maskeleme.

### K11 — Gözlemlenebilirlik ve "broker raporu"
- **Günlük rapor (Telegram, 21:00 UTC):** equity (toplam, futures, spot), günlük/haftalık net PnL (komisyon+funding dahil), açık pozisyonlar (yön, kaldıraç, margin, likidasyon mesafesi, SL/TP durumu), strateji tablosu (durum, n, μ_mean, μ_lcb, P(μ>0), bu gün PnL), öğrenme özeti, bütçe durumu (`LEARNING_BUDGET` kullanımı), sistem sağlığı, **bu gün botun kendi aldığı kararlar** (çarpan/risk seçimleri ve nedenleri).
- **`/saglik`:** mod, DB, hesap durumu, reconcile, resolver, job hataları, saat kayması, bellek/swap, cüzdan bölünmesi.
- **Dashboard:** equity eğrisi, strateji performansı, karar zaman çizgisi; salt-okunur + onaylı yazma uç noktaları, parola ≥16, hız sınırı.
- **Karar günlüğü (`decision_log`)**: her işlem için "neden bu strateji, neden bu risk/kaldıraç, neden bu stop" (yapılandırılmış alanlar, LLM metni değil).

### K12 — Operasyon
`scripts/deploy.sh` (testler → preflight → açık emir/pozisyon yoksa → DB yedeği → restart), `scripts/switch_mode.py`, migration'lar, CI, `deploy/quant-bot.service.example` (`TimeoutStopSec=90`, `MemoryHigh/Max`), DB günlük yedeği (şifreli, `.env` hariç), `preflight_check.py` (API izinleri, hedge-mode kapalı, saat, bellek).

---

## 5. Yeni/değişen ayarlar (varsayılanlar)

| Ayar | Varsayılan | Not |
|---|---|---|
| `ENABLE_FUTURES` | `true` | D1 |
| `FUTURES_ONLY_MODE` | `true` | spot yalnızca dust |
| `ALLOW_SPOT_FALLBACK` | `false` | sessiz spot'a düşme yok |
| `AUTO_TRANSFER` | `false` | Universal Transfer yetkisi gerekir |
| `FUTURES_WALLET_TARGET_PCT` | `95` | |
| `MAX_LEVERAGE` | `20` | mutlak tavan |
| `LEVERAGE_CAP_LEARN` / `_EXPLOIT_LOW` / `_EXPLOIT_HIGH` | `5` / `8` / `12` | bot bunun altında kendisi seçer |
| `LIQ_BUFFER_X` | `2.0` | alt sınır 2.0 |
| `MAX_MARGIN_PCT_PER_TRADE` | `25` | |
| `LEARN_RISK_PCT` | `0.5` | |
| `RISK_MAX_PCT_EXPLOIT_LOW` / `_HIGH` | `1.5` / `3.0` | `MAX_RISK_PER_TRADE_PCT_HARD=3.0` |
| `KELLY_FRACTION` | `0.25` | |
| `MAX_TOTAL_OPEN_RISK_PCT` | `3` (learn) / `6` (exploit) | |
| `MAX_GROSS_EXPOSURE_X` | `3` (learn) / `5` (exploit) | |
| `DAILY_MAX_LOSS_PCT` | `4` | |
| `MAX_DRAWDOWN_PCT` | `15` | `CAUTION` ≥6, `COOLDOWN` ≥12 |
| `LEARNING_BUDGET_PCT` | `15` | rolling 14 gün, operatör onayıyla yenilenir |
| `MAX_LEARN_TRADES_PER_DAY` | `4` | |
| `MAX_TRADES_PER_DAY` | `6` | |
| `FUTURES_FEE_MAKER_PCT` / `_TAKER_PCT` | `0.02` / `0.05` | borsadan gerçek değer okunur |
| `SLIPPAGE_PCT` | `0.04` | ATR'ye bağlı seçenek |
| `MAX_COST_R` | `0.12` | maliyet kapısı |
| `MIN_STOP_PCT` | `0.8` | |
| `ENTRY_MAKER_TIMEOUT_BARS` | `2` | |
| `MAX_ABS_24H_CHANGE_PCT` | `40` | |
| `SYMBOL_BLACKLIST` | `U,USDC,FDUSD,XAUT,RWUSD,…` | peg dedektörü de var |
| `G1_MIN_TRADES` / `G1_LCB_MIN_R` | `150` / `-0.05` | offline kapı |
| `G2_MIN_LIVE_TRADES` / `G2_P_MU_POS` | `60` / `0.80` | learn→exploit_low |
| `G3_MIN_LIVE_TRADES` / `G3_P_MU_POS` | `150` / `0.95` | exploit_low→exploit_high |
| `RESEARCH_MAX_CONFIGS_PER_STRATEGY` | `12` | |

---

## 6. Kanıt kapıları (kodda zorunlu, ajan gevşetemez)

| Kapı | Geçiş | Koşul |
|---|---|---|
| **G0** | strateji → `shadow` | kod + birim testler geçti |
| **G1** | `shadow` → `learn` (canlıya ilk giriş) | K1 purged walk-forward: n ≥ 150 işlem, **net R ortalaması > 0**, LCB(%80) ≥ −0.05, 3 piyasa bölgesinde negatif değil, çoklu test düzeltmesi geçti, maliyet kırılımı raporlandı |
| **G2** | `learn` → `exploit_low` | canlı learn n ≥ 60, `P(μ>0) ≥ 0.80`, canlı μ_mean > 0, drawdown < %10, SL/likidasyon ihlali yok |
| **G3** | `exploit_low` → `exploit_high` | n ≥ 150 (toplam), `P(μ>0) ≥ 0.95`, μ_lcb > 0.05 R, reconcile ihlali 0 |
| **Düşüş** | herhangi → `suspended` | rolling-30 net R < −0.15 **veya** `P(μ>0) < 0.30` (n ≥ 30) |
| **Hesap** | `CAUTION/COOLDOWN` | K9 |

---

## 7. Uygulama sırası

| Faz | İçerik | Çıkış kriteri |
|---|---|---|
| **F0 (1–2 gün)** | K0 | Token maskeli; job smoke yeşil; dust/minNotional/peg düzeltmeleri; PnL backfill aracı |
| **F1 (3–7 gün)** | K1 + K4 (cost model) | Tekrarlanabilir backtest raporu; mevcut stratejinin **offline sonucu** (D7'yi doğrular/çürütür) |
| **F2 (1 hafta)** | K2 + K3 | S1–S4 backtest'te çalışıyor; hangileri G1'i geçti raporu |
| **F3 (1 hafta)** | K5 + K6 | Futures long/short **paper'da** uçtan uca; likidasyon/SL doğrulamaları; LeverageAdvisor testleri |
| **F4 (1 hafta)** | K7 + K8 + K9 | Öğrenme döngüsü, bütçe, kanıt kapıları; G1'i geçen stratejiler `learn`'de **canlıya** minimal risk |
| **F5** | K10–K12 | Rapor, dashboard, ops, CI |

**Her faz sonunda dur:** değişen dosyalar, testler, `pytest -q`, `TODO(user)`; onay bekle.

---

## 8. Operatör (sunucu) yapılacaklar — ajan yapmaz

1. **Telegram token'ını yenile** (BotFather `/revoke`) ve `.env`'i güncelle; eski logları sil. (Token loglarda düz metin görüldü.)
2. **Sermayeyi futures cüzdanına taşı** (Binance → Transfer → Spot→USDⓈ-M Futures) ve **API anahtarında "Enable Futures" ve "Enable Reading" açık, withdraw kapalı, IP kısıtı açık** olduğundan emin ol. `AUTO_TRANSFER` istersen ek olarak "Permits Universal Transfer" gerekir (isteğe bağlı).
3. **Hesap modunu doğrula:** USDⓈ-M Futures → One-way Mode, Isolated margin, BNB ile ücret indirimi (isteğe bağlı).
4. **`.env`:** yeni ayarlar `.env.example`'dan; `TRADING_MODE`'u açık yaz. **Canlıyı F4'e kadar paper'da (`TRADING_MODE=paper`) tut**; çünkü mevcut stratejiler G1'i geçmiyor (D7). F4'te `learn` canlı işlemi, G1 geçen strateji için otomatik açılır; **bütçe yenileme** Telegram `/onayla_ogrenme` ile.
5. **Yedek:** `tar --exclude=.env --exclude='*.db'`; veritabanı ayrı ve şifreli.
6. **K0-7** için `tools/backfill_pnl.py`'yi sunucuda çalıştır (salt-okunur API).

---

## 9. Açık noktalar (ajan çıktısının sonuna ekle)

1. Universal Transfer yetkisi var mı? (`AUTO_TRANSFER` için)
2. Futures hesabı One-way/Isolated mı? Mevcut açık futures pozisyon/emir var mı?
3. `LEARNING_BUDGET_PCT=15` ve `LEARN_RISK_PCT=0.5` uygun mu?
4. Araştırma verisi için ağ erişimi (Binance public) mevcut mu, süre/disk sınırı?
5. Short pozisyonlara başlangıçtan izin veriliyor mu? (`ENABLE_SHORT`, varsayılan `true`)

---

## 10. Antigravity'ye yapıştırılacak başlangıç istemi

```
Repodaki QUANTBOT_BROKER_PLAN_V4.md dosyasını baştan sona oku ve Bölüm 0 kurallarına uy.
Tasarım kararları (D1–D8) bağlayıcıdır; tartışma, uygula.
Yeni branch: feat/v4-broker. Önce F0 (K0) fazını uygula: önce testleri yaz ve kırmızı olduklarını göster
(Telegram URL token maskeleme, tüm zamanlanmış işler smoke testi, dust spam, futures minNotional ön kontrolü).
Sonra F1 (K1 araştırma altyapısı + K4 cost model) fazına geç. Backtest ve kanıt kapıları (Bölüm 6) koda gömülü olacak.
Canlı servise, .env'e, data/*.db'ye ve gerçek emirlere ASLA dokunma; testlerde fake exchange kullan.
Her faz sonunda pytest -q çıktısı, değişiklik özeti ve TODO(user) listesi ver; onay bekle.
```
