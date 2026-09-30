---
name: Servis isteği
about: Frontend'in bir ekran için ihtiyaç duyduğu uç nokta
title: "[Servis] "
labels: servis-istegi
---

## Ekran
<!-- Hangi ekran, kim kullanıyor (rol) -->

## Uç nokta
<!-- Örn. GET /api/v1/sites/{slug}/debtors — docs/06-api-sozlesmesi.md kataloğundaki satır -->

## İstek
<!-- Parametreler / gövde. Örnek JSON -->
```json
```

## Beklenen yanıt
<!-- Örnek JSON. Para metin ("1234.56"), tarih "YYYY-MM-DD", liste sayfalı -->
```json
```

## İş kuralları
<!-- Bu ekranda geçerli kurallar. docs/04-is-kurallari.md'deki ilgili bölümü belirt -->
- İlgili bölüm: `04-is-kurallari.md` §

## Hata durumları
<!-- Hangi durumda hangi kod ve Türkçe mesaj -->
| Durum | Kod | Mesaj |
|---|---|---|
| | | |

## Yetki
<!-- Gereken izin (docs/05-yetki.md §2). Hangi rol görmemeli? -->

## Kabul kriterleri
- [ ] Yanıt yukarıdaki örnekle aynı şekilde
- [ ] Yetkisi olmayan rol 403, başka sitenin kullanıcısı 404 alıyor
- [ ] Liste ise sayfalı
- [ ] İlgili test senaryosu (`docs/07-test-senaryolari.md` §) geçiyor
