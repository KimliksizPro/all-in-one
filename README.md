# all-in-one

Linux için **Antigravity / Gemini hesap ve kota merkezi**. Birden fazla Google hesabını tek tek kaydeder, OAuth oturumları arasında keyring seviyesinde geçiş yapar, gerçek Antigravity kotalarını okur ve kota azaldığında uyarır.

---

## Ne yapar?

- **10+ hesap**: Her Google hesabı ayrı bir profil olarak saklanır.
- **Tek tıkla geçiş**: Antigravity'yi kapatıp yeniden açarak hesap değiştirir; logout/login akışına gerek kalmaz.
- **Gerçek kota**: Antigravity'nin kendi `language_server` RPC'sinden model bazlı kalan kota, sıfırlanma zamanı, plan ve kredi bilgisi okunur.
- **Uyarı**: Eşik altına düşen hesaplar için web arayüzünde banner, masaüstünde `notify-send` bildirimi.
- **Ana hesap koruması**: İlk hesap `main` olarak işaretlenir; silinemez, yanlışlıkla değiştirilmez.
- **Skills/MCP/settings korunur**: Geçişte yalnızca OAuth keyring kaydı ve Antigravity'nin son giriş etiketi değişir. MCP sunucuları, skills, `settings.json`, projeler ve konuşmalar aynı kalır.

---

## Nasıl çalışır?

### Kimlik doğrulama

Antigravity, OAuth token'ını Linux'ta **Secret Service** (KDE Wallet / GNOME Keyring) içinde şu niteliklerle tutar:

```text
service  = gemini
username = antigravity
```

`all-in-one` bu kaydı `secret-tool` ile okur. Her profil ayrı bir kasada saklanır:

```text
service  = aio-antigravity
username = <profil-adı>
```

Profil geçişinde yalnızca **canlı** `gemini/antigravity` kaydı hedef profilin token'ı ile değiştirilir. Antigravity kapalıyken yapılan bu işlem, uygulama açıldığında hedef hesabın oturumunu getirir.

### Kota

Antigravity'nin kota/plan bilgisi `GetUserStatus` RPC'sinden gelir:

```text
/exa.language_server_pb.LanguageServerService/GetUserStatus
```

Bu RPC, model bazlı `quotaInfo.remainingFraction` ve `resetTime` alanlarını döndürür. `all-in-one`:

- Antigravity açıksa **çalışan** language_server'a bağlanır (hızlı yol).
- Antigravity kapalıysa binary'yi **izole bir HOME** ile headless çalıştırır; böylece `~/.gemini/config` altındaki skills/MCP/settings dosyalarına dokunmaz.
- Birden fazla hesabın kotasını okurken keyring'i sırayla değiştirir ve **her seferinde eski canlı token'ı geri yazar**.

Doğrudan Cloud Code REST endpoint'i bu hesap türünde `403 SUBSCRIPTION_REQUIRED` dönebildiği için kota kaynağı olarak kullanılmaz.

---

## Gereksinimler

| Gereksinim | Not |
|---|---|
| Linux | KDE veya GNOME masaüstü |
| Python | 3.10+ |
| Antigravity | `/opt/Antigravity` kurulumu |
| Secret Service | KDE Wallet / GNOME Keyring, **kilidi açık** olmalı |
| `secret-tool` | `libsecret` paketi |
| `notify-send` | Masaüstü bildirimleri için (opsiyonel) |

Arch / CachyOS:

```bash
sudo pacman -S libsecret libnotify
```

Debian / Ubuntu:

```bash
sudo apt install libsecret-tools libnotify-bin
```

KDE Wallet kapalıysa veya kilitliyse `aio doctor` bunu bildirir.

---

## Kurulum

```bash
git clone https://github.com/voidvenom/all-in-one.git
cd all-in-one

python3 -m venv .venv
.venv/bin/pip install -e .
```

Kısayol:

```bash
# Bash / zsh
source .venv/bin/activate
aio --help

# Fish (POSIX activate dosyası fish'te hata verir)
source .venv/bin/activate.fish
aio --help

# Aktivasyonla uğraşmadan, her shell'den
./bin/aio --help
```

