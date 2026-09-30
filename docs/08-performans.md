# 08 — Performans ve ölçek

**Kural: kullanıcının açtığı sayfanın hızı, toplam veri miktarından bağımsız olmalı.**
100 daire de olsa 10.000 daire de olsa aynı hızda açılır.

Hedef: web uç noktaları **p95 < 300 ms**, mobil **p95 < 200 ms**, 10.000 bölümlü bir sitede.

## 1. Ölçülmüş gerçek — bu kurallar neden var

Referans uygulamada 29 Eylül 2026'da ölçüldü (SQLite, 24 aylık hareket, 7 tekrarın ortancası):

| Sayfa | 48 daire | 2.000 daire | 2.000 daire, düzeltme sonrası | 10.000 daire, düzeltme sonrası |
|---|---|---|---|---|
| Yönetici panosu | 19 ms | **5.943 ms** | 950 ms | 1.403 ms |
| Borçlular | 15 ms | **4.770 ms** | 359 ms | 2.301 ms · **8,8 MB sayfa** |
| Daireler | 13 ms | 308 ms | 340 ms | 1.146 ms · **9,5 MB sayfa** |

İki ayrı sorun çıktı:

1. **İç içe tarama (N × M).** Her cari hesap için bütün hareket listesi baştan taranıyordu:
   2.000 hesap × 90.000 hareket = 180 milyon karşılaştırma. Daire sayısı 42 kat arttığında süre
   300 kat arttı. `dict`/gruplama ile düzeltilince 6 saniye → 1 saniye.
2. **Her şeyi belleğe çekip uygulamada toplamak** ve **sayfalamasız liste.** Düzeltmeden sonra
   bile 10.000 dairede borçlular 2,3 saniye ve 8,8 MB. Bunun çözümü aşağıdaki §2–§3.

## 2. Özet bakiye tablosu — en büyük kazanç

Pano ve borçlular her açılışta 450.000 hareketi **toplamamalı.** Bankaların yaptığı gibi:

- `ledger_entries` **tek doğruluk kaynağıdır** (değişmez hareket defteri).
- Yanında `account_balances(account_id, site_id, debit_total, credit_total, balance,
  oldest_open_due_date, updated_at)` tablosu tutulur.
- Bir hareket yazıldığında **aynı transaction içinde** ilgili satır güncellenir
  (`UPDATE … SET balance = balance + :debit - :credit`).
- Özet tablo defterden her an yeniden üretilebilir: `rebuild_balances(site_id)` fonksiyonu ve
  gece çalışan bir **mutabakat işi** (özet ile defter toplamı tutuyor mu; tutmuyorsa alarm).
- Aynısı kasa için: `cash_balances(cash_account_id, inflow_total, outflow_total, balance)`.
- Site düzeyinde pano sayıları için: `site_finance_summary(site_id, period, charged, collected, …)`.

Pano artık birkaç satır okur; veri büyüdükçe yavaşlamaz.

## 3. Yasaklar ve zorunluluklar

| # | Kural |
|---|---|
| 3.1 | **Döngü içinde koleksiyon taraması yasak.** `for a in accounts: [e for e in entries if e.account_id == a.id]` → önce `dict`'e grupla (`defaultdict(list)`), ya da daha iyisi veritabanında `GROUP BY` |
| 3.2 | **Toplama veritabanında.** `SUM`, `COUNT`, `GROUP BY` SQL'de; Python'a ham satır taşıyıp orada toplama |
| 3.3 | **Her liste sayfalı** (`06-api-sozlesmesi.md` §1.3). 10.000 satır tek yanıtta dönmez |
| 3.4 | **N+1 yok.** İlişkili veri tek sorguda (`selectinload` / `joinedload`) |
| 3.5 | **İndeks** — her kiracı tablosunda `site_id`; ayrıca `(site_id, account_id, date)` hareketler, `(site_id, cash_account_id, date)` kasa, `(site_id, date)` gider, `(site_id, status)` talepler |
| 3.6 | **Ağır iş arka planda** — 1.000+ bölümde tahakkuk kaydı, toplu bildirim, büyük Excel (`02-mimari.md` §6) |
| 3.7 | **Önbellek** — pano sayıları kısa süre (ör. 60 sn) önbellekte tutulabilir; yazma işlemi ilgili önbelleği temizler |
| 3.8 | **Sakin uç noktaları** yalnız o kişinin verisini okur — birkaç yüz satır, toplam veri büyüklüğünden bağımsız |

Her yeni liste/rapor uç noktası için bir **yük testi** önerilir: 10.000 bölüm, 24 ay hareket
(≈ 450.000 satır) üret, p95'i ölç.

## 4. Veritabanı stratejisi — neden tek veritabanı

Bir bölüm yılda yaklaşık **120 satır** üretir (12 tahakkuk + ~60 kalem satırı + 24 cari hareket
+ 12 tahsilat + ~15 mahsup).

| Ölçek | Yılda | 5 yılda |
|---|---|---|
| 10.000 bölüm | 1,2 milyon satır | 6 milyon |
| 100.000 bölüm | 12 milyon | 60 milyon |
| 500.000 bölüm | 60 milyon | **300 milyon** (≈ 60–120 GB) |

Tek bir PostgreSQL sunucusu bunu rahat taşır. Karar: **tek veritabanı + `site_id`.**

Ama şu yapılmalı ki ileride kurumsal bir müşteri "verimiz ayrı dursun" derse ayrılabilsin:
**hangi sitenin hangi veritabanına gideceğini çözen kod tek bir yerde olsun** (site → bağlantı
dizesi). Bugün hepsi aynı veritabanını döner; yarın bir müşteri kendi veritabanına taşınınca bu
ayar değişikliği olur, yeniden yazım değil.

Büyüdükçe sırayla:
1. İndeksler (baştan)
2. Özet tablolar (§2 — baştan)
3. `ledger_entries`, `charges`, `charge_lines` için **yıla göre bölümleme (partition)**
4. Kapanmış dönem salt okunur, özet tablosuna düşülür
5. Raporlar için **okuma kopyası (read replica)**
6. Gerekirse büyük müşteriyi kendi veritabanına ayırma

## Referans

`brhnnkaraa6/siteyonetimi` → commit `94abf1d` ("Pano, borçlular, daireler ve portföyde iç içe
taramayı kaldır") — N × M düzeltmesinin kendisi.
