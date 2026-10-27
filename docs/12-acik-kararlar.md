# 12 — Açık kararlar

**Yapay zekâ için kural:** bu listedeki bir konuya dokunan bir iş gelirse **kural uydurma.**
"Şu anki davranış" sütununu uygula ya da issue'ya yazıp sor. Karar verilince madde buradan
silinir ve ilgili dokümana taşınır.

| # | Konu | Şu anki davranış | Öneri | Karar | Bloke ettiği |
|---|---|---|---|---|---|
| **K1** | **Online ödeme** | Yok. Tahsilat yönetici tarafından elle girilir | PRD bunu "kesin, MVP" diyor ve pilot hedefi koyuyor (%35 online tahsilat). Platform parayı **tutmamalı / üzerinden geçirmemeli** (6493 sayılı Kanun) — lisanslı PSP'nin pazaryeri ürünü (ör. iyzico Pazaryeri) ile para doğrudan sitenin hesabına gider. Sağlayıcı seçilmeli | Burhan + ortaklar | Online tahsilat, banka eşleştirme |
| **K2** | Gecikme tazminatı oranı ve yöntemi | Aylık %5, günlük = aylık/30, 5 gün tolerans, anaparadan (`04` §6) — **ama referansta deftere yazılmıyor** **26.10.2026 (P03a, PM):** opsiyonel, site kararı — `late_fee_enabled` (varsayılan **kapalı**), oran ≤ %5 / yöntem / tolerans finans parametresi; motor P03b. | Hukuki teyit: günlük mü aylık mı işletilir, tolerans var mı, kıst ay nasıl sayılır | Hukuk | Gecikme işi |
| **K3** | Aylık tutarın yuvarlama farkı | `round(yıllık/12)` her ay → yılda birkaç kuruş kayıp | Yıllık tutar `distribute()` ile 12 aya bölünür, farkı ilk aylar taşır (`04` §3) | Burhan | Tahakkuk |
| **K4** | Avansın sonraki borca mahsubu | Bakiye doğru, ama avans yeni borca mahsup kaydı üretmiyor | Tahakkuk kaydında hesabın avansı önce yeni borca mahsup edilir (`04` §5.1) | Burhan | Tahakkuk, mahsup dökümü |
| **K5** | Bildirim sağlayıcısı | Duyuru/kargo bildirimi **kayıt tutuluyor, gönderilmiyor**. Uygulama içi bildirim merkezi var (`06` §2.21; belge geçerlilik uyarıları da oraya düşer — `run-document-expiry`, 25.10.2026); kanal tercihleri ve push jetonları saklanıyor, dış gönderim yok | SMS (paralı, ölçeğe göre maliyet), e-posta, mobil push. Önce sağlayıcıdan bağımsız bir gönderim arayüzü + "yalnız kaydet" adaptörü | Burhan + ortaklar | Gerçek bildirim |
| **K6** | Mobil uygulama | Yok. Web önce | Tek uygulama + girişte site seçimi (ucuz) mı, müşteri başına uygulama (her müşteri için ayrı mağaza kaydı) mı | Burhan + ortaklar | Mobil |
| **K7** | Muhasebe/ERP aktarımı | Yok **26.10.2026 (P03a, PM: Kivi görüşmesi bekleniyor):** genel fiş dosyası (CSV/Excel, `AccountingExportAdapter`) yazıldı; Kivi biçimi görüşmeden sonra. | PRD ilk aday olarak "Kivi" diyor (netleşmedi). Önce sağlayıcıdan bağımsız bir muhasebe fişi dışa aktarımı; adaptörler sonra. Fiş formatı ve hesap planı mali müşavirle | Mali müşavir + ürün | ERP |
| **K8** | KVKK saklama süreleri ve metinler | **PM kararı (25.10.2026): dokümandaki varsayılanlar.** Ziyaretçi ve plaka kayıtları için site ayarı `security_settings.retention_months` (varsayılan 6, üst sınır 24); gece işi `run-security-daily` süresi dolanları anonimleştirir, ama site ayarı `auto_anonymize` **varsayılan kapalı** — hukuk onayı gelince açılır. Belge arşivi (servis isteği 78): belge başına `retention_years` (RAP-03); doküman varsayılan vermediğinden **boş** (= kanuni süre), bilgi amaçlı, **otomatik silme yok**. Diğer kategoriler (finans, log, başvuru…) hâlâ tanımsız | Hukukçu belirler: finansal kayıt, kişi, ziyaretçi, plaka, log, sakin kayıt başvurusu (bekleyen/reddedilen), banka ekstresi önizlemesi (6 saat) için ayrı süreler | Hukuk | Canlı pilot |
| **K9** | Kıst hesap (ay ortası taşınma) | Yok: tahakkuk tarihinde kim aktifse ayın tamamı ona **26.10.2026 (P03a, PM: gün bazlı):** parametre `proration = day` (varsayılan) / `month`; motor P03b. | Gün bazlı paylaştırma seçeneği | Ürün + mali müşavir | Tahakkuk |
| **K10** | Satışta devir öncesi borç | **Karar (PM, 25.10.2026): eski malikte kalır.** Malik değişimi (`ownership-transfer`) yalnız `debt_policy: stays_with_seller` kabul eder; eski malikin cari hesabı açık kalır, yeni malike yeni cari açılır, yeni malik devir tarihinden sorumlu | Borcun yeni malike devri seçeneği hukuki teyit gelirse eklenir | — | — |
| **K11** | Borçlu bölüme hizmet kısıtlaması | Yok **26.10.2026 (P03a, PM: site kararı):** `debtor_restriction_enabled` (varsayılan kapalı) + eşik tutar/gün parametresi; rezervasyon/hizmet uçları gelince uygulanır. | Rezervasyon vb. engellenebilir mi, hukuki sınırı | Hukuk | Rezervasyon |
| **K12** | Platform yöneticisinin müşteri paneline destek erişimi | Yok. Platform yöneticisinin site üyeliği yok | Ayrı yetki, süreli, müşteri onaylı, her giriş denetim kaydında | Burhan + hukuk | Destek |
| **K13** | Fiyat paketleri | Planlar var, fiyat yok | | Ortaklar | Faturalama |
| **K14** | Merkezi ısıtma tüketim bileşeni | **Karar verildi (PM, 25.10.2026): "Pilot sonrası."** Sayaç kartı ve okuma (OPS-05, istek 57) yapıldı (göç 0032): endeks, tüketim, %X sapma onayı, okunamadı, sayaç değişimi, sakin görünümü. Okumalar **tahakkuk motoruna beslenmez**; ısıtma %70 eşit + %30 m² sürer (`10-demo-veri.md` §3) | Pilot sonrası `by_meter_consumption` beslemesi + eksik okuma politikası (K23 tahmin kuralı) | Ürün | Isıtma dağıtımı |
| **K15** | Aidatta vergi, e-arşiv, tahsilat makbuzu | Yok. Site yönetimi KDV mükellefi değil kabulü. Makbuz **verisi** var (`GET …/payments/{id}`), numara yok (`receipt_number = null`) | Makbuz gerekli mi, numaralandırma kuralı | Mali müşavir | Makbuz |
| **K16** | Excel aktarımında aynı kişinin birden çok bölümü | **Karar (PM, 25.10.2026): ad + telefon aynıysa tek kişi**, önizlemede `person_merges` ile gösterilir; sitede kayıtlı aynı kişi (ad + telefon) yeniden açılmaz; kişi birden çok bölüme bağlanır. Telefonu olmayan ya da yalnız e-postası aynı satır birleştirilmez | — | — | — |
| **K17** | Hisseli mülkiyette (birden çok aktif malik) borç kime | Tek kişiye: hissesi büyük, eşitse başlangıcı eski olan (`04` §4.5). **PM 25.10.2026: bugünkü gibi kalsın** | Hisse oranında bölmek (her malike `distribute` ile pay) — ama her malikin ayrı cari hesabı ve ayrı tahsilatı gerekir | Burhan + ürün | Tahakkuk |
| **K18** | Talep durum geçişleri ve sakinin yetkileri | **Karar (PM, 25.10.2026 — "dokümandaki akış"; uygulandı, göç 0031):** Açık → Atandı → İşlemde → Beklemede → Çözüldü → Kapandı · İptal geçiş tablosu (`domain/operations.TRANSITIONS`, tablo dışı 409 `invalid_transition`); atama açık talebi "Atandı" yapar; kapandı/iptal yeniden açılmaz (dokümanda yeniden açma yok); sakin kendi talebini iş başlamadan (açık/atandı) iptal eder; "Çözüldü"den sonra sakin onayı + 1–5 puan, `auto_close_days` (varsayılan 3) yanıtsızsa `run-request-jobs` kapatır; puan ≤ eşik (varsayılan 2) yöneticiye bildirim; sakin açtıysa personel doğrudan kapatamaz (409 `awaiting_confirmation`); durum değişince sakine bildirim | — | Burhan + ürün | — |
| **K19** | Portföyde site "sağlık durumu" | Hesaplanmıyor; ham ölçüler dönüyor (tahsilat oranı, açık bakiye, geciken talep) | Örn. tahsilat oranı ≥ %90 ve 60+ gün borç yok → iyi; %70–90 → dikkat; altı → sorunlu. Eşikler ürün kararı | Burhan + ürün | Portföy rozeti |
| **K20** | Rol sırası (yetki devri ve eskalasyon için "aynı veya üst rol") | **Karar verildi, uygulandı (PM kararı 25.10.2026; P16a, 26.10.2026).** Rol sırası **veridir**: her rolün düzeyi var (1 en üst), varsayılan Yönetici (1) > YK üyesi (2) > Muhasebe = Denetçi = İK sorumlusu (3) > Teknik = Güvenlik = Görevli (4); site şablonun düzeyini ve özel rolün düzeyini değiştirir (PM notu: "site yönetimlerinin veya sistem yöneticisinin belirleyeceği yetki matrisine göre"). Yetki devri: devralan aynı ya da daha üst düzeyde bir role sahip olmalı (ONY-03 adım 2). Eskalasyon hedefi kuralda yine açıkça seçilir; rol sırası yalnız **öneri** verir (`GET …/approval-rules/escalation-suggestion`) — otomatik seçim yok | `05` §3–§4.2 | PM | — |
| **K33** | M'ye ulaşılamayan adım | "Ret hesaba sayılır"da rettler M'yi imkânsız kılsa da talep beklemede kalır (otomatik ret yok, 6.3); ekranda `quorum_unreachable` uyarısı; talep eden geri çekebilir, eskalasyon hedefi karar verebilir | Yazılım Dokümanı'nda yazılmamış: otomatik ret mi, yöneticiye uyarı mı | Ürün | Onay |
| **K34** | Yetki devrinin anlamı (K20'ye bağlı) | Devir yalnız aynı role ve kişi bir talepte bir kez karar verdiği için, devralan çoğu zaman zaten kendi hakkıyla listede; devir yalnız devralanın hakkı yokken işe yarar (ör. role adım açıldıktan sonra katıldı) | Rol sırası (K20) belirlenince üst rol, alt rolün hakkını vekâleten kullanır; ya da "kişi" tipli onaycı adımı | Ürün | Yetki devri |
| **K21** | Mevcut akışların onay mekanizmasına bağlanması | Bağlı değil: kayıt başvurusu, iade, tahakkuk, rol değişikliği doğrudan yetkiyle (Furkan, 19.10.2026: "şimdilik bağlama"; 24.10.2026: ortakların K20/K21 kararından sonra bağlanır, önce kayıt başvurusu; Burhan'a önceden haber verilir, frontend aynı anda çıkar; 25.10.2026: **yeni modüller** — satın alma, rezervasyon, tadilat vb. — ilk günden onay mekanizmasıyla yazılır, bekleme yalnız mevcut akışlar için; yeni işlem türleri o modülle eklenir) | Yazılım Dokümanı 6.1 "hiçbir modül kendi onay mantığını yazmaz". Önce kayıt başvurusu, sonra iade/tahakkuk; frontend akışı "onaya gönderildi" (202) olur. **25.10.2026 (P02):** yeni işlem "malik değişimi" (`ownership_transfer`) ilk günden bağlı (kural varsa 202); kayıt başvurusunun bağlanması P16 paketinde, P02 başvuru akışına dokunmadı | Furkan + Burhan | Onay kapsamı |
| **K22** | Finans sorumlusu modeli | Bölüm × kişi × tür (malik/oturan) cari hesabı; ödeyeni borç türü belirler (`04` §4.5, KMK m.22). **PM 25.10.2026: malik ve oturan ayrı kalsın (bugünkü).** Hisse toplamının tam %100 olması günlük ekleme akışında ara durum olarak serbest; canlıya geçiş kontrol listesinde zorunlu madde (D-19) | Yazılım Dokümanı YAP-04: bölümde tarih aralıklı tek "finans sorumlusu" bayrağı ve hisselerin tam %100 olması zorunlu | Ürün + mali müşavir | Cari, tahakkuk |
| **K23** | Tahakkuk kaynağı | İşletme projesinden (KMK m.37) **26.10.2026 (P03a, PM: site seçer):** parametre `charge_source = budget_plan` (varsayılan) / `billing_rule`; aidat kuralları ve motor P03b. | Yazılım Dokümanı FIN-02: versiyonlu aidat kurallarından; bütçeden aidat türetmek Faz 2 | Ürün | Tahakkuk, bütçe |
| **K24** | Mahsup sırası | Aynı vadede önce gecikme tazminatı (TBK m.100, `04` §5.1) **26.10.2026 (P03a, PM: mevcut kalır):** `allocation_order = late_fee_first` (varsayılan) / `principal_first` parametre olarak gösterilir. | Yazılım Dokümanı §8.7: önce ana borç, sonra gecikmesi | Hukuk | Tahsilat |
| **K25** | Vade ve dönem varsayılanları | Kesim ayın 1'i, vade +14 gün, dönem = takvim ayı (`04` §7.2) **26.10.2026 (P03a, PM: varsayılan doküman):** `due_day = 10` parametresi; motor/takvim P03b. | Yazılım Dokümanı: vade ayın 10'u, dönem başından 5 gün önce taslak, özel dönem tanımları | Ürün | Tahakkuk |
| **K26** | Talep yorumu ile iç not | **Karar (PM, 25.10.2026; uygulandı, göç 0031):** iç not ayrı, değişmez kayıt (`request_internal_notes`, `/requests/{id}/internal-notes`, `requests.assign`); talep yanıtında, olay geçmişinde, sakin uçlarında ve bildirimlerde hiç dönmez. Yorumlar sakine görünmeye devam eder | — | Ürün | — |
| **K27** | Adlandırma: "incidents" | `incidents` = güvenlik olay kaydı — **karar (Furkan, 24.10.2026): ad değişmez**; belgedeki "ana olay" `outages` adıyla yapıldı (25.10.2026, göç 0031) | Yazılım Dokümanı'nda `incidents` = talepleri toplayan "ana olay" (HIZ-02); güvenlik olayı "nöbet defteri" | Burhan + Furkan | Talep, güvenlik |
| **K28** | Demirbaş mı teknik varlık mı | **Karar verildi (PM, 25.10.2026): "Ayrı listeler."** Uygulandı (göç 0032): `assets` mali demirbaş (bedel, zimmet) olarak kalır; `technical_assets` (kod `TV-0001`, kategori, marka/model/seri no, garanti, bakım sözleşmesi, sorumlu firma) + yasal muayene takvimi (60/30/7 gün uyarısı, rapor zorunlu, olumsuz → düzeltme talebi) + periyodik bakım planı (planlanan tarihte talep/iş emri) + zaman çizelgesi ve yıllık maliyet. İş emri = talep modülündeki talep; malzeme maliyeti iş emri–stok bağı gelince | — | Ürün | — |
| **K29** | Personel–kullanıcı bağı | **Karar verildi (PM, 25.10.2026): "Bağlansın, ayrılınca erişim kapansın."** Uygulandı (göç 0029): kart bir giriş hesabına bağlanır (`staff_members.user_id`); ayrılış tarihi geçince bağlı hesabın site üyeliği pasifleşir (gece `run-staff-jobs` + erişim çözümlemesi bir sonraki istekte düşürür). Personel kendi vardiyasını, izin talebini ve QR/konumlu giriş-çıkışını `/sites/{slug}/me/*` ile kullanır. Kart yeniden açılırsa erişim kendiliğinden açılmaz (Kullanıcılar ekranından). Belgedeki "aynı telefonla otomatik eşleştirme" yapılmadı: kullanıcı kaydında telefon yok — eşleşme elle seçilir | — | Ürün | — |
| **K30** | Otopark modeli | **PM kararı (25.10.2026): ayrı kaynak, bölüme tarihçeli tahsis (OTP-01).** `parking_spaces` + `parking_allocations` (göç 0030); `parking` bölüm tipi mevcut sitelerde kalır. Açık: ücretli yerin dönem dışı ücrete bağlanması, ilişki bitince tahsisin kendiliğinden bitmesi, bölüm başına plaka sınırı (araç tablosu P02 ile) | Yazılım Dokümanı OTP-01: park yeri ayrı kaynak, bölüme tarihçeli tahsis | Ürün | Otopark |
| **K31** | Anket gizliliği | Bölüm başına bir oy, kim verdiği tutulmaz | Yazılım Dokümanı ILT-03: anket bazında "anonim mi" ve "bölüm başına tek yanıt mı" seçeneği | Ürün + hukuk | Anket |
| **K32** | Sakine gider şeffaflığı | Gerçekleşen giderler tedarikçi adı ve fatura görüntüsüyle açık | Yazılım Dokümanı RAP-02: kişi bilgisi içermeyen, onaylı özet | Ürün + hukuk | Şeffaflık |

### Yapı, kişiler ve kayıt paketi (P02, 25.10.2026) — uygulanan varsayımlar
| Konu | Şu anki davranış | Bekleyen |
|---|---|---|
| Davetle kayıtta giriş | Giriş e-posta + parolayla (P5 telefonla giriş SMS sağlayıcısına bağlı): davette e-posta yoksa kabulde istenir; geçici parola bir kez gösterilir | P5 gelince telefon doğrulamasıyla giriş |
| Hane üyesi yetkileri | `visitors/packages/reservations/requests` saklanır; hane üyesi "oturan" ilişkisiyle eklenir (cari yok, borç görmez). Yetkilerin ilgili uçlarda tek tek uygulanması sonraki adım | Sakin uçlarında yetki süzgeci; davet edenin ilişkisi bitince üyeliğin bitmesi |
| Malik onayı / belge (kayıt yöntemleri) | `owner_can_approve_tenant`, `owner_document_required` yalnız saklanır | Ortak kararı (YAP-07) |
| Özel alanların sakin tarafı | `resident_can_view/edit` saklanır; sakin uçları yok | Sakin profil ekranı |
| Açılış bakiyesi aktarımı onayı | Mutabakat (toplam tutmalı) zorunlu; onay kuralına bağlı değil (doküman "varsayılan iki kişi" der) | K21 sırası: finans işlemleri onaya bağlanırken |
| İletişim görünürlüğü (D-22) | Yeni izin `people.contact` (yalnız Yönetici): liste/ayrıntıda maskeli, açma denetimli; YK ve diğerleri null. Muhasebe iletişim görmez | Muhasebenin borç takibi için açma hakkı ürün kararı |

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
| **P18** | Rol modeli | **PM kararı (25.10.2026): ilk sürümde. Uygulandı (P16a, 26.10.2026):** rol ataması kişi × rol × kapsam (tesis / blok; kapı GUV-08 ile) × (başlangıç–bitiş) (`site_role_assignments`); izinler birleşir; siteye özel rol = şablon kopyası + izin seti (`site_roles`); atayan kendi izninden fazlasını atayamaz/tanımlayamaz/kaldıramaz (403). Açık üyelik türetilmişi ezmeye devam eder (D-38 açık). Kritik rol atamasında onay (K21 4. adım) P16b'de | Aynı kapsamda birden çok rol, izinler birleşir; özel roller; blok/kapı kapsamı; süreli atama (PLT-09) | Yetki |
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