Kurulumu doğrula:

```bash
aio doctor
```

---

## İlk çalıştırma: ana hesap

Antigravity'de zaten giriş yaptığın hesap **ana hesap** olarak alınır:

```bash
aio init
```

Bu komut:

1. Canlı keyring'deki hesabı okur.
2. `main` profilini oluşturur, `Ana hesap` etiketiyle **primary** yapar.
3. Canlı keyring'i **değiştirmez**; mevcut oturum aynen kalır.

Web arayüzü:

```bash
aio ui
```

Varsayılan adres: `http://127.0.0.1:8765`

> İstersen `aio ui --port 9000 --no-browser` ile portu değiştirebilirsin. Sunucu yalnızca `127.0.0.1` dinler.

---

## 10 hesabı ekleme

Yeni hesapları iki yolla ekleyebilirsin. **Önerilen yol Antigravity'nin kendi giriş akışıdır**; çıkış yapmak gerekmez.

### 1) Antigravity ile giriş (önerilen)

Yeni hesap ekleme akışı için Antigravity ile uyumlu OAuth uygulama bilgileri yerelde tanımlanmalıdır. Bu değerler kaynak kodunda veya Python paketinde dağıtılmaz. Varsayılan dosya `~/.all-in-one/oauth-client.local.json`; `AIO_HOME` kullanılıyorsa aynı dosyayı o veri dizinine koyun:

```json
{
  "client_id": "<OAuth uygulama istemci kimliği>",
  "client_secret": "<OAuth uygulama istemci sırrı>"
}
```

Gerçek değerleri yalnızca özel dosyanıza yazın; dosyayı `chmod 600` ile koruyun ve GitHub'a göndermeyin. Uyumlu uygulama bilgileri yoksa aşağıdaki **elle yakalama** yolu kullanılabilir. Eksik/geçersiz ayarda giriş, tarayıcı açılmadan açıklayıcı hatayla durur.

1. Ana hesabınla giriş yap ve `aio init` çalıştır.
2. Web arayüzünde **+ Hesap ekle → Antigravity ile giriş yap** veya terminalde:

```bash
aio login
```

3. Antigravity'nin Google giriş ekranı/tarayıcısı açılır. Eklemek istediğin hesabı seç.
4. Giriş tamamlanınca canlı token otomatik yakalanır; profil adı e-posta yerel kısmından üretilir (ör. `hesap1`). UI'dan **Yeniden adlandır** ile değiştirebilirsin.

Notlar:

- Varsayılan Google hesabı otomatik seçilirse tarayıcıda **“Başka hesap kullan”** seçeneğini kullan.
- Aynı hesap seçilirse sistem bunu bildirir ve yeni profil oluşturmaz.
- Google kimlik bilgileri bu araç tarafından toplanmaz; giriş Antigravity'nin kendi OAuth akışında yapılır.
- Mevcut canlı hesap, giriş başlamadan önce kasaya yazılır; ana hesap `main` korunur.

### 2) Elle yakalama (alternatif)

Antigravity'de bir hesap açıkken:

```bash
aio accounts capture hesap2 --label "Hesap 2"
```

Web arayüzünde **Canlı hesabı yakala** paneline adı yazıp **Yakala** da kullanılabilir.

### Önemli uyarı

**Antigravity ile giriş** akışı çıkış gerektirmez; mevcut token kasada kalır. Yalnızca elle hesap değiştirirken Antigravity'de **çıkış yapmak**, o hesabın refresh token'ını sunucuda iptal edebilir. `all-in-one` bunu engelleyemez. Bir profil daha sonra "Giriş gerekli" gösterirse:

1. O profile geç.
2. Antigravity'de yeniden giriş yap.
3. `aio accounts capture <ad> --overwrite` ile token'ı tazele.

Ana hesap `main` **silinemez**; aktif hesap da silinemez. Başka hesaba geçtikten sonra gereksiz profilleri silebilirsin.

---

