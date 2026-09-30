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
| **K15** | Aidatta vergi, e-arşiv, tahsilat makbuzu | Yok. Site yönetimi KDV mükellefi değil kabulü | Makbuz gerekli mi, numaralandırma kuralı | Mali müşavir | Makbuz |

## Referans uygulamadaki bilinen hatalar (düzeltilmiş kural dokümanlarda)

Bunlar karar değil, **hata**. Yeni backend'de doğru kural uygulanır; referansı kopyalama.

| Konu | Hata | Doğru kural |
|---|---|---|
| Sıradaki tahakkuk dönemi | Ters kayıt koşuları da hesaba giriyor; eylülü 30 Eylül'de ters kaydedince sistem 30 Ekim öneriyor | `04` §7.2 |
| Gecikme tazminatı | Hesap fonksiyonu var, **hiçbir yerde çağrılmıyor** | `04` §6 |
| Ölçek | İç içe tarama ve sayfalamasız liste (kısmen düzeltildi) | `08` |
