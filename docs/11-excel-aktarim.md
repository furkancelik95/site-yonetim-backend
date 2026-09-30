# 11 — Excel'den daire ve sakin aktarımı

Gerçek bir siteyi sisteme almanın ilk adımı. 48 daireyi elle girmek kimsenin yapacağı iş değil.

**Akış iki adımlı: önce doğrula ve göster, sonra onayla ve yaz.** Kirli bir Excel'in sessizce
içeri girmesi, sonradan temizlenmesi çok pahalı bir hatadır.

## 1. Akış

1. `GET …/imports/units/template.xlsx` — şablon indir.
2. `POST …/imports/units` (multipart, `file`) — yükle.
   - Dosya kontrolü: `.xlsx`, **en fazla 5 MB**, içerik imzası ZIP (`50 4B 03 04`) değilse reddet.
   - Dosya geçici klasöre yazılır, **hiçbir kayıt oluşturulmaz**.
   - Yanıt: **önizleme** — satırlar (doğrulanmış hâliyle), hatalar, uyarılar, aktarılabilir satır
     sayısı, bir `import_id`.
3. Kullanıcı önizlemeyi görür, onaylar.
4. `POST …/imports/units/{import_id}/confirm` — **tek transaction** ile yaz (§4).
5. Onaylanmamış geçici dosyalar **6 saat** sonra silinir.

## 2. Şablon

İki sayfa: **Daireler** (başlık satırı kalın, dondurulmuş, 3 örnek satır) ve **Açıklama**.

Sütunlar: `Blok · Daire No · Kat · Daire Tipi · Brüt m² · Net m² · Arsa Payı Pay · Arsa Payı Payda ·
Kullanım · Malik Ad · Malik Soyad · Malik Telefon · Malik E-posta · Kiracı Ad · Kiracı Soyad ·
Kiracı Telefon · Kiracı E-posta`

Örnek satırlar:
| Blok | Daire No | Kat | Tip | Brüt | Net | Pay | Payda | Kullanım | Malik | | Tel | E-posta | Kiracı | | Tel |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 1 | 1 | 2+1 | 104.5 | 86 | 40 | 10000 | Konut | Ayşe | Yılmaz | 5321234567 | ayse@ornek.com | | | |
| A | 2 | 1 | 1+1 | 68 | 56.4 | 26 | 10000 | Konut | Mehmet | Kaya | 05339876543 | | Elif | Demir | 5445556677 |
| B | Z1 | 0 | Dükkan | 120 | 110 | 54 | 10000 | Ticari | Ali | Çelik | +905367778899 | | | | |

**Sütun sırası önemli değil, başlık adına bakılır.** Başlık kırpılır, büyük/küçük harf duyarsız
(Türkçe kurallarla, `04` §15). Eş anlamlılar:

| Alan | Kabul edilen başlıklar |
|---|---|
| Blok | `blok`, `blok adı` |
| Daire No | `daire no`, `daire`, `bağımsız bölüm no`, `no` |
| Kat | `kat` |
| Daire Tipi | `daire tipi`, `tip` |
| Brüt m² | `brüt m2`, `brüt m²`, `brüt` |
| Net m² | `net m2`, `net m²`, `net` |
| Arsa Payı Pay | `arsa payı pay`, `arsa payı`, `pay` |
| Arsa Payı Payda | `arsa payı payda`, `payda` |
| Kullanım | `kullanım`, `kullanım tipi` |
| Malik / Kiracı alanları | `malik ad`, `malik adı`, `malik soyad`, `malik soyadı`, `malik telefon`, `malik e-posta`, `malik eposta` — kiracı için aynıları `kiracı …` ile |

## 3. Satır doğrulama

**Temel ilke: hatalı satır koşuyu durdurmaz.** 200 satırın 3'ü bozuksa kullanıcı 197'sini alıp
3'ünü düzeltebilmeli. Tamamını reddetmek kullanıcıyı Excel'e geri gönderir.

- **Hata** → o satır aktarılmaz. **Uyarı** → satır aktarılır, sorunlu alan boş bırakılır ya da
  varsayılan kullanılır.
- Her sorun: `{row_number, column, message, severity}`. Mesajlar Türkçe, değeri tırnak içinde gösterir.
- Tamamen boş satırlar sessizce atlanır.
- Metinler kırpılır, içteki çoklu boşluk teke indirilir.

