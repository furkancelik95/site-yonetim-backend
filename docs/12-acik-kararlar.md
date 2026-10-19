# 12 — Açık kararlar

**Yapay zekâ için kural:** bu listedeki bir konuya dokunan bir iş gelirse **kural uydurma.**
"Şu anki davranış" sütununu uygula ya da issue'ya yazıp sor. Karar verilince madde buradan
silinir ve ilgili dokümana taşınır.

| # | Konu | Şu anki davranış | Öneri | Karar | Bloke ettiği |
|---|---|---|---|---|---|
| **K1** | **Online ödeme** | Yok. Tahsilat yönetici tarafından elle girilir | PRD bunu "kesin, MVP" diyor ve pilot hedefi koyuyor (%35 online tahsilat). Platform parayı **tutmamalı / üzerinden geçirmemeli** (6493 sayılı Kanun) — lisanslı PSP'nin pazaryeri ürünü (ör. iyzico Pazaryeri) ile para doğrudan sitenin hesabına gider. Sağlayıcı seçilmeli | Burhan + ortaklar | Online tahsilat, banka eşleştirme |
| **K2** | Gecikme tazminatı oranı ve yöntemi | Aylık %5, günlük = aylık/30, 5 gün tolerans, anaparadan (`04` §6) — **ama referansta deftere yazılmıyor** | Hukuki teyit: günlük mü aylık mı işletilir, tolerans var mı, kıst ay nasıl sayılır | Hukuk | Gecikme işi |
| **K3** | Aylık tutarın yuvarlama farkı | `round(yıllık/12)` her ay → yılda birkaç kuruş kayıp | Yıllık tutar `distribute()` ile 12 aya bölünür, farkı ilk aylar taşır (`04` §3) | Burhan | Tahakkuk |
| **K4** | Avansın sonraki borca mahsubu | Bakiye doğru, ama avans yeni borca mahsup kaydı üretmiyor | Tahakkuk kaydında hesabın avansı önce yeni borca mahsup edilir (`04` §5.1) | Burhan | Tahakkuk, mahsup dökümü |
| **K5** | Bildirim sağlayıcısı | Duyuru/kargo bildirimi **kayıt tutuluyor, gönderilmiyor**. Uygulama içi bildirim merkezi var (`06` §2.21); kanal tercihleri ve push jetonları saklanıyor, dış gönderim yok | SMS (paralı, ölçeğe göre maliyet), e-posta, mobil push. Önce sağlayıcıdan bağımsız bir gönderim arayüzü + "yalnız kaydet" adaptörü | Burhan + ortaklar | Gerçek bildirim |
| **K6** | Mobil uygulama | Yok. Web önce | Tek uygulama + girişte site seçimi (ucuz) mı, müşteri başına uygulama (her müşteri için ayrı mağaza kaydı) mı | Burhan + ortaklar | Mobil |
| **K7** | Muhasebe/ERP aktarımı | Yok | PRD ilk aday olarak "Kivi" diyor (netleşmedi). Önce sağlayıcıdan bağımsız bir muhasebe fişi dışa aktarımı; adaptörler sonra. Fiş formatı ve hesap planı mali müşavirle | Mali müşavir + ürün | ERP |
| **K8** | KVKK saklama süreleri ve metinler | Yok | Hukukçu belirler: finansal kayıt, kişi, ziyaretçi, plaka, log, sakin kayıt başvurusu (bekleyen/reddedilen), banka ekstresi önizlemesi (6 saat) için ayrı süreler | Hukuk | Canlı pilot |
| **K9** | Kıst hesap (ay ortası taşınma) | Yok: tahakkuk tarihinde kim aktifse ayın tamamı ona | Gün bazlı paylaştırma seçeneği | Ürün + mali müşavir | Tahakkuk |
| **K10** | Satışta devir öncesi borç | Tanımsız | Malik değişince eski malikin borcu kimde kalır | Hukuk | Daire devri |
| **K11** | Borçlu bölüme hizmet kısıtlaması | Yok | Rezervasyon vb. engellenebilir mi, hukuki sınırı | Hukuk | Rezervasyon |
| **K12** | Platform yöneticisinin müşteri paneline destek erişimi | Yok. Platform yöneticisinin site üyeliği yok | Ayrı yetki, süreli, müşteri onaylı, her giriş denetim kaydında | Burhan + hukuk | Destek |
| **K13** | Fiyat paketleri | Planlar var, fiyat yok | | Ortaklar | Faturalama |
| **K14** | Merkezi ısıtma tüketim bileşeni | %70 **eşit** (tüketim yerine) + %30 m² — sayaç okuma yok (`10-demo-veri.md` §3) | Sayaç okuma modülü gelince `by_meter_consumption` | Ürün | Isıtma dağıtımı |
| **K15** | Aidatta vergi, e-arşiv, tahsilat makbuzu | Yok. Site yönetimi KDV mükellefi değil kabulü. Makbuz **verisi** var (`GET …/payments/{id}`), numara yok (`receipt_number = null`) | Makbuz gerekli mi, numaralandırma kuralı | Mali müşavir | Makbuz |
| **K16** | Excel aktarımında aynı kişinin birden çok bölümü | Tekilleştirme yok: her satırın maliki/kiracısı ayrı kişi kaydı (`11` §5) | Aynı ad + aynı telefon/e-posta → tek kişi; önizlemede "birleştirilecek" diye göster. Yanlış birleştirme kişisel veri karıştırır, bu yüzden kural ürün kararı | Burhan + ürün | Kişi birleştirme |
| **K17** | Hisseli mülkiyette (birden çok aktif malik) borç kime | Tek kişiye: hissesi büyük, eşitse başlangıcı eski olan (`04` §4.5) | Hisse oranında bölmek (her malike `distribute` ile pay) — ama her malikin ayrı cari hesabı ve ayrı tahsilatı gerekir | Burhan + ürün | Tahakkuk |
| **K18** | Talep durum geçişleri ve sakinin yetkileri | Her durumdan her duruma geçilebilir (aynı durum hariç); `resolved`/`closed` için çözüm metni zorunlu; yeniden açmak serbest. Sakin kendi talebine yorum yazar, durumunu değiştiremez/iptal edemez | Kapalı/iptal talep yeniden açılamasın ya da yalnız 7 gün içinde; sakin kendi açık talebini iptal edebilsin; durum değişince sakine bildirim | Burhan + ürün | Talep akışı |
| **K19** | Portföyde site "sağlık durumu" | Hesaplanmıyor; ham ölçüler dönüyor (tahsilat oranı, açık bakiye, geciken talep) | Örn. tahsilat oranı ≥ %90 ve 60+ gün borç yok → iyi; %70–90 → dikkat; altı → sorunlu. Eşikler ürün kararı | Burhan + ürün | Portföy rozeti |
| **K20** | Rol sırası (yetki devri ve eskalasyon için "aynı veya üst rol") | Tanımsız. Onay mekanizmasında devir yalnız **aynı role** yapılır; eskalasyon hedefi her kuralda açıkça seçilir, seçilmezse yalnız hatırlatma gider (Furkan, 19.10.2026) | Ör. Yönetici > YK Üyesi > Muhasebe/Denetçi > Teknik/Güvenlik — ürün belirlemeli | Ürün | Yetki devri, eskalasyon |
| **K21** | Mevcut akışların onay mekanizmasına bağlanması | Bağlı değil: kayıt başvurusu, iade, tahakkuk, rol değişikliği doğrudan yetkiyle (Furkan, 19.10.2026: "şimdilik bağlama") | Yazılım Dokümanı 6.1 "hiçbir modül kendi onay mantığını yazmaz". Önce kayıt başvurusu, sonra iade/tahakkuk; frontend akışı "onaya gönderildi" (202) olur | Furkan + Burhan | Onay kapsamı |
| **K22** | Finans sorumlusu modeli | Bölüm × kişi × tür (malik/oturan) cari hesabı; ödeyeni borç türü belirler (`04` §4.5, KMK m.22) | Yazılım Dokümanı YAP-04: bölümde tarih aralıklı tek "finans sorumlusu" bayrağı ve hisselerin tam %100 olması zorunlu | Ürün + mali müşavir | Cari, tahakkuk |
| **K23** | Tahakkuk kaynağı | İşletme projesinden (KMK m.37) | Yazılım Dokümanı FIN-02: versiyonlu aidat kurallarından; bütçeden aidat türetmek Faz 2 | Ürün | Tahakkuk, bütçe |
| **K24** | Mahsup sırası | Aynı vadede önce gecikme tazminatı (TBK m.100, `04` §5.1) | Yazılım Dokümanı §8.7: önce ana borç, sonra gecikmesi | Hukuk | Tahsilat |
| **K25** | Vade ve dönem varsayılanları | Kesim ayın 1'i, vade +14 gün, dönem = takvim ayı (`04` §7.2) | Yazılım Dokümanı: vade ayın 10'u, dönem başından 5 gün önce taslak, özel dönem tanımları | Ürün | Tahakkuk |
| **K26** | Talep yorumu ile iç not | Yorumlar talebi açan sakine de görünür; iç not yok | Yazılım Dokümanı HIZ-02: iç not sakin kanalında hiç dönmez — eklenmeden önce görünürlük alanı ve süzgeç şart | Ürün | Talep |
| **K27** | Adlandırma: "incidents" | `incidents` = güvenlik olay kaydı | Yazılım Dokümanı'nda `incidents` = talepleri toplayan "ana olay" (HIZ-02); güvenlik olayı "nöbet defteri" | Burhan + Furkan | Talep, güvenlik |
| **K28** | Demirbaş mı teknik varlık mı | `assets` mali demirbaş (bedel, zimmet) | Yazılım Dokümanı HIZ-07: teknik varlık (marka, seri no, garanti, yasal muayene) — aynı tablo mu ayrı mı | Ürün | Varlık, bakım |
| **K29** | Personel–kullanıcı bağı | Personel kaydı hesap açmaz (`06` §2.20) | Yazılım Dokümanı IK-01: kart kullanıcıyla eşleşir, ayrılışta rol atamaları kendiliğinden biter | Ürün | İK |
| **K30** | Otopark modeli | Park yeri bir bağımsız bölüm tipi (`parking`, kendi aidatı) | Yazılım Dokümanı OTP-01: park yeri ayrı kaynak, bölüme tarihçeli tahsis | Ürün | Otopark |
| **K31** | Anket gizliliği | Bölüm başına bir oy, kim verdiği tutulmaz | Yazılım Dokümanı ILT-03: anket bazında "anonim mi" ve "bölüm başına tek yanıt mı" seçeneği | Ürün + hukuk | Anket |
| **K32** | Sakine gider şeffaflığı | Gerçekleşen giderler tedarikçi adı ve fatura görüntüsüyle açık | Yazılım Dokümanı RAP-02: kişi bilgisi içermeyen, onaylı özet | Ürün + hukuk | Şeffaflık |

