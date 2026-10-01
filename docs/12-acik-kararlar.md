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
| **K5** | Bildirim sağlayıcısı | Duyuru/kargo bildirimi **kayıt tutuluyor, gönderilmiyor** | SMS (paralı, ölçeğe göre maliyet), e-posta, mobil push. Önce sağlayıcıdan bağımsız bir gönderim arayüzü + "yalnız kaydet" adaptörü | Burhan + ortaklar | Gerçek bildirim |
| **K6** | Mobil uygulama | Yok. Web önce | Tek uygulama + girişte site seçimi (ucuz) mı, müşteri başına uygulama (her müşteri için ayrı mağaza kaydı) mı | Burhan + ortaklar | Mobil |
| **K7** | Muhasebe/ERP aktarımı | Yok | PRD ilk aday olarak "Kivi" diyor (netleşmedi). Önce sağlayıcıdan bağımsız bir muhasebe fişi dışa aktarımı; adaptörler sonra. Fiş formatı ve hesap planı mali müşavirle | Mali müşavir + ürün | ERP |
| **K8** | KVKK saklama süreleri ve metinler | Yok | Hukukçu belirler: finansal kayıt, kişi, ziyaretçi, plaka, log için ayrı süreler | Hukuk | Canlı pilot |
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

## PRD ile repo dokümanları arasındaki çelişkiler

**Karar (Furkan, 30.09.2026):** çelişkide **repo `docs/` geçerlidir**. PRD
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
| **P8** | Denetim kaydı | `09` §6 şeması | + aktör tipi, kanal, korelasyon id, gerekçe; outbox ile aynı transaction; giriş/çıkış ve doküman erişimi | `audit_log` |
| **P9** | Dosya indirme | Her zaman yetki kontrollü uç nokta (`09` §3) | İmzalı, süreli bağlantı | Dosya deposu |

## Referans uygulamadaki bilinen hatalar (düzeltilmiş kural dokümanlarda)

Bunlar karar değil, **hata**. Yeni backend'de doğru kural uygulanır; referansı kopyalama.

| Konu | Hata | Doğru kural |
|---|---|---|
| Sıradaki tahakkuk dönemi | Ters kayıt koşuları da hesaba giriyor; eylülü 30 Eylül'de ters kaydedince sistem 30 Ekim öneriyor | `04` §7.2 |
| Gecikme tazminatı | Hesap fonksiyonu var, **hiçbir yerde çağrılmıyor** | `04` §6 |
| Ölçek | İç içe tarama ve sayfalamasız liste (kısmen düzeltildi) | `08` |
