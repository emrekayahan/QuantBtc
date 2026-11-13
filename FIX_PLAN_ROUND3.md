# QuantBot Live — 3. Tur: Canlı Yedek Analizinden Çıkan Hatalar ve Eksikler

> **Bu dosya Antigravity (AI kodlama ajanı) içindir.**
> Girdi: sunucu yedeği `quant_bot_live_backup_20261112_0058` (kod + loglar + 3 DuckDB dosyası). 50 test geçiyor.
> Önceki planlar: `QUANTBOT_FIX_PLAN.md` (T0–T14), `FIX_PLAN_ROUND2.md` (R1–R19). Bu dosya onların **devamıdır**. Çakışmada en yeni dosya geçerli.
> **Bu dosyadaki bulgular loglardan, veritabanlarından ve kod okumasından doğrulanmıştır.** "Doğrula" yazan maddeler hipotezdir: önce kanıtla, sonra düzelt.

---

## 0. Ajan için çalışma kuralları (ZORUNLU)

1. **Canlı servise, sunucudaki `.env`'e ve gerçek emirlere dokunma.** Testlerde gerçek borsa çağrısı yok, sahte (fake) exchange kullan.
2. **Gizli bilgi yazdırma/commit etme:** API anahtarı, token, şifre, sunucu IP'si, ev dizini yolları. Yedekteki `.env` ve `data/*.db` dosyalarını **asla repoya ekleme**; `.gitignore`'u doğrula.
3. Yeni branch: `fix/round3`. Her görev (`S1`, `S2`, …) ayrı commit: `fix(Sn): …`.
4. **Önce testi yaz ve kırmızı olduğunu göster, sonra düzelt.** Bölüm 8'deki regresyon testleri zorunlu.
5. Yeni/mevcut her ayar için **kodda gerçek bir kullanım noktası ve test** olsun. Kullanılmayan ayar kalmasın (Bölüm 5).
6. Davranış değişikliklerini config bayrağıyla ver, **varsayılan = güvenli taraf**.
7. P0 → P1 → P2 sırası. Her faz sonunda `pytest -q` yeşil, değişiklik özeti, `TODO(user)` listesi; **onay bekle.**
8. Emin olmadığın yerde varsayım yapma, `TODO(user):` bırak.

---

## 1. Durum özeti

### 1.1 Round 2'den çalışan parçalar (kanıtlı)
- Merkezi paper koruması (`BinanceExchangeWrapper._assert_live_or_raise`), `paper_fill.py`, paper ledger.
- `RiskSizer` risk kapısına bağlı (`risk_engine.py`).
- Reconciler borsayı tarıyor (I1 yetim pozisyon tespiti çalışıyor, `reconcile_events` doluyor).
- Gece Brier: 0 örnekte `INSUFFICIENT_DATA`, parametre değişmiyor (`overnight_audit_2026_11_11.md`).
- Mod başına ayrı DB (`quant_bot_live.db` / `quant_bot_paper.db`), dashboard `127.0.0.1`.
- Graceful shutdown: 21:23, 21:37, 21:46 yeniden başlatmalarında hata/iptal yok.
- Sunucu `.env`: `TRADING_MODE=live`, `MAX_OPEN_POSITIONS=2`, `DAILY_MAX_TRADES=6` (güncel).

### 1.2 11 Kasım 2026 akşamı olay zinciri (UTC, loglardan)
| Saat | Olay |
|---|---|
| 21:24 | Bot **paper** modunda yeniden başladı (`exchange recovery skipped`). |
| 21:37:59 | Bot **live** modunda açıldı; **boş** `quant_bot_live.db` kullanıldı. Eski DB'deki gerçek pozisyonlar "yetim" göründü. |
| 21:38:06–07 | R4 otomatik iyileştirme: futures **ONE (5 kontrat) ve SOL (1.08)** market `reduceOnly` ile kapatıldı (`I1_ORPHAN_FUTURES_CLOSED`). |
| 21:38–21:46 | Spot **BTC (0.00015249, ~$11.36)** ve **DOGE (21, ~$1.44)** için her 30 sn `CRITICAL: Failed to remediate I1 orphan spot … insufficient balance` (36 kez). `entries_blocked=true`, `reconcile_ok=false`. |
| 21:47 | Yeniden başlatma. Eski DB'de BTC ve DOGE `MAX_HOLD_TIME_EXPIRED` ile kapandı; eski DB'de equity $31.76, `entries_blocked=false`. |
| 21:52 | `outcome_resolver`: 35+ sinyal için `fetch_ohlcv() got an unexpected keyword argument 'since'`. |

