# Codespaces demo ortamı

PostgreSQL, API, demo verisi ve frontend'i GitHub Codespaces'te tek tıkla açar ve
paylaşılabilir bir bağlantı verir. **Yalnız demo ve toplantı içindir; gerçek veri girilmez.**

## Açmak

1. Backend reposunda **Code → Codespaces → Create codespace on main**.
2. İlk açılışta GitHub, frontend reposunu okuma izni ister (**Authorize and continue**). Bu
   izin olmadan frontend klonlanamaz; API yine çalışır.
3. Kurulum ilk seferde birkaç dakika sürer. Bittiğinde terminalde şu satırı görürsünüz:

   ```
   Site Yönetim demo hazır:  https://<codespace-adı>-5173.app.github.dev
   ```

4. Giriş ekranında demo hesapları listelenir (parola `Demo1234!`). Her rol ayrı bir hesapla
   denenebilir: platform yöneticisi, yönetim şirketi, muhasebe, denetçi, güvenlik, sakin.

## Paylaşmak

Bağlantı varsayılan olarak **yalnız size** açıktır (GitHub girişi ister).

```bash
bash .devcontainer/share.sh public    # bağlantıyı bilen herkese aç
bash .devcontainer/share.sh private   # tekrar kapat
```

Komut yetki nedeniyle çalışmazsa: **PORTS** sekmesi → "Site Yönetim (demo)" → sağ tık →
**Port Visibility → Public**.

> **Dikkat:** herkese açıkken bağlantıyı bilen herkes demo hesaplarıyla giriş yapabilir.
> Demo bitince `private` yapın ya da codespace'i durdurun.

## Nasıl çalışır

| Parça | Nerede | Dışarıdan erişim |
|---|---|---|
| PostgreSQL 16 | `db` konteyneri; roller `docker/postgres/10-roles.sh` ile (uygulama rolü RLS'i atlayamaz) | yok |
| API | `127.0.0.1:8000`, açılışta göçler + demo verisi | yok — yalnız `/api` yolundan |
| Frontend | Vite, `5173`; `/api` isteklerini API'ye geçirir | tek açık port |

- Frontend ve API **aynı adresten** sunulur: oturum çerezi ve CORS ek ayar istemez.
- Veritabanı parolaları ve JWT sırrı ilk kurulumda rastgele üretilir
  (`.devcontainer/.env`, repoya girmez).
- `API_DOCS_ENABLED=false`: API belgeleri paylaşılan bağlantıda görünmez.
- Frontend henüz yazılmamış birkaç uç için sahte veri (MSW) kullanır; ekranda rozetle belli olur.
- Kayıtlar: `/tmp/site-yonetim/{api,web,migrate}.log`.

## Kota ve maliyet

Kişisel GitHub hesabında Codespaces ayda **120 çekirdek-saat** (2 çekirdekli makinede yaklaşık
60 saat) ve 15 GB depolama ücretsizdir. Codespace boşta kalınca kendiliğinden durur (varsayılan
30 dakika); tekrar açıldığında her şey yeniden başlar, veri korunur. Kullanılmayan codespace'i
github.com/codespaces sayfasından silin.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| "Frontend kurulu değil" | Frontend izni verilmemiş. Codespace'i silip yeniden oluşturun, izni onaylayın. |
| Sayfa açılmıyor | `bash .devcontainer/start-demo.sh` ile yeniden başlatın; `api.log`/`web.log`'a bakın. |
| Frontend'in son halini görmek | `git -C /workspaces/site-yonetim-frontend pull && bash .devcontainer/start-demo.sh` |