## PRD ile repo dokümanları arasındaki çelişkiler

**Karar (Furkan, 30.09.2026; 19.10.2026'da yeni dokümanlar için yinelendi):** çelişkide
**repo `docs/` geçerlidir**. PRD
(`Site_Tesis_Yonetim_Platformu_Master_PRD_v1_2.docx`) ile uyum, madde madde ayrı işler olarak
ele alınır; bir madde uyumlanınca ilgili dokümana taşınır ve bu tablodan silinir.

| # | Konu | Repo `docs/` (şu an geçerli) | PRD v1.2 | Etkisi |
|---|---|---|---|---|
| **P1** | Erişimi olmayan site | **404** (`05` §7) | 403 + denetime "kapsam ihlali denemesi" | Yetki katmanı |
| **P2** | Kapalı / planda olmayan modül | **404** (`02` §4) | 402/403 + yükseltme bilgisi, veri salt okunur | Modül kontrolü |
| **P3** | Kiracı (tenant) sınırı | **Site** — `site_id` (`02` §3) | Organizasyon; kapsam Org › Site › Blok › Bölüm | İzolasyon, blok/bölüm kapsamlı roller |
| **P4** | Para saklama | **`NUMERIC(18,2)` + `Decimal`**, tek para birimi TRY (`04` §1) | Kuruş cinsinden tam sayı + para birimi sütunu | Veri modeli |
| **P5** | Giriş | **E-posta + parola**; OTP/2FA sonra (`05` §8) | OTP/parola + MFA MVP'de; platform kullanıcısına MFA zorunlu; hassas işlemde yeniden doğrulama | Kimlik |
| **P6** | Aydınlatma metni | **Bağlantı yeterli, onay kutusu yok** (`09` §5) | İlk girişte ve metin değişince onay | KVKK ekranları |
| **P7** | Barındırma | **Türkiye'de** (`09` §5) | Açık karar: yurt içi seçenek + yurt dışı aktarım değerlendirmesi | Altyapı |
| **P8** | Denetim kaydı | `09` §6 şeması — **19.10.2026'da büyük kısmı uyumlandı**: aktör tipi, istek kimliği (korelasyon), tarayıcı, gerekçe, Excel dışa aktarma, giriş/çıkış (`auth_events`), yapı/kişi/ilişki değişiklikleri, kişisel veri maskeleme | Kalan: kanal (web/mobil/API — mobil gelince), outbox | `audit_log` |
| **P9** | Dosya indirme | Her zaman yetki kontrollü uç nokta (`09` §3) | İmzalı, süreli bağlantı | Dosya deposu |

### Ürün ve Yazılım Dokümanı (18.10.2026) ile çelişkiler

PRD v1.2'nin yerine gelen iki doküman (`Site_Tesis_Yonetim_Platformu_Urun_Dokumani_191026`,
`…_Yazilim_Dokumani_191026`). **Karar (Furkan, 19.10.2026): mevcut API sözleşmesi kalır**;
çelişkiler burada tutulur, ürün sahibi isterse ileride tek seferlik bir `v2` sürümüyle geçilir
(tek tek uygulanırsa frontend kırılır). Yukarıdaki P1–P9 yeni dokümanda da aynen geçerli; ek
olarak:

| # | Konu | Repo (şu an geçerli) | Yazılım Dokümanı | Etkisi |
|---|---|---|---|---|
| **P10** | Para gösterimi (P4'ün API yüzü) | JSON'da metin `"1234.56"` (`06` §1.2) | `{ "amountMinor": 125050, "currency": "TRY" }` (§4.2). Repo da kayan nokta kullanmaz; amaç karşılanıyor | Tüm finans uçları, frontend |
| **P11** | Alan adları | `snake_case` | `camelCase` (§4.2) | Tüm uçlar, üretilen tipler |
| **P12** | Sayfalama ve süzgeç | `page`, `page_size`, `total`; düz parametreler | İmleç: `limit`, `cursor`, `items`, `nextCursor`; `filter[x]=`, `sort=-alan` (§4.2) | Tüm listeler |
| **P13** | Hata gövdesi | `{code (küçük harf), message, fields: {alan: mesaj}, request_id}` | `{code (BÜYÜK_HARF), message, correlationId, fields: [{field, code, message}]}` (§4.3). `request_id` 19.10.2026'da eklendi (geriye uyumlu) | Hata gösterimi |
| **P14** | Yol ve tesis belirleme | `/api/v1/sites/{slug}/…`; frontend yol bazlı (`/s/:slug`) | `/api/v1/facilities/{id}/…`; web'de alt alan adı tesisi belirler (PLT-01/02) | Yönlendirme, alan adı |
| **P15** | Dil | Kullanıcıya dönen her metin Türkçe (`AGENTS.md` kural 10) | `Accept-Language` ile TR + EN hata mesajları, 7 dil altyapısı, RTL (PLT-08) | Tüm mesajlar, frontend |
| **P16** | Eşzamanlı düzenleme | Sürüm alanı yok | `If-Match: <version>` + 409 `VERSION_CONFLICT` (§4.2) | Güncelleme uçları |
| **P17** | Idempotency-Key | İsteğe bağlı; para uçlarında | Para, durum ve dış sistem etkili her POST'ta zorunlu (§4.2) | Yazma uçları |
| **P18** | Rol modeli | Üyelik başına tek rol; açık üyelik türetilmişi ezer (`05` §4); sabit roller; kapsam = site | Aynı kapsamda birden çok rol, izinler birleşir; özel roller; blok/kapı kapsamı; süreli atama (PLT-09) | Yetki |
| **P19** | Parola ve oturum | En az 8 karakter; tek oturum süresi 8 sa kayan (`05` §8) | En az 10 + sızıntı listesi; web yönetici 12 sa, sakin 30 gün, mobil 90 gün (§4.7) | Kimlik |
| **P20** | Dosya | 10 MB; pdf/jpg/png/webp (`09` §3) | 20 MB (Excel 50 MB); heic/xlsx/csv/docx; karantina + virüs taraması; EXIF silme (S-07) | Dosya deposu |

Yazılım Dokümanı'nın kendi içindeki tutarsızlıklar (ürün sahibine iletilecek): kapsam dışı tesis
§4.1'de 403, §4.3'te 404; eski API sürüm desteği §4.2'de 6 ay, §3.5'te 12 ay; webhook yeniden
deneme S-08'de 5 deneme, PLT-11'de 24 saat; §4.2 "Hata modeli: Bkz. 2.3" (doğrusu §4.3);
açılış bakiyesi onayı Ürün §7.1'de "YK", YAP-06'da "iki kişi"; kayıt rolleri 7.2 ile YAP-07'de
farklı.

## Referans uygulamadaki bilinen hatalar (düzeltilmiş kural dokümanlarda)

Bunlar karar değil, **hata**. Yeni backend'de doğru kural uygulanır; referansı kopyalama.

| Konu | Hata | Doğru kural |
|---|---|---|
| Sıradaki tahakkuk dönemi | Ters kayıt koşuları da hesaba giriyor; eylülü 30 Eylül'de ters kaydedince sistem 30 Ekim öneriyor | `04` §7.2 |
| Gecikme tazminatı | Hesap fonksiyonu var, **hiçbir yerde çağrılmıyor** | `04` §6 |
| Ölçek | İç içe tarama ve sayfalamasız liste (kısmen düzeltildi) | `08` |