> **Hangi DB'nin şu an aktif olduğu kesinleşmedi:** `.env`'de `DB_PATH` yok ve kod live için `quant_bot_live.db` seçiyor, ama `quant_bot.db.wal` 21:57'de yazılmış. Bu çelişki S4'te çözülecek.

### 1.3 Gerçek işlem sonuçları (eski DB, `positions`)
| Sembol | PnL (DB) | Kapanış nedeni |
|---|---|---|
| ONE | −0.2213 | SL |
| ZEC | −0.3018 | SL |
| SOL | **+1.3089** | `PHANTOM_POSITION_RESOLVED_CLOSED` (**tahmini**, gerçek dolum değil) |
| BTC | +0.1219 | `MAX_HOLD_TIME_EXPIRED` |
| DOGE | +0.0112 | `MAX_HOLD_TIME_EXPIRED` |

n=5, edge hakkında sonuç çıkarılamaz. Equity $30.23 (10 Kasım) → $31.76 (11 Kasım). 222 "high-conviction" sinyal loglandı, 5 işlem açıldı; 10 Kasım 21:00'dan sonra yeni giriş yok (`Correlated longs cap reached (3/2)`: üç pozisyon açıkken cap=2 ve zaman çıkışı yoktu).

---

## 2. Bulgu özeti

| # | Bulgu | Önem | Kanıt |
|---|---|---|---|
| B1 | `outcome_resolver` çalışmıyor → `signal_outcomes` hep boş | **KRİTİK** | `engine/exchange.py:207` `fetch_ohlcv(symbol, timeframe, limit)` `since` almıyor; `outcome_resolver.py:206` `since=ts` gönderiyor; log: 35+ `Could not resolve signal …`; DB: `signal_outcomes=0` |
| B2 | Reconciler mevcut SL'yi görmüyor, yenisini kurmaya çalışıyor | **KRİTİK** | 36× `insufficient balance` (bakiye mevcut `STOP_LOSS_LIMIT` emrinde kilitli); `entries_blocked` kalıcı |
| B3 | Mod/DB geçişinde veri kaybı ve yanlış "yetim" | **KRİTİK** | Eski DB 5 pozisyon/11 emir/192 karar; live DB 0 pozisyon; `reconcile_events` 42 satır |
| B4 | Yetim futures pozisyonlar SL aranmadan market kapatıldı | YÜKSEK | `I1_ORPHAN_FUTURES_CLOSED` ×3 (SOL, ONE); SOL'un borsada SL emri vardı |
| B5 | Tahmini PnL gerçek PnL gibi kaydediliyor | YÜKSEK | SOL `+1.3089` `PHANTOM_POSITION_RESOLVED_CLOSED`; reconciler/time-stop kapanışları `orders` tablosuna satır yazmıyor |
| B6 | Log filtresi httpx kayıtlarını bozuyor | YÜKSEK | `config/log_filter.py`: `tuple(self.mask_text(str(a)) …)` → `%d` hatası; `systemd.log` içinde **704** `TypeError: %d format` ve 15 MB |
| B7 | Eski çöp dersler ve ceza hâlâ DB'de | ORTA | `trade_lessons` 2 satır (biri prompt-echo, ceza 0.06 ve 0.08); `trade_features` boş |
| B8 | Güven eşiği tutarsız | ORTA | Eski DB `min_confidence_threshold=0.71` (tek işlemle yükselmiş); yeni audit "0.68 korunuyor" |
| B9 | Ölü ayarlar ve bağlanmamış modüller | ORTA | Bölüm 5; `market_router.py` hiçbir yerden çağrılmıyor |
| B10 | Otonomi merdiveni etkisiz | ORTA | `autonomy_level=0` iken `TRADING_MODE=live`; `autonomy_log=0` satır |
| B11 | Alarm/olay seli ve log şişmesi | ORTA | Aynı yetim için 36 `reconcile_events` satırı; `quant_bot.log` 13 MB, `systemd.log` 15 MB, rotasyon yok |
| B12 | Eski pozisyonların `market_type`'ı hep `spot` | ORTA | `positions.market_type='spot'` (SOL ve ZEC gerçekte futures'tı) |
| B13 | Zaman çıkışı ilk dağıtımda eski pozisyonları toplu kapattı | DÜŞÜK | BTC/DOGE 25 saatlik pozisyonlar 21:47'de kapandı |
| B14 | Dashboard parola uzunluğu doğrula | DÜŞÜK | Sunucu parolası 15 karakter; Round 2 ≥16 şart koşuyordu; reddedilmedi (doğrula) |

