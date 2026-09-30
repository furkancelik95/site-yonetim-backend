# AGENTS.md — site-yonetim-backend

Bu dosya, bu repoda çalışan yapay zekâ asistanı için yazıldı (Claude Code, Cursor, Codex vb.).
Her oturumun başında bunu oku. Ayrıntılar `docs/` altında; aşağıdaki harita hangi işte
hangi dosyayı okuyacağını söyler.

---

## Bu proje ne

Site, apartman, rezidans, iş merkezi ve AVM yönetimi için **çok kiracılı (multi-tenant) bir SaaS**.
Yönetici aidatı hesaplar ve tahsil eder, gideri kaydeder, kasayı izler; sakin borcunu görür ve
talep açar; güvenlik ziyaretçi ve kargoyu kaydeder. Tek bir yönetim şirketi yüzlerce siteyi
tek hesaptan yönetebilir.

Hedef ölçek: tek sitede 10.000+ bağımsız bölüm, toplamda 500.000+ bölüm.
Ürün tanımı: `docs/01-urun.md`.

## Ekip ve iş bölümü

| Kim | Ne yapar | Nerede |
|---|---|---|
| **Furkan** (+ sen) | Backend: API, iş kuralları, veritabanı, yetki | **bu repo** |
| **Burhan** (+ kendi asistanı) | Frontend: web arayüzü | `furkancelik95/site-yonetim-frontend` |

Çalışma şekli **servis isteği** üzerinedir:
1. Frontend bir ekran için ihtiyacı olan servisi bu repoda **GitHub Issue** olarak açar
   (`.github/ISSUE_TEMPLATE/servis-istegi.md` şablonu).
2. Issue'da uç nokta, girdi, çıktı, örnek veri ve o ekranın iş kuralları yazar.
3. Sen servisi `docs/` altındaki kurallara uyarak yazarsın, testini yazarsın, issue'yu kapatırsın.
4. Frontend o sırada sahte veriyle çalışır; servis gelince gerçeğine bağlanır.

Issue ile `docs/` çelişirse **dur ve sor**. Sessizce birini seçme.

## Teknoloji

- **Dil: Python.** Karar verildi.
- Framework ve kütüphaneler Furkan'ın kararıdır. Henüz karar verilmediyse önerilen yığın
  `docs/02-mimari.md` içinde: **FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL + Pydantic v2 + pytest.**
- Frontend: Node.js tabanlı ayrı bir uygulama. Bu repo **yalnızca JSON API** sunar, HTML üretmez.

## ASLA / HER ZAMAN — tartışmaya kapalı kurallar

Bunlar pazarlık konusu değil. Bir istek bunlardan birini çiğnemeni gerektiriyorsa yapma, sor.

1. **Para asla `float` değildir.** Her yerde `decimal.Decimal`, veritabanında `NUMERIC(18,2)`,
   JSON'da **metin** (`"1234.56"`). Yuvarlama `ROUND_HALF_UP`. Python'un varsayılanı
   `ROUND_HALF_EVEN` — **yanlış**, kullanma. → `docs/04-is-kurallari.md` §1
2. **Dağıtımda kuruş kaybolmaz.** 100,00 TL üç daireye 33,34 + 33,33 + 33,33 olarak dağılır;
   toplam her zaman kaynak tutara eşittir. → §2
3. **Finansal kayıt silinmez, düzenlenmez.** Tahakkuk, tahsilat, gider, kasa hareketi yanlışsa
   **ters kayıt** atılır; orijinal yerinde kalır. `DELETE` ve `UPDATE amount` yok. → §8
4. **Bir site başka bir sitenin verisini asla göremez.** Her sorgu site kapsamındadır; kapsam
   dışı kayıt eklemek/güncellemek hata fırlatır. → `docs/09-guvenlik-kvkk.md` §1
5. **Erişimi olmayan siteye 404 döner, 403 değil.** 403 "bu site var ama giremezsin" demektir,
   sitenin varlığını sızdırır. → `docs/05-yetki.md`
6. **Yetki rol adına değil izne bakar.** `if role == "Yönetici"` yazma; `if can("finance.charge.post")` yaz.
7. **Tüm listeler sayfalanır, toplama veritabanında yapılır.** Bir döngü içinde tüm tabloyu
   tarama. → `docs/08-performans.md` (ölçülmüş rakamlar orada)