## Hesap geçişi

Antigravity token'ı açılışta okur; bu yüzden geçişten sonra uygulamanın yeniden başlatılması gerekir.

```bash
# Antigravity'yi kapat, geç, yeniden aç
aio use hesap1 --restart

# Uygulama zaten kapalıysa
aio use hesap1

# Kapatma/açma kararını sen ver
aio use hesap1 --stop --no-launch
```

Web arayüzünde:

- **Geç** düğmesi Antigravity açıksa kapatılacağını sorar.
- Geçişten sonra uygulama, ayarlardaki **"Geçişten sonra Antigravity'yi aç"** seçeneğine göre başlatılır.
- Geçiş öncesi hedef token doğrulanır. Doğrulama başarısız olursa canlı keyring eski hâline geri alınır.

Geçiş sırasında değişen tek şey:

- `service=gemini/username=antigravity` keyring kaydı
- `~/.config/Antigravity/app_storage.json` içindeki `jetski.onboarding.lastLoginUsername`

Değişmeyenler:

- `~/.gemini/settings.json`
- `~/.gemini/config/` (MCP’ler, pluginler, skills)
- `~/.gemini/antigravity/` (konuşmalar, brain, projeler)
- Antigravity binary/ayarları

---

## Kota okuma

```bash
# Önbellekteki kotaları göster
aio quota

# Tüm hesapları canlı oku (Antigravity kapalı olmalı)
aio quota --refresh

# Tek hesabı canlı oku
aio quota hesap1 --refresh

# JSON çıktısı
aio quota --json
```

Kota tablosunda her hesap için:

- Plan (Free / Pro / Ultra)
- En düşük kalan yüzde
- Her model/varyant için ayrı kalan yüzde: Argon, Gemini, Claude ve GPT ayrı başlıklarda
- Her modelin kendi sıfırlanma zamanı ve geri sayımı

bulunur. Web arayüzünde ayrıca kalan prompt/flow kredisi görünür.

Model kotaları `GetUserStatus → userStatus.cascadeModelConfigData.clientModelConfigs` içindeki `modelId`, `quotaInfo.remainingFraction` ve `quotaInfo.resetTime` alanlarından alınır. High/Medium/Low varyantları birleştirilmez; aynı yüzde veya aynı sıfırlanma zamanı ortak kota havuzunun kanıtı sayılmaz. Kaynakta bildirilmeyen kota **“Kota bildirilmedi”** olarak görünür; `%0`/`%100` varsayılmaz. Yüzde hassasiyeti korunur (`0.99998707` → `%99,9987`). Sıfırlanmaya kalan süreden 5 saatlik/günlük/haftalık kota dönemi tahmin edilmez.

JSON `groups` koleksiyonunun her satırı tek modeldir; `model_id` model kimliğidir. `remaining`/`remaining_percent` bilinmiyorsa `null`, dönem bildirilmediğinde `window` alanı `bilinmiyor` olur.

### Neden bazen "Antigravity kapalı olmalı" diyor?

Antigravity aynı anda **tek bir canlı keyring token'ı** kullanır. Farklı hesapların kotasını okumak için token sırayla değiştirilir. Uygulama açıkken bu yapılırsa uygulama yanlış hesaba geçebilir; bu yüzden toplu tarama öncesi kapatma istenir.

- Tek aktif hesap: uygulama açıkken okunabilir.
- Tüm hesaplar: Antigravity kapalı olmalı.
- Tarama sırasında keyring her profil sonunda eski hâline döner.

---

## Uyarılar

Varsayılan eşikler:

| Seviye | Kalan kota |
|---|---|
| Uyarı | ≤ %20 |
| Kritik | ≤ %5 |
| Doldu | %0 |

Ayarlar web arayüzünden veya `aio watch` ile değiştirilebilir.

### Masaüstü bildirimi

`notify-send` varsa eşik geçişlerinde bildirim gönderilir. Aynı hesap/grup için aynı reset penceresinde tekrar bildirim gönderilmez.