---

# BÖLÜM A — P0

## S1 — `outcome_resolver` düzelt (B1)

**Problem:** `BinanceExchangeWrapper.fetch_ohlcv` imzasında `since` yok; resolver `since=ts` ile çağırıyor → `TypeError`, hiçbir sinyal sonuçlanmıyor. R10/R11/R13'ün tamamı bu tabloya bağlı.

**Yapılacaklar**
1. `engine/exchange.py`: `fetch_ohlcv(self, symbol, timeframe="15m", limit=250, since=None)`; `since` doluysa ccxt'ye ilet. Binance sınırı 1000 mum: uzun aralıkta `since` ilerleterek sayfalama yap (`fetch_ohlcv_range(symbol, tf, start_ms, end_ms)` yardımcısı).
2. `outcome_resolver.py`: çağrıyı bu yardımcıya taşı; hata yakalamayı daralt (bir sembolün hatası diğerlerini durdurmasın), **hatayı sayaçla** (`resolver_errors` state alanı; art arda N hatada Telegram uyarısı).
3. **SL-önce kuralı** ve net R hesabı (R10 spec'i): aynı mumda SL ve TP görülürse SL; `SIGNAL_HORIZON_BARS`, `ROUND_TRIP_FEE_PCT`, `SLIPPAGE_PCT` kullanılsın.
4. Çözülmemiş geçmiş sinyaller için **backfill** komutu: `python -m tools.resolve_backlog --since 7d`.
5. `tools/signal_report.py`: sinyal/rejim/Q/güven kovasına göre n, isabet, ortalama net R.
6. Brier (R11) artık `signal_outcomes`'tan beslensin (yalnızca açılan işlemlerden değil).

**Testler:** `test_resolver_calls_fetch_ohlcv_with_since` (fake exchange imzası gerçeğiyle aynı olmalı: `since` parametresini zorunlu kıl), `test_resolver_continues_after_symbol_error`, `test_resolver_sl_first_same_candle`, `test_brier_uses_signal_outcomes`.
**Kabul:** 24 saat sonra `signal_outcomes` > 0 ve tüm 15 dk'dan eski sinyaller çözülmüş.

## S2 — Reconciler: mevcut korumayı benimse, yetimi önce koru (B2, B4, B12)

**Problem:** Spot BTC/DOGE bakiyesi borsadaki `STOP_LOSS_LIMIT` emrinde kilitliydi. Reconciler mevcut SL'yi bulamadı (veya eşleştirmedi) ve yeni SL kurmaya çalıştı → `insufficient balance` döngüsü, kalıcı `entries_blocked`. Futures yetimler ise SL aranmadan market kapatıldı.

**Önce incele:** `engine/reconciler.py` (I1 yetim işleme, `fetch_open_orders()` kullanımı), `exchange.py` (`fetch_open_orders`, `fetch_futures_order`).

**Yapılacaklar**
1. **Açık emirleri doğru çek:** sembolsüz `fetch_open_orders()` hem `warn`'a hem rate-limit'e takılabilir. Bakiyesi/pozisyonu olan **her sembol için** `fetch_open_orders(symbol)` çağır. Futures algo/stop emirleri için `params={"stop": True}` ile de sorgula. Birleştirip tek liste yap.
2. **Yetim akışı (spot ve futures için aynı sıra):**
   a. Pozisyon için **zaten aktif bir koruyucu emir** var mı (spot: `STOP_LOSS_LIMIT/STOP_LOSS` sell, miktar ≥ %99; futures: `STOP`/`STOP_MARKET` `reduceOnly`/`closePosition`)? Varsa → **pozisyonu DB'ye benimse** (`adopted=true`): giriş fiyatı borsadan (`entryPrice`; spot için son alış dolumu `fetch_my_trades`), SL fiyatı mevcut emirden, `market_type`, `leverage`, `margin`. `ORPHAN_ADOPTED` olayı yaz, **alarm yok** (yalnızca bilgi).
   b. Yoksa → **önce SL kur** (R5 kuralları), başarılıysa benimse.
   c. SL kurulamıyorsa (örn. dust, minNotional altı) → spot'ta **dust** işaretle (`DUST_MAX_USD` altında ve satılamıyorsa: `ORPHAN_DUST_IGNORED`, bir kez bildir, giriş bloklama), futures'ta **market kapat**.
   d. Spot bakiye kilitliyse (`free < amount` ve açık emir var) `insufficient balance` **hata değil bilgi**dir: emri benimse, yeniden kurmayı deneme.
3. **`entries_blocked` yalnızca gerçekten çözülemeyen ihlalde** açık kalsın; benimsenen/dust yetimler engellemesin. Her ihlalin `resolved` bayrağı güncellensin.
4. **Alarm/olay tekrarını kes:** aynı (invariant, sembol, durum) için `reconcile_events`'e **upsert** (`first_seen`, `last_seen`, `count`); Telegram/CRITICAL aynı olay için en çok `ALERT_REPEAT_MIN` (varsayılan 30 dk) dakikada bir.
5. Reconciler her turda `positions` ↔ borsa eşleşme özetini `system_state.reconcile_summary` olarak yazsın (benimsenen, dust, ihlal sayıları).

**Testler (`tests/test_reconciler_adoption.py`)**
- `test_orphan_spot_with_existing_sl_is_adopted_no_critical`: borsada BTC bakiyesi + açık `STOP_LOSS_LIMIT` sell, DB boş → pozisyon benimsenir, yeni emir gönderilmez, `entries_blocked=false`.
- `test_orphan_futures_with_existing_algo_sl_is_adopted_not_closed`.
- `test_orphan_without_sl_gets_sl_before_any_close`.
- `test_dust_below_min_notional_is_ignored_once`.
- `test_events_are_upserted_and_alerts_throttled`.
- `test_open_orders_fetched_per_symbol_not_global`.
**Kabul:** Bölüm 1.2'deki senaryo (eski DB'deki pozisyonlar + yeni boş DB) bot açılışında **hiçbir CRITICAL ve hiçbir otomatik kapatma olmadan** benimsenir.