| Alan | Kural | Sonuç |
|---|---|---|
| Daire No | boş olamaz | **hata**, satır atlanır |
| Blok + Daire No | aynı dosyada **aynı blokta aynı numara** ikinci kez | **hata**: `'A-5' zaten 2. satırda var.` Farklı blokta aynı numara sorun değil |
| Brüt / Net m² | sayı değilse **hata**; 1–10.000 dışıysa **uyarı**, boş bırakılır; 2 ondalığa yuvarlanır | |
| Net > Brüt | | **uyarı**: `Net alan (90,00) brütten (80,00) büyük görünüyor.` |
| Arsa payı | pay ve payda **ikisi birlikte**; yalnız biri doluysa **hata**; ikisi de ≥ 1; pay > payda ise **uyarı** | |
| Kat | tam sayı, −5 … 100; dışındaysa uyarı. `"3,0"` gibi Excel artıkları kabul | |
| Kullanım | `konut/mesken/daire` → residential · `ticari/dükkan/dukkan/ofis/işyeri/isyeri` → commercial · `depo` → storage · `otopark/garaj` → parking · boş → residential | tanınmazsa **uyarı**, residential |
| Malik | **zorunlu**. Ad ve soyad ikisi de boşsa **hata** ("Her bağımsız bölümün maliki olmalı"); yalnız biri boşsa **hata** | |
| Kiracı | isteğe bağlı; doldurulduysa malikle aynı kurallar | |
| Ad / soyad | 2–40 karakter, **rakam içeremez** → aksi **hata** | |
| Ad biçimi | kayıtta **ad baş harf büyük** (`Ayşe`), **soyad tamamı büyük** (`YILMAZ`) — Türkçe kurallarla | |
| Telefon | yalnız rakamlar alınır; `90` + 10 hane ise baştaki 90, `0` + 10 hane ise baştaki 0 atılır; **10 hane ve 5 ile başlamıyorsa uyarı**, boş bırakılır. Kayıt **E.164**: `+905321234567` | |
| E-posta | küçük harfe çevrilir; `^[^@\s]+@[^@\s]+\.[^@\s]+$` ve ≤ 254 değilse **uyarı**, boş bırakılır | |

### 3.1 Sayı okuma — kültür tuzağı

Excel'den gelen `"104.50"` tr-TR'de 10450, İngilizcede 104,5 demektir. Sessiz veri bozulmasının
klasik kaynağı. Tahmin etme, **ayracın konumuna bak:**

1. Boşluklar (normal ve bölünmez) silinir.
2. **Hem virgül hem nokta** varsa → **sondaki** ondalık ayracıdır, diğeri binlik.
   `1.234,56` → 1234.56 · `1,234.56` → 1234.56
3. **Yalnız virgül** varsa → ondalık (Türk yazımı). `104,50` → 104.50
4. **Yalnız nokta** varsa:
   - birden çok nokta → binlik: `1.234.567` → 1234567
   - tek nokta ve arkasında **tam 3 hane** ve başta değil → binlik: `1.234` → 1234
   - diğer → ondalık: `104.50` → 104.50, `104.5` → 104.5, `1.2345` → 1.2345

Sonra `Decimal(...)` ile oku. Senaryolar: `07-test-senaryolari.md` §5.3.

## 4. Onay — yazma

Tek transaction:
- **Sitede zaten var olan bölüm** (aynı blok + numara) **atlanır, üzerine yazılmaz.** Atlananlar
  sonuçta listelenir ("A-5 zaten kayıtlı, atlandı").
- Blok yoksa oluşturulur (sıralama sona).
- Daire tipi yoksa oluşturulur, **ağırlık 1** (ağırlığı sonra yönetici düzeltir).
- Kişiler oluşturulur (§3 biçimiyle).
- Taraflar: malik `owner`, kiracı `tenant`; **başlangıç tarihi = içinde bulunulan yılın 1 Ocak'ı**.
- Cari hesaplar `03-veri-modeli.md` §5 kuralıyla: malik → `-M`; kiracı varsa kiracıya `-K`;
  kiracı yoksa malike oturan hesabı `-O`. Kod çakışmasında sonuna sayı.
- Sonuç: yeni bölüm, yeni kişi, yeni hesap, yeni blok sayıları ve atlanan bölümler.