```bash
# Yalnızca aktif hesabı izle
aio watch

# Tüm hesapları izle (Antigravity kapalı olmalı)
aio watch --all

# Bir kez kontrol et (cron/systemd timer için)
aio watch --once

# Bildirim göndermeden
aio watch --no-notify
```

Systemd kullanıcı servisi örneği:

```ini
# ~/.config/systemd/user/aio-watch.service
[Unit]
Description=all-in-one Antigravity kota izleyici
After=graphical-session.target

[Service]
Type=simple
# Kendi proje yolunu yaz:
WorkingDirectory=%h/path/to/all-in-one
ExecStart=%h/path/to/all-in-one/.venv/bin/aio watch --interval 300
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now aio-watch.service
```

### Web arayüzü

`aio ui` açıkken sunucu aktif hesabı periyodik olarak yeniler; uyarılar banner olarak görünür. Tarayıcı bildirimlerini açmak için **Ayarlar → İzin ver** kullanılabilir.

---

## Web arayüzü

- Canlı keyring hesabı ve token durumu
- Hesap kartları, plan ve kota grupları
- **Hesap seç** açılır listesi ve tek tıkla geçiş
- **+ Hesap ekle → Antigravity ile giriş yap** akışı; token otomatik kaydı
- Giriş bekleme/başarı/hata banner'ı
- Tek tıkla kota okuma / yeniden adlandırma / silme
- Ana hesap ve aktif hesap rozetleri
- Arama (`/`), yenile (`R`), yeni hesap (`N`), kapat (`Esc`)
- Sistem / açık / koyu tema seçimi; tercih tarayıcıda korunur
- Yalnızca `127.0.0.1`; token'lar API yanıtlarında yer almaz
- Yerel olmayan `Origin`/`Host` istekleri reddedilir

Sunucu arka planda aktif hesabı yeniler; tüm hesapları yenilemek için **Yenile** düğmesi kullanılır ve gerekirse Antigravity kapatılır.

### Dashboard

- Hesap filtreleri: tümü, ilgilenilmesi gereken, kotası olan/bilinmeyen, aktif hesap. Kota veya ada göre sıralama.
- Kartlar güncellemede yerinde korunur; seçili hesap, odak, açık model ayrıntıları ve elle yazılan form değerleri kaybolmaz.
- `Model kotaları` içinde Argon/Gemini/Claude/GPT ayrı açılır bölümlerde, tam model adları ve bağımsız değerlerle gösterilir. Bölümler başlangıçta açık gelir; kapatma tercihi yoklamalarda korunur. `Diğer işlemler` bölümü ikincil düğmeleri toplar.
- Tek durum yoklaması: normalde 15 saniye, giriş/tarama sırasında 2,2 saniye. Gizli sekmede yeni yoklama planlanmaz; bağlantı hatalarında bekleme en çok 60 saniyeye yükselir.
- Durum istekleri 15 saniye, kullanıcı işlemleri 120 saniye istemci zaman aşımına sahiptir. İstemci zaman aşımı sunucudaki işlemi iptal etmez; tekrar denemeden önce durumu kontrol edin.
- Canlı keyring gözlemi 2 saniyelik önbellek/single-flight ile arka planda yapılır. Durum yanıtı uzun Secret Service/kota işini beklemez. Başlangıç ve açık kullanıcı işlemleri bu süreye dahil değildir.
- `aio watch` veya başka terminalin kota önbelleğini güncellemesi/silmesi dashboard'da algılanır. Başarısız tarama profilleri ayrı raporlanır.
- Uygunluk bilgisi hesaba ve uygulama oturumuna bağlanır: yüksek kota, doğrulanmış token veya bilinmeyen auth sonucu Antigravity uygunluğu anlamına gelmez.
- Üstteki **Sunucu: v…** etiketi gerçekten çalışan backend sürümüdür. Kod güncellemesinden sonra kendi `aio ui` sürecinizi durdurup yeniden başlatın; ardından tarayıcıda `Ctrl+Shift+R` kullanın. Eski süreç kendiliğinden yeni Python kodunu yüklemez.

---

