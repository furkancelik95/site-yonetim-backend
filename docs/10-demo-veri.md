# 10 — Demo verisi ve site kurulumu

İki ayrı şey var:
- **§1–2 Demo verisi:** yalnız **geliştirme ortamında** yüklenir. Ürünü göstermek ve rol rol
  denemek için. Üretimde **asla** oluşmaz.
- **§3 Yeni site kurulumu:** platform panelinden site açılınca her ortamda kurulan varsayılanlar.

Seed komutu idempotent olmalı: veri varsa hiçbir şey yapmaz.

> **Uygulama durumu (30.09.2026):** `uv run python -m site_yonetim.cli seed-demo` ya da
> geliştirmede açılışta otomatik. Yüklenenler: planlar, şirket, 3 site, bloklar, daire tipleri,
> 84 bölüm, modüller, §2'deki hesaplar (sakin dahil), işletme projesi, son 6 ayın tahakkuku ve
> tahsilatı (FIFO'dan geçer), site başına 4 duyuru (biri sabit, biri süresi geçmiş) ve farklı
> durum/öncelikte 6 talep, Banka ve Kasa hesapları (tahsilat paranın girdiği hesaba yazılır),
> son 6 ayın giderleri (bu ayın ~üçte biri ödenmemiş). Hesapların açılışı "3 ay önce" yerine
> **demo geçmişinin başında**: tahsilat/gider hareketleri ekstrede açılıştan sonra görünsün.
> **Henüz yok:** kargo/ziyaretçi. Site
> kurulumunda (§3) bugün kurulanlar: site, modüller, gecikme politikası, gider kategorileri,
> tahakkuk tipleri, dağıtım kuralları, daire tipleri, kasa hesapları
> (`services/provisioning.py`).

---

## 1. Demo verisi

### 1.1 Planlar
`01-urun.md` → Planlar tablosu (Başlangıç 30 / Standart 150 / Pro sınırsız / Yönetim Şirketi sınırsız).

### 1.2 Yönetim şirketi
**Kent Yönetim A.Ş.** · VKN `1234567890` · plan: Yönetim Şirketi. Üç sitenin hepsi bu şirkette.

### 1.3 Siteler
| | Aksu Konakları | Yıldız Sitesi | Mimoza Apartmanı |
|---|---|---|---|
| slug | `aksu-konaklari` | `yildiz-sitesi` | `mimoza-apartmani` |
| Yer | İstanbul / Beykoz | Ankara / Çankaya | İzmir / Karşıyaka |
| Tür | `mixed` | `residential` | `residential` |
| Plan | Pro | Standart | Başlangıç |
| Bloklar (asansör, kat) | A (var, 8) · B (var, 8) · C (yok, 4) | A (var, 6) · B (yok, 5) | tek blok, adsız (yok, 4) |
| Blok başına daire | 16 → **48** | 12 → **24** | 12 → **12** |
| Ek modüller | reservations, visitors, packages, valet | visitors | — |
| Aylık bütçe | 486.000 | 148.000 | 38.500 |
| Tahsilat oranı | %94 | %78 | %61 |
| IBAN (örnek) | TR33 0006 1005 1978 6457 8413 26 | TR62 0001 0002 2233 4455 6677 88 | TR11 0011 1000 0000 0012 3456 78 |

Toplam **84 bağımsız bölüm**. Oranlar bilinçli farklı: panoda sağlıklı, orta ve sorunlu site görünsün.

### 1.4 Her sitede
- **Daireler:** 1+1 / 2+1 / 3+1 karışık, gerçekçi m² ve arsa payı; bir kısmı kiracılı.
  Kişi adları Türkçe, rastgele ama sabit tohumla (her kurulumda aynı veri).
- **İşletme projesi** (`finalized`), yıllık tutar = aylık bütçe × 12, sekiz kalem:

  | Kalem | Pay | Dağıtım | Tip |
  |---|---|---|---|
  | Personel Giderleri (kapıcı, güvenlik) | %32 | eşit | Aidat |
  | Merkezi Isıtma | %24 | bileşik (%70 eşit — tüketim yerine, §3 notu — + %30 m²) | Aidat |
  | Ortak Alan Elektrik ve Su | %11 | brüt m² | Aidat |
  | Temizlik ve Bahçe Bakımı | %9 | eşit | Aidat |
  | Yönetim Hizmet Bedeli | %8 | daire tipi ağırlığı | Aidat |
  | Sigorta ve Diğer | %6 | arsa payı | Aidat |
  | Demirbaş ve Yatırım Katılım Payı | %6 | arsa payı | **Demirbaş (malik)** |
  | Asansör Bakım Sözleşmesi | %4 | eşit, **yalnız asansörlü bloklar** | Aidat |

- **Son 6 ayın tahakkuku** motordan geçirilerek kaydedilir (elle tutar yazılmaz).
- **Tahsilat:** eski aylar daha çok kapanmış — bu ay oranın ~%45'i, geçen ay ~%90'ı, daha
  eskiler oran + %4. Tahsilat tarihi ayın 2–25'i. ~%25'i nakit (Kasa'ya), gerisi havale (Banka'ya).
  Her tahsilat cari hesaba **ve** kasaya yazılır.
- **Kasa/banka:** "Banka Hesabı" (açılış = aylık bütçe × 1,4) ve "Kasa" (açılış 2.500), 3 ay önce açılmış.
- **Giderler:** son 6 ay, kalem bazında (kapıcı maaşı, doğalgaz, elektrik, temizlik, asansör,
  bahçe, su deposu, bir kez kamera yenileme — demirbaş). Bu ayın faturalarının ~üçte biri
  **ödenmemiş**; ödenenler kasadan düşmüş.
- **Duyurular**, **talepler** (farklı durum ve önceliklerde), güvenlik modülü açık sitelerde
  **kargo ve ziyaretçi** kayıtları.

## 2. Demo hesapları

Hepsinin parolası **`Demo1234!`** (giriş ekranında yazılı; yalnız geliştirme). Adlar kurgusaldır.

| E-posta | Ad | Rol | Ne görür |
|---|---|---|---|
| `platform@demo.local` | Deniz Aksoy | **Platform yöneticisi** | Müşteri ekler, site açar, plan atar. Site üyeliği **yok** |
| `yonetici@demo.local` | Kerem Yıldırım | Kent Yönetim — **Sahip** | Üç sitenin tamamı, portföy |
| `muhasebe@demo.local` | Selin Arı | Kent Yönetim — **Muhasebe** | Üç sitede finans; modül ve kullanıcı yönetimi yok |
| `mimoza@demo.local` | Hakan Tunç | Mimoza — **Yönetici** (açık üyelik) | Yalnız Mimoza, portföy yok |
| `guvenlik@demo.local` | Recep Er | Aksu — **Güvenlik** | Yalnız ziyaretçi ve kargo |
| `denetci@demo.local` | Nuray Şen | Aksu — **Denetçi** | Salt okunur finans |
| `teknik@demo.local` | Ergün Kılıç | Aksu — **Teknik Personel** | Yalnız talepler |
| `sakin@demo.local` | *(Aksu'da en borçlu oturan hesabın kişisi)* | Aksu — **Sakin** | Kendi dairesi |

Sakin hesabı bilinçli olarak borçlu bir hesaba bağlanır ki sakin ekranı boş görünmesin.

## 3. Yeni site kurulumu (her ortamda)

`POST /platform/sites` bir site açınca **tek transaction** içinde:

1. **Site** — ad 3–120 karakter; `slug` verilmediyse addan üretilir (`04` §11), en az 3 karakter; ad ve slug benzersiz.
2. **Modüller** — `ModuleKeys` listesinin tamamı için satır. Açık başlayan: `finance`
   (çekirdek) + **planın izin verdiği** `announcements`, `requests`, `documents`. Diğerleri
   (kargo, vale, rezervasyon…) kapalı başlar; site kullanacaksa yönetici açar.
3. **Gecikme tazminatı politikası** — aylık %5, 5 gün tolerans, asgari 0, açık.
4. **Gider kategorileri** — "İşletme Giderleri" (`operating`), "Demirbaş ve Yatırım" (`capital_improvement`).
5. **Tahakkuk tipleri** — "Aidat" (`occupant`, "KMK m.20 — işletme gideri, oturan öder"),
   "Demirbaş Katılım Payı" (`owner`, "KMK m.22 — demirbaş/yatırım, malik öder").
6. **Dağıtım kuralları** — Eşit Paylaşım · Arsa Payı · Brüt Metrekare · Daire Tipi Ağırlığı ·
   Merkezi Isıtma: bileşik, **%70 `equal` + %30 `by_area`**.
   > Dikkat: kuralın adı "Merkezi Isıtma (%70 tüketim + %30 m²)" ama tüketim bileşeni **şimdilik
   > `equal`** — sayaç okuma özelliği yok, okuma girilene kadar tüketim payı eşit dağıtılıyor.
   > Sayaç modülü gelince bileşen `by_meter_consumption` yapılır. Adı yanıltıcı; ekranda
   > "tüketim payı sayaç okuması girilene kadar eşit dağıtılır" notu gösterilmeli.
7. **Daire tipleri** — 1+1 (1,0) · 2+1 (1,35) · 3+1 (1,7).
8. **Kasa hesapları** — "Banka Hesabı" (`bank`, sitenin IBAN'ı ile) · "Kasa" (`cash`).

Sonraki adım (kullanıcıya söylenir): daire listesini Excel'den aktarmak.

## Referans

`brhnnkaraa6/siteyonetimi`:
- Demo: `src/SiteYonetimi.Infrastructure/Persistence/DemoDataSeeder.cs`, `DemoUserSeeder.cs`
- Site kurulumu: `src/SiteYonetimi.Infrastructure/Provisioning/SiteProvisioningService.cs`
- Müşteri kurulumu: `src/SiteYonetimi.Infrastructure/Provisioning/CustomerProvisioningService.cs`