*(Öneri — referansta yok)* Plan tavanı: aktarım sonrası bölüm sayısı planın `max_units`'ini
aşacaksa önizlemede **uyarı** göster (engelleme değil). Platform panelinde "tavan aşıldı" zaten görünür.

## 5. Uygulama notları (Python backend)

Kod: `domain/imports/` (saf doğrulayıcı, sayı okuma) · `services/imports.py` (dosya, depo,
yazma) · `api/v1/imports.py` (uçlar).

- **Dosya güvenliği** (`09` §3.1): `.xlsx` uzantısı + ZIP imzası; en fazla 5 MB (istek gövdesi
  ara katmanda, ayrıştırmadan önce kesilir → 413). Açılmış boyut ≤ 50 MB ve ≤ 500 arşiv
  girdisi (zip bombası). XML `defusedxml` ile okunur (XXE). Yalnız saklanmış hücre değerleri
  okunur: formül çalıştırılmaz, dış bağlantı ve makro okunmaz. En fazla **5.000 veri satırı**
  ve 60 sütun.
- **Sayfa:** adı `Daireler` olan sayfa, yoksa ilk sayfa. İlk dolu satır başlıktır.
- **Zorunlu sütunlar:** `Daire No`, `Malik Ad`, `Malik Soyad`. Biri yoksa dosya bütün olarak
  reddedilir (satır satır hata üretmek anlamsız). Aynı alan iki başlıkla gelirse ilki kullanılır,
  uyarı verilir.
- **Başlık ve kullanım eşlemesi ASCII'ye katlanarak yapılır:** `KIRACI AD`, `Kiracı Ad`,
  `kiraci ad` aynı başlıktır; `ISYERI` → commercial. (Türkçe kurala göre `I` → `ı` olduğu için
  büyük ASCII yazım aksi hâlde eşleşmezdi.)
- **Blok** boş olabilir (tek bloklu site); en fazla 40 karakter. **Daire No** en fazla 20.
  **Daire tipi** 40 karakteri aşarsa uyarı, tip boş. Aynı bölüm tespiti: blok adı Türkçe
  kurallarla harf duyarsız, numara birebir.
- **Arsa payı pay > payda** → uyarı ve **arsa payı boş** (tek elden giriş kuralıyla aynı).
- **Sayı hücresi** (Excel'in sayı olarak sakladığı) kültür sorunu taşımaz, doğrudan okunur;
  yalnız metin hücreleri §3.1 ile. `0.250` binlik sayılmaz (tam kısmı 0).
- **Geçici depo:** `IMPORT_STORAGE_DIR/{site_id}/{user_id}/{import_id}.xlsx` (boşsa sistemin
  geçici klasörü). `import_id` tahmin edilemez; aktarımı **yalnız yükleyen kullanıcı, aynı
  sitede** onaylar. Onay dosyayı atomik olarak sahiplenir (ikinci onay 404); yazma hata
  verirse dosya geri konur. Süresi dolanlar her yüklemede ve `python -m site_yonetim.cli
  purge-imports` ile silinir (saatlik zamanlanmış iş önerilir). **Birden çok API kopyası**
  çalışıyorsa depo ortak bir birim olmalı.
- **Onay dosyayı yeniden doğrular**; önizlemeden sonra sitede oluşan bölümler de atlanır.
  Aynı anda çakışan yazım (benzersizlik ihlali) → 409, hiçbir şey yazılmaz.
- Aktarılacak yeni bölüm yoksa önizlemede `import_id = null` (dosya saklanmaz).
- **Kişi tekilleştirme yapılmaz:** her satırın maliki/kiracısı yeni kişidir. Aynı kişinin
  birden çok bölümü varsa kayıtlar sonra birleştirilir — açık karar `12` K16.
- Denetim kaydı (`09` §6) gelince toplu içe aktarma oraya yazılacak.

## Referans

`brhnnkaraa6/siteyonetimi`:
- `src/SiteYonetimi.Application/Import/UnitImportValidator.cs`
- `src/SiteYonetimi.Infrastructure/Import/ExcelUnitImporter.cs` (şablon, başlık eşleme)
- `src/SiteYonetimi.Infrastructure/Import/UnitImportApplier.cs` (yazma)
- `src/SiteYonetimi.Web/Controllers/ImportController.cs` (5 MB, ZIP imzası, 6 saat temizlik)