## CLI komutları

| Komut | Açıklama |
|---|---|
| `aio init` | Canlı hesabı `main` olarak yakala |
| `aio status [--json]` | Durum özeti |
| `aio accounts list` | Hesapları listele |
| `aio login` / `aio accounts login` | Antigravity giriş akışıyla yeni hesap ekle |
| `aio accounts capture <ad> [--label] [--overwrite] [--primary]` | Canlı hesabı profile kaydet |
| `aio accounts add <ad>` | `capture` ile aynı |
| `aio accounts use <ad> [--stop] [--launch/--no-launch] [--restart]` | Hesaba geç |
| `aio use <ad>` | Kısayol |
| `aio accounts rename <eski> <yeni>` | Yeniden adlandır |
| `aio accounts delete <ad>` | Sil (ana/aktif korumalı) |
| `aio accounts primary <ad>` | Ana hesabı değiştir |
| `aio quota [ad] [--all] [--refresh] [--json]` | Kota göster / yenile |
| `aio ui [--port N] [--no-browser]` | Web arayüzü |
| `aio watch [--interval N] [--all] [--once] [--no-notify]` | Kota izleyici |
| `aio app status` | Antigravity durumu |
| `aio app start` / `aio app stop [--force]` | Uygulamayı aç/kapat |
| `aio doctor [--probe] [--json]` | Tanı |

---

## Veri ve gizlilik

| Yol | İçerik |
|---|---|
| `~/.all-in-one/state.antigravity.json` | Profil metadatası, aktif/ana seçim, ayarlar |
| `~/.all-in-one/quota_cache/antigravity/*.json` | Son kota önbelleği (token yok) |
| `~/.all-in-one/alerts.antigravity.json` | Uyarı tekrarını önleyen durum |
| `~/.all-in-one/keyring.lock` | Süreçler arası keyring geçiş kilidi (token yok) |
| `~/.all-in-one/backups/` | `app_storage.json` ve bozuk state yedekleri |
| `~/.all-in-one/oauth-client.local.json` | Yerel OAuth uygulama bilgileri; Git'e/pakete eklenmez |
| Secret Service | Canlı token ve profil token'ları |

- Token'lar **hiçbir zaman** state, cache, log veya API yanıtına yazılmaz.
- Kota istekleri Antigravity'nin `language_server` sürecinden gider; yeni hesap ekleme akışı doğrudan Google OAuth endpoint'lerine bağlanır.
- Telemetri gönderilmez (`--disable_telemetry`).
- `AIO_HOME` ortam değişkeni ile veri dizini değiştirilebilir.
- Eski sürümün `~/.all-in-one/state.json` ve `profiles/codex` verileri silinmez.

### Skills, MCP ve settings neden güvende?

Kota probe'u **geçici bir HOME** ile çalışır. Geçiş ise yalnızca keyring ve `app_storage.json` içindeki tek bir UI alanını yazar. `~/.gemini/config` altındaki MCP sunucuları, pluginler, skill'ler ve `settings.json` hiçbir aşamada kopyalanmaz veya değiştirilmez.

---

## Sorun giderme

### `secret-tool` bulunamadı

```bash
sudo pacman -S libsecret        # Arch / CachyOS
sudo apt install libsecret-tools # Debian / Ubuntu
```

KDE Wallet / GNOME Keyring'in açık ve kilidinin açık olduğundan emin ol.

### "Antigravity'de aktif oturum bulunamadı"

Antigravity'yi açıp bir Google hesabıyla giriş yap. Sonra:

```bash
aio init
```

### "Antigravity açık" hatası

```bash
aio app stop
# veya zorla:
aio app stop --force
```

Sonra tekrar dene.

### "Hesap doğrulanamadı" / "Giriş gerekli"

Refresh token iptal edilmiş olabilir. O profile geç, Antigravity'de yeniden giriş yap ve:

```bash
aio accounts capture <ad> --overwrite
```

### Antigravity “Welcome” / “not eligible” ekranı