## S3 — Log filtresi (B6)

**Problem:** `SensitiveDataFilter.filter` tüm `record.args` elemanlarını `str()` yapıyor. `httpx`'in `%d` içeren log formatı `TypeError` veriyor; 704 "Logging error" ve dolu `systemd.log`; ilgili log satırları da kayboluyor.

**Düzelt:** Yalnızca `str` argümanları maskele, diğer tipleri aynen bırak; dict ve tuple için ayrı dallar; `record.getMessage()` hatasız çalışmalı. `msg` içindeki `%` kalıplarına dokunma.

```python
# config/log_filter.py
def _mask_arg(self, a):
    return self.mask_text(a) if isinstance(a, str) else a

def filter(self, record):
    if isinstance(record.msg, str):
        record.msg = self.mask_text(record.msg)
    if isinstance(record.args, dict):
        record.args = {k: self._mask_arg(v) for k, v in record.args.items()}
    elif isinstance(record.args, tuple):
        record.args = tuple(self._mask_arg(a) for a in record.args)
    return True
```
**Testler:** `test_filter_keeps_int_args` (`logger.info('HTTP %s "%d %s"', 'GET', 200, 'OK')` hata vermez, mesaj doğru biçimlenir), `test_filter_masks_token_in_str_args`, `test_no_logging_error_on_httpx_style_record`.
**Kabul:** 1 saatlik çalışmada `systemd.log` içinde `Logging error` = 0.

## S4 — Mod/DB geçişi, migration ve açılış güvenliği (B3, B12, B13)

**Problem:** Paper→live geçişinde yeni DB boş açıldı; eski DB'deki gerçek pozisyonlar yetim sayıldı. Hangi DB'nin aktif olduğu belirsiz. Eski kayıtların `market_type`'ı yanlış.

