# Quant-Bot-Live v3.0 — Yeni Sunucuya Taşıma ve Kurulum Rehberi

Bu rehber, **Quant-Bot-Live v3.0** projesinin yeni bir Ubuntu sunucuya (VM) sıfırdan veya yedek paketinden nasıl kurulacağını adım adım açıklar.

---

## 1. Yeni Sunucu (VM) Oluştururken Dikkat Edilecekler

Binance Global (`api.binance.com`), **ABD (`us-*`)** veri merkezlerinden gelen özel API (bakiye okuma ve emir gönderme) isteklerini **HTTP 451** hatası ile engellemektedir. Bu nedenle yeni sunucunuzu açarken:

1. **Bölge (Region) Seçimi:**
   - **Avrupa (Önerilen - Düşük Gecikme):** `europe-west3` (Frankfurt), `europe-west1` (Belçika), `europe-west4` (Hollanda), `europe-west12` (Milano/Torino)
   - **Asya (Önerilen - Binance Eşleşme Motoruna Yakın):** `asia-northeast1` (Tokyo), `asia-southeast1` (Singapur)
   - ⚠️ **Kesinlikle `us-central1`, `us-east1`, `us-west1` gibi ABD bölgeleri seçmeyin.**
2. **Donanım ve İşletim Sistemi:**
   - **İşletim Sistemi:** Ubuntu 22.04 LTS veya Ubuntu 24.04 LTS (x86_64)
   - **RAM:** En az **2 GB RAM** (1 GB RAM'li sunucularda en az 2 GB Swap alanı açılması önerilir).

---

## 2. GitHub Üzerinden Yeni Sunucuya Kurulum (Önerilen)

### Adım 1: Yeni Sunucuya Bağlanın ve Depoyu Klonlayın

```bash
ssh <KULLANICI_ADI>@<YENI_SUNUCU_IP>
git clone https://github.com/emrekayahan/QuantBtc.git ~/btc
cd ~/btc/quant-bot-live
```

### Adım 2: `.env` Yapılandırma Dosyasını Oluşturun

```bash
cp .env.example .env
nano .env
```
`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `OPENROUTER_API_KEY_1`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` ve `DASHBOARD_PASSWORD` alanlarını kendi bilgilerinizle doldurup kaydedin (`Ctrl+O`, `Enter`, `Ctrl+X`).

### Adım 3: Tek Komutla Otomatik Kurulumu Başlatın

`install_ubuntu.sh` betiği; NTP saat senkronizasyonunu (`chrony`), Python 3.12 sanal ortamını (`.venv`), bağımlılıkları, birim testlerini (`pytest`), Binance/OpenRouter ön kontrollerini (`preflight_check.py`) ve `quant-bot.service` Systemd servisini otomatik olarak kurup başlatır:

```bash
chmod +x install_ubuntu.sh
bash install_ubuntu.sh
```

---

## 3. Yerel Yedek Arşivi (`btc_migration_bundle.tar.gz`) ile Taşıma

Eğer yerel bilgisayarınızdaki hazır `.tar.gz` yedek paketini kullanarak taşımak isterseniz:

```powershell
# 1. Bilgisayarınızdan yeni sunucuya arşiv dosyasını gönderin:
scp btc_migration_bundle.tar.gz <YENI_SUNUCU_SSH_ADI>:~/

# 2. Sunucuya bağlanıp arşivi açın ve kurulum betiğini çalıştırın:
ssh <YENI_SUNUCU_SSH_ADI> "mkdir -p ~/btc && tar -xzf ~/btc_migration_bundle.tar.gz -C ~/btc && cd ~/btc/quant-bot-live && chmod +x install_ubuntu.sh && bash install_ubuntu.sh"
```

---

## 4. Web Dashboard Erişimi ve Yönetim Komutları

### Bilgisayarınızdan Web Dashboard'a Bağlanma (`Port 8000`)
Sunucuda `8000` portunu dış internete açmak zorunda kalmadan, bilgisayarınızda SSH tüneli açarak güvenli şekilde bağlanabilirsiniz:

```powershell
ssh -L 8000:127.0.0.1:8000 <YENI_SUNUCU_SSH_ADI>
```
Ardından tarayıcınızda **`http://localhost:8000`** adresini açın:
- **Kullanıcı Adı:** `.env` dosyanızdaki `DASHBOARD_USERNAME` (varsayılan: `admin`)
- **Şifre:** `.env` dosyanızdaki `DASHBOARD_PASSWORD`

### Faydalı Servis ve Log Komutları

| İşlem | Komut |
| :--- | :--- |
| **Servis Durumunu Görme** | `sudo systemctl status quant-bot.service` |
| **Canlı Bot Loglarını İzleme** | `tail -f ~/btc/quant-bot-live/logs/quant_bot.log` |
| **Servisi Yeniden Başlatma** | `sudo systemctl restart quant-bot.service` |
| **Servisi Durdurma** | `sudo systemctl stop quant-bot.service` |
| **Pre-Flight Testini Tekrar Çalıştırma** | `sudo systemctl stop quant-bot.service && cd ~/btc/quant-bot-live && ./.venv/bin/python preflight_check.py && sudo systemctl start quant-bot.service` |