8. **Tahakkuk önce önizlenir, sonra kaydedilir.** Aynı döneme ikinci kez tahakkuk kesilemez.
9. **Sır koda girmez.** Bağlantı dizesi, parola, anahtar yalnızca ortam değişkeninden okunur;
   `.env` repoya girmez (`.gitignore`'da). Parolalar hash'li saklanır (bcrypt/argon2).
10. **Kullanıcıya dönen her mesaj Türkçedir.** Kod, değişken ve tablo adları İngilizce.

## Doküman haritası — hangi işte neyi oku

| İş | Oku |
|---|---|
| Projeye ilk kez bakıyorsun | `01-urun.md` → `02-mimari.md` → bu dosya |
| Tablo / model yazacaksın | `03-veri-modeli.md` |
| Para, tahakkuk, tahsilat, gider, kasa ile ilgili herhangi bir şey | **`04-is-kurallari.md` — atlama** |
| Yetki, rol, izin, giriş | `05-yetki.md` |
| Uç nokta yazacaksın | `06-api-sozlesmesi.md` |
| Test yazacaksın | `07-test-senaryolari.md` (beklenen değerler birebir orada) |
| Liste, rapor, pano, toplam | `08-performans.md` |
| Dosya yükleme, kişisel veri, izolasyon | `09-guvenlik-kvkk.md` |
| Örnek veri / seed | `10-demo-veri.md` |
| Excel'den daire/sakin aktarımı | `11-excel-aktarim.md` |
| Henüz karar verilmemiş konular | `12-acik-kararlar.md` — **bunlar için kural uydurma** |

## Referans uygulama

Bu kuralların tamamı daha önce **.NET 10** ile yazıldı, çalışıyor ve 101 testle doğrulandı:
`brhnnkaraa6/siteyonetimi` (gizli repo — erişim gerekirse Burhan'dan istenir).

`docs/` kendi başına yeterli olacak şekilde yazıldı; referans kodu açmadan işi yapabilmelisin.
Bir kuralın **nasıl** uygulandığından emin değilsen referansa bak, ama .NET kalıplarını
Python'a taşıma — aynı davranışı Python'un doğal yolu ile kur. İlgili referans dosyalar her
dokümanın sonunda listelenir.

## Bir servis isteği gelince

1. Issue'yu oku. İlgili `docs/` bölümünü oku (yukarıdaki tablo).
2. Belirsiz bir nokta varsa **issue'ya yorum yaz, sor.** `12-acik-kararlar.md` içindeki bir
   konuya dokunuyorsa kesinlikle kural uydurma.
3. Veri modeli → servis katmanı (iş kuralı) → uç nokta → test sırasıyla yaz.
4. İş kuralını **uç nokta içine değil**, framework'ten bağımsız bir servis/alan katmanına yaz.
   Kural, HTTP olmadan test edilebilmeli.
5. `07-test-senaryolari.md`'de o kurala ait senaryo varsa onu **birebir** teste çevir.
6. Tenant izolasyon testi ekle: "B sitesinin kullanıcısı bu uç noktadan A'nın verisini göremez."
7. OpenAPI şeması güncel olsun (FastAPI bunu kendisi üretir). Frontend buradan okur.

## Bitti tanımı

Bir iş şunların hepsi olmadan bitmiş sayılmaz:

- [ ] Uç nokta `06-api-sozlesmesi.md` kurallarına uyuyor (hata biçimi, sayfalama, para metni)
- [ ] Yetki kontrolü var ve testi var (izinli kullanıcı yapabiliyor, izinsiz yapamıyor)
- [ ] Tenant izolasyon testi var
- [ ] Finansal işlemse: tekrar gönderimde çift kayıt oluşmuyor (idempotent), testi var
- [ ] Hata mesajları Türkçe, kullanıcıya ne yapması gerektiğini söylüyor
- [ ] Liste döndürüyorsa sayfalı; toplama yapıyorsa veritabanında
- [ ] Seed/demo verisi gerekiyorsa güncellendi
- [ ] İlgili issue kapatıldı, kapanış yorumunda uç nokta ve örnek yanıt var