**Yapılacaklar**
1. **Açılışta net bildirim:** log + Telegram: `MODE`, `DB_PATH` (dosya adı), `autonomy_level`, `entries_blocked`, açık pozisyon sayısı (DB ve borsa).
2. **Legacy DB tespiti:** `data/quant_bot.db` varsa ve içinde açık pozisyon/bekleyen emir bulunuyorsa **otomatik başlama**, `LEGACY_DB_DETECTED` uyarısı ve `scripts/migrate_legacy_db.py` talimatı.
3. `scripts/migrate_legacy_db.py` (kullanıcı çalıştırır; bot kapalıyken): `quant_bot.db` → `quant_bot_live.db` tablolarını kopyalar (`positions`, `orders`, `trade_lessons`, `reflex_decisions`, `brier_calibration`, `system_state` seçili anahtarlar), **önce yedek** alır, tekrar çalıştırılabilir (idempotent), sonunda satır sayılarını raporlar.
4. **`market_type` backfill:** `positions.market_type` için borsa geçmişinden/emirlerden çıkar: sembol futures pozisyon listesindeyse veya emir `STOP`/`reduceOnly` ise `futures`; belirsizse `unknown` (reconciler benimserken borsadan düzeltsin). `leverage`, `margin_usd`, `notional_usd` NULL kalmasın.
4. `scripts/switch_mode.py paper|live`: mod değişimi öncesi açık pozisyon/emir yoksa izin verir, varsa durur ve listeler (R1'deki mod kilidi ile birleşik).
5. **Zaman çıkışı için geçiş payı:** yeni çıkış kuralı ilk dağıtımda mevcut pozisyonları toplu kapatmasın. `LEGACY_POSITION_GRACE_HOURS` (varsayılan 24): kural yürürlüğe girdiği andan itibaren o kadar süre eski pozisyonlara uygulanmaz ve kapatma **kararı Telegram'a önceden bildirilir**.
6. Tek `DB_PATH` kaynağı: `.env`'de `DB_PATH` boşsa mod→dosya eşlemesi `Settings` içinde tek yerde; başka hiçbir yer DB dosya adı üretmesin (`grep` testi).

**Testler:** `test_legacy_db_with_open_positions_blocks_start`, `test_migration_is_idempotent_and_backs_up`, `test_market_type_backfill`, `test_single_db_path_source`, `test_time_stop_grace_for_legacy_positions`.

## S5 — Gerçek PnL, tüm çıkışlar için emir kaydı (B5)

**Problem:** Reconciler/zaman çıkışı/yetim kapatma işlemleri `orders` tablosuna satır yazmıyor; "phantom" kapanışta PnL **güncel fiyattan tahmin** ediliyor (SOL +1.3089) ve istatistiğe karışıyor.

**Yapılacaklar**
1. **Her** çıkış (SL, TP, zaman çıkışı, panic, yetim kapatma, phantom çözümü) `orders` tablosuna `sell/market` satırı olarak yazılsın: gerçek `average` dolum fiyatı, `filled`, ücret.
2. Kapanış PnL'i **borsadaki gerçek dolumlardan** hesaplansın: `fetch_my_trades(symbol, since=opened_at)` / `fetch_order` ile ortalama çıkış fiyatı + komisyon. Mümkün değilse `pnl_estimated=true` işaretle ve nedenini yaz.
3. `pnl_estimated=true` olan işlemler **Brier, öğrenme, strateji istatistiği ve günlük PnL limitinden hariç** tutulsun (ayrı raporda gösterilsin).
4. `PHANTOM_POSITION_RESOLVED_CLOSED` yalnızca borsada gerçekten pozisyon yoksa ve son çıkış dolumu bulunamıyorsa kullanılsın; bulunabiliyorsa gerçek dolumla kapat.
5. Mevcut SOL kaydı için migration: `pnl_estimated=true`, mümkünse borsa geçmişiyle düzelt.

**Testler:** `test_every_exit_writes_order_row`, `test_pnl_uses_actual_fills`, `test_estimated_pnl_excluded_from_stats`.

---

# BÖLÜM B — P1/P2

## S6 — Öğrenme verisini temizle ve giriş-anı özelliklerini doğrula (B7, B8)

1. **Migration:** `trade_lessons`'ta `validated=false` olmayanları işaretle; prompt-echo içeren kaydın (`"The user wants a 1-sentence…"`) cezasını sıfırla; **toplam ceza yalnızca `validated=true` ve `n ≥ MIN_LESSONS_FOR_PENALTY` desenlerden** hesaplansın (bu ayar şu an kodda kullanılmıyor, bağla).
2. **`trade_features`** tablosu boş: yeni pozisyon açılışında satır yazıldığını doğrula (R12). Test: pozisyon aç → `trade_features` 1 satır (rsi, adx, atr_pct, bb_dist, ob_imbalance, regime, strategy_id, side, leverage).
3. **Güven eşiği tek kaynak:** `system_state.min_confidence_threshold` yalnızca `calibration_log` kaydı olan bir değişiklikle güncellensin. Eski DB'deki 0.71 migration'da **taşınmasın** (n=1 örnekten gelmişti); `INSUFFICIENT_DATA` iken baz değer (`MIN_CONFIDENCE_THRESHOLD`) geçerli. Açılışta `system_state` ile `Settings` farkı varsa logla.
4. `/dersler` ve `/status` aktif eşiği ve ceza toplamını göstersin.

**Testler:** `test_invalid_lessons_give_no_penalty`, `test_trade_features_written_on_open`, `test_threshold_single_source_and_logged`.

## S7 — Ölü ayarlar ve bağlanmamış modüller (B9)

Aşağıdaki ayarlar `Settings`'te tanımlı ama **kodda hiç kullanılmıyor** (OpenRouter anahtarları ortam değişkeninden okunuyorsa istisna, doğrula):
`SL_LIMIT_BUFFER_PCT`, `SL_ESCALATION_SEC`, `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED`, `MIN_LESSONS_FOR_PENALTY`, `CLOCK_WARN_MS`, `CLOCK_BLOCK_MS`, `FUTURES_MIN_LEVEL`, `SAFE_MODE_ENABLED`, `WATCHDOG_TIMEOUT_SEC`, `PAPER_PARTIAL_FILL_PROB`, `FUTURES_MARGIN_USD` (eski sabit-margin mantığı, kaldır).

Her biri için **ya bağla ya sil**:
- **R16:** `SL_LIMIT_BUFFER_PCT` (limit = stop × (1 − tampon)), `SL_ESCALATION_SEC` (tetiklendi ama dolmadı ve fiyat limitin altındaysa market).
- **R18:** `CLOCK_WARN_MS` (uyarı), `CLOCK_BLOCK_MS` (giriş bloklama); `SAFE_MODE_ENABLED` / `WATCHDOG_TIMEOUT_SEC` (heartbeat zaman aşımı → güvenli mod).
- **R5:** `FALLBACK_TO_SPOT_WHEN_FUTURES_EXHAUSTED` (futures→spot yedek yolu bayrağa bağlı ve risk kapısından geçsin).
- **R17:** `FUTURES_MIN_LEVEL` (S8'de).
- **`engine/market_router.py` hiçbir yerden çağrılmıyor:** `execution`/`main` akışına bağla (R15); router kararını `reflex_decisions.market_type/router_reason` alanlarına yaz.
- **Gerçek `test_no_dead_settings`** yaz: `Settings` alanlarını tarar, `config/settings.py` dışında en az bir referansı olmayanı **başarısız** sayar (izin listesi: ortam değişkeninden okunanlar, gerekçesiyle).

## S8 — Otonomi merdiveni gerçekten uygulansın (B10)

`autonomy_level=0` iken `TRADING_MODE=live` çalışıyor, `autonomy_log` boş. Netleştir:
1. **Sözleşme:** L0 = yalnızca paper. `TRADING_MODE=live` ve seviye 0 ise **başlamayı reddet** ya da açıkça `AUTONOMY_START_LEVEL=1` iste (ilk canlı geçiş elle). Seviye değişimleri `autonomy_log`'a ve Telegram'a yazılır.
2. **Uygulama noktaları:** `execution`/`risk_engine` seviyeye göre kaldıraç tavanı (`LEVERAGE_CAP_BY_LEVEL`), `FUTURES_MIN_LEVEL`, risk çarpanı ve `MAX_OPEN_POSITIONS` uygular.
3. **Terfi/düşüş işi** (günlük): R17 tablosundaki metrikler (`signal_outcomes` n, net R güven aralığı, drawdown, reconcile ihlali, anomali sayısı). **Veri yoksa terfi yok.** Düşüş: drawdown/ihlal/anomali → bir seviye düş veya `safe_mode`.
4. `/durum` seviyeyi ve bir sonraki terfi için eksik koşulları göstersin.

**Testler:** `test_live_mode_refuses_level0`, `test_no_promotion_without_data`, `test_demotion_on_drawdown`, `test_leverage_capped_by_level`.

## S9 — Gözlemlenebilirlik ve log yönetimi (B11)

- Log rotasyonu: `RotatingFileHandler` (örn. 20 MB × 5) veya sunucuda `logrotate` örneği (`deploy/logrotate.quant-bot.example`); `systemd.log` için ayrı kural.
- Gürültü azaltma: `apscheduler` ve `httpx` INFO'larını WARNING'e çek (config'li).
- `/saglik` komutu: mod, DB, seviye, `reconcile_ok`, son reconcile, `resolver_errors`, saat kayması, bellek/swap, bekleyen yetim/ihlal sayısı.
- Günlük broker raporu (R18) bu verileri kullanır; ilk sürümde Telegram yeterli.

## S10 — Round 2'den devreden işler (doğrulama + tamamlama)

Aşağıdakileri kodla **tek tek doğrula**, eksikse tamamla; sonuç tablosu (yapıldı/kısmen/yok) çıktının sonuna eklensin:

| Görev | Kontrol |
|---|---|
| R3 | `MAX_RISK_PER_TRADE_PCT`, `MAX_TOTAL_OPEN_RISK_PCT`, küçük hesap modu, likidasyon tamponu ve margin tavanı kodda ve testte var mı? Brüt maruziyet yeni işlemi sayıyor mu? |
| R5 | Kısmi dolumda SL ≤5 sn; kapatma sırası (satış doğrulanmadan DB kapanmıyor); `TaskRegistry` SIGTERM'de gerçekten bekliyor mu? |
| R6 | Panic futures'ı `reduceOnly` kapatıyor mu? |
| R7 | Dashboard: zayıf/kısa parola reddi (≥16), hız sınırı, yazma uç noktaları için ek onay; kodda sabit IP kalmamış mı (`grep`)? |
| R11 | `MIN_BRIER_SAMPLES`, N/A için `NULL` (0.0 değil), güvenilirlik tablosu |
| R13/R14 | Strateji kayıt defteri, `BB_TOUCH_TOL`, `MOMENTUM_MIN_ATR_FRAC`, `BLOCK_LONGS_BELOW_BTC_EMA200` var mı? |
| R19 | `scripts/deploy.sh` açık emir/pozisyon kontrolü; CI (`pytest`, `ruff`); `.env.example` güncel mi? |

---

# BÖLÜM C — Sunucu tarafı kontrol listesi (kullanıcı için, ajan yapmaz)

1. **Aktif DB ve mod:** `ls -la data/`, `sudo systemctl show quant-bot -p Environment`, `sudo lsof data/*.db`. Açılış logunda hangi DB yazıldığına bak.
2. **Borsa gerçeği:** Binance'te spot bakiyeler, açık emirler (spot ve futures algo), futures pozisyonlar. Beklenen: korumasız pozisyon yok.
3. **Anahtar hijyeni:** Yedek arşivi `.env` ve `*.db` içeriyordu. Arşiv bir üçüncü tarafa yüklendiyse Binance, OpenRouter ve Telegram anahtarlarını **yenile** (API'de withdraw kapalı + IP kısıtı açık). Yedekleri `tar --exclude=.env --exclude='*.db'` ile al, veritabanını ayrı ve şifreli sakla.
4. **Log rotasyonu:** `systemd.log` (15 MB) ve `quant_bot.log` (13 MB) için `logrotate` kur.
5. **Dashboard:** Parola ≥16 karakter, `admin` yerine farklı kullanıcı adı; erişim SSH tüneli ile.
6. **Dağıtım:** Yalnızca açık/bekleyen emir yokken (`scripts/deploy.sh`).

---

# 8. Zorunlu regresyon testleri (özet)

```python
# S1: resolver, gerçek imzayla aynı olan fake exchange'te çalışmalı
class FakeEx:
    async def fetch_ohlcv(self, symbol, timeframe="15m", limit=250, since=None):  # gerçek imza
        return CANDLES
async def test_resolver_resolves_with_since():
    n = await resolver.resolve_pending(FakeEx())
    assert n > 0 and db.count("signal_outcomes") > 0           # ŞU AN: TypeError, 0 satır

# S3: httpx tarzı kayıt hata vermemeli
def test_filter_keeps_int_args(caplog):
    logger.addFilter(SensitiveDataFilter())
    logger.info('HTTP Request: %s %s "%s %d %s"', 'GET', 'https://x', 'HTTP/1.1', 200, 'OK')
    assert 'Logging error' not in caplog.text and '200 OK' in caplog.text   # ŞU AN: TypeError

# S2: mevcut SL ile yetim spot pozisyon benimsenmeli, alarm ve yeni emir olmamalı
async def test_orphan_spot_with_existing_sl_is_adopted():
    ex.set_balance("BTC", 0.00015249)
    ex.add_open_order(symbol="BTC/USDT", type="STOP_LOSS_LIMIT", side="sell", amount=0.00015249, stopPrice=72949.68)
    await reconciler.reconcile_cycle()
    assert db.open_positions()[0].symbol == "BTC/USDT" and db.open_positions()[0].adopted
    assert ex.created_orders == [] and not critical_logged and db.get_state("entries_blocked") == "false"
```

Ek test listesi (özet): `test_orphan_futures_with_existing_algo_sl_is_adopted_not_closed`, `test_orphan_without_sl_gets_sl_before_any_close`, `test_dust_below_min_notional_is_ignored_once`, `test_events_are_upserted_and_alerts_throttled`, `test_open_orders_fetched_per_symbol_not_global`, `test_legacy_db_with_open_positions_blocks_start`, `test_migration_is_idempotent_and_backs_up`, `test_market_type_backfill`, `test_single_db_path_source`, `test_time_stop_grace_for_legacy_positions`, `test_every_exit_writes_order_row`, `test_pnl_uses_actual_fills`, `test_estimated_pnl_excluded_from_stats`, `test_invalid_lessons_give_no_penalty`, `test_trade_features_written_on_open`, `test_threshold_single_source_and_logged`, `test_no_dead_settings`, `test_live_mode_refuses_level0`, `test_no_promotion_without_data`, `test_demotion_on_drawdown`, `test_leverage_capped_by_level`, `test_market_router_is_called_from_execution`.

---

# 9. Uygulama sırası

| Faz | Görevler | Çıkış kriteri |
|---|---|---|
| **F1 (P0)** | S3 (hızlı), S1, S2, S4, S5 | 3 regresyon testi yeşil; Bölüm 1.2 senaryosu CRITICAL'sız çözülüyor; `signal_outcomes` doluyor |
| **F2** | S6, S7 | Ölü ayar yok; öğrenme verisi temiz; router bağlı |
| **F3** | S8, S9, S10 | Otonomi seviyesi uygulanıyor; log rotasyonu ve `/saglik`; Round 2 devir tablosu |

**Her faz sonunda dur:** değişen dosyalar, eklenen testler, `pytest -q` çıktısı, `TODO(user)` listesi; onay bekle.

---

# 10. Canlıda devam kriterleri (güncel)

1. `pytest -q` yeşil; Bölüm 8 testleri dahil.
2. Açılışta `LEGACY_DB_DETECTED` yok, mod/DB/seviye bildirimi geliyor.
3. 7 gün boyunca `reconcile` ihlali = 0, `Logging error` = 0, korumasız pozisyon = 0.
4. `signal_outcomes` n ≥ 100 ve net beklenen R güven aralığı alt sınırı ≥ 0 olmadan **otonomi seviyesi yükselmez**.
5. Tüm kapanışlar gerçek dolumla hesaplanmış (`pnl_estimated=false`).

---

# 11. Operatöre sorulacak açık noktalar (ajan çıktısının sonuna ekle)

1. Şu an hangi DB aktif (`quant_bot.db` mi `quant_bot_live.db` mi)? Eski DB'deki geçmiş `quant_bot_live.db`'ye taşınsın mı? (S4)
2. Borsada şu an açık pozisyon/emir var mı? (S2 testleri için gerçek örnek)
3. Yetim futures pozisyonlar için varsayılan: önce SL kur (önerilen). Kapatmayı tercih ediyor musun? (S2)
4. Otonomi başlangıç seviyesi canlıda 1 mi olsun? (S8)
5. Gerçek PnL için Binance işlem geçmişi (trade history) API izni mevcut mu (okuma yetkisi)? (S5)

---

# 12. Antigravity'ye yapıştırılacak başlangıç istemi

```
Repodaki FIX_PLAN_ROUND3.md dosyasını baştan sona oku ve Bölüm 0 kurallarına uy.
Yeni branch aç: fix/round3. Önce Bölüm 8'deki üç regresyon testini yaz ve kırmızı olduklarını göster
(resolver 'since' hatası, log filtresi %d hatası, yetim spot pozisyonda mevcut SL'nin benimsenmemesi).
Sonra F1 fazını uygula: S3, S1, S2, S4, S5 (sırayla). Her görev ayrı commit.
Canlı servise, sunucudaki .env'e, data/*.db dosyalarına ve gerçek emirlere ASLA dokunma; testlerde fake exchange kullan.
F1 bitince pytest -q çıktısını ve değişiklik özetini ver, TODO(user) listesini yaz ve onay bekle.
```
