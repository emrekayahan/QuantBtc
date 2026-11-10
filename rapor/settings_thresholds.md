# QuantBot Live v3.1 — Risk, Strateji ve Eşik Parametreleri

> **Güvenlik Notu:** Bu raporda API anahtarları, şifreler veya gizli token'lar filtrelenmiş olup yalnızca operasyonel ve sayısal eşik değerleri yer almaktadır.

---

## 1. Zaman Dilimi ve İzleme Listesi
| Parametre | Değer | Açıklama |
| :--- | :--- | :--- |
| `TIMEFRAME` | `15m` | Mum grafiği ve gösterge hesaplama periyodu (15 Dakika) |
| `TOP_WATCHLIST_SIZE` | `20` | Takip edilen en yüksek hacimli onaylı parite sayısı |
| `MIN_24H_VOLUME_USD` | `$10,000,000` | İzleme listesine dahil edilmek için gereken min. 24s hacim |
| `MAX_SPREAD_PCT` | `%0.20` | Parite kabulünde izin verilen maksimum alış/satış makası |

---

## 2. Karar & Kalite Eşikleri
| Parametre | Değer | Açıklama |
| :--- | :--- | :--- |
| `MIN_SETUP_QUALITY` | `2` (Q >= 2) | En az 2 kural onayı (RSI, Bollinger, Mum Formasyonu / Rejim) |
| `MIN_CONFIDENCE_THRESHOLD` | `%68` (`0.68`) | Brier kalibrasyonlu minimum giriş güven skoru |
| `ROUND_TRIP_FEE_PCT` | `%0.20` | Giriş-çıkış komisyon ve kayma rezervi |

---

## 3. Sermaye Yönetimi ve Cüzdan Eşikleri
| Parametre | Değer | Açıklama |
| :--- | :--- | :--- |
| `MAX_OPEN_POSITIONS` | `5` | Eşzamanlı izin verilen maksimum açık pozisyon adedi |
| `MAX_POSITION_USD` | `$25.00` | Tek bir pozisyona ayrılabilecek maksimum teorik büyüklük |
| `SPOT_MIN_POSITION_USD` | `$11.50` | Binance Spot $5-$10 minNotional filtresini aşmak için ayrılan min. büyüklük |
| `FUTURES_MIN_USDT` | `$3.00` | Futures modunun aktif kalması için cüzdandaki min. serbest bakiye eşiği |
| `FUTURES_MARGIN_USD` | `$4.50` | Vadeli işlem başına yatırılan izole teminat (margin) |
| `FUTURES_DEFAULT_LEVERAGE` | `20x` (ISOLATED) | USD-M Vadeli İşlemler varsayılan izole kaldıraç çarpanı |

---

## 4. Sert Risk ve Güvenlik Limitleri
| Parametre | Değer | Açıklama |
| :--- | :--- | :--- |
| `DAILY_MAX_TRADES` | `20` | Bir UTC gününde açılabilecek maksimum işlem kotası |
| `DAILY_MAX_LOSS_USD` | `$5.00` | Günlük gerçekleşen toplam zarar bu eşiğe ulaşırsa sistem durdurulur |
| `MAX_DRAWDOWN_PCT` | `%15.00` | Zirve kasadan itibaren izin verilen maksimum tepe-dip kayıp yüzdesi |