Bu bir token veya kota hatası değildir. Antigravity'nin `GetAuthStatus` yanıtı hesabı **Antigravity için uygun değil** olarak işaretliyorsa uygulama her açılışta Welcome/Continue with Google ekranını gösterir. Aynı hesapla tekrar giriş yapmak bunu değiştirmez.

- UI canlı oturum panelinde **“Doğrulamayı aç”** düğmesi Google'ın verdiği doğrulama bağlantısını açar; hesabı doğrulayıp tekrar deneyin.
- Google Workspace/business hesabıysa Antigravity Welcome ekranındaki **Use Business Account** yolunu kullanın.
- Hesap hâlâ uygun değilse Antigravity için uygun kişisel bir hesap kullanın; ilgili profil kota takibi için listede kalabilir.
- Antigravity'yi hemen kullanmak için UI'da **Ana hesaba geç** (veya başka uygun hesabı seçip **Geç**) kullanın.

Kota Pro görünse bile Antigravity hesap uygunluğu ayrı bir Google kontrolüdür.

### Kota okunamıyor

```bash
aio doctor --probe
```

- Antigravity ve `language_server` yolunu kontrol et.
- Ağ erişimini kontrol et.
- Antigravity'yi güncelle; RPC şeması değişmiş olabilir.
- Ayrıntılı log: `~/.config/Antigravity/logs/language_server.log`

### Tüm kotalar neden yavaş?

Her profil için ayrı bir headless `language_server` başlatılır; profil başına yaklaşık 3–5 saniye sürer. 10 hesap için ~30–50 saniye normaldir. Antigravity açıkken yalnızca aktif hesap hızlı yoldan okunur.

### Port kullanımda

```bash
aio ui --port 9000
```

---

## Sınırlamalar

- Linux odaklıdır; Windows/macOS keyring yolları bu sürümde yoktur.
- Google uygulama/RPC değişiklikleri uyarlama gerektirebilir.
- Antigravity'nin giriş akışı otomatik başlatılır; Google hesabı seçimi/parola adımı kullanıcıda kalır. Araç Google kimlik bilgilerini toplamaz.
- Aynı anda tek canlı keyring vardır; tüm hesapların taranması Antigravity kapalıyken yapılır.
- Kota değerleri Antigravity'nin bildirdiği `remainingFraction` değerleridir; Google tarafında değişebilir.

---

## Geliştirme

```bash
# Aktivasyon gerektirmez; fish'te de çalışır.
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check --select F401,F821,F841 aio tests
node --check aio/web/static/app.js
```

Modüller:

| Modül | Görev |
|---|---|
| `aio/keyring.py` | Secret Service okuma/yazma, token çözümleme |
| `aio/accounts.py` | Profil yakalama, geçiş, ana hesap koruması |
| `aio/antigravity.py` | Süreç tespiti, çalışan LS keşfi, app_storage |
| `aio/probe.py` | İzole language_server probe ve `GetUserStatus` parser |
| `aio/quota.py` | Kota önbelleği, keyring takası, arka plan işi |
| `aio/alerts.py` | Eşikler, bildirim, tekrar önleme |
| `aio/login.py` | Giriş akışı ve otomatik hesap yakalama |
| `aio/google_oauth.py` | Antigravity OAuth istemcisi + yerel callback + token kaydı |
| `aio/locks.py` | Süreçler arası keyring geçiş kilidi |
| `aio/service.py` | CLI/web ortak servis katmanı |
| `aio/server.py` | FastAPI + güvenlik |
| `aio/cli.py` | `aio` komutları |

Testler ağa çıkmaz; gerçek kota doğrulaması için `aio doctor --probe`, gerçek giriş akışı için `aio login` kullanılır. UI + `aio watch` aynı anda çalışabilir; keyring geçişleri `flock` ile serileştirilir.

---

## Lisans

MIT. Ayrıntılar için [LICENSE](LICENSE).

Bu araç, Antigravity/Google hesaplarının kendi OAuth oturumlarını yönetir. Hesap kullanım koşullarına ve Google politikalarına uymak kullanıcının sorumluluğundadır.
