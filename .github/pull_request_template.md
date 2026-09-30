## Ne değişti

<!-- Kısa özet. İlgili issue: Closes #… · Trello kartı: … -->

## Nasıl test edildi

<!-- Eklenen/çalıştırılan testler -->

## Bitti tanımı (AGENTS.md)

- [ ] Uç nokta `06-api-sozlesmesi.md` kurallarına uyuyor (hata biçimi, sayfalama, para metni)
- [ ] Yetki kontrolü var ve testi var (izinli yapabiliyor, izinsiz yapamıyor)
- [ ] Tenant izolasyon testi var (başka sitenin kullanıcısı 404 alıyor)
- [ ] Finansal işlemse: tekrar gönderimde çift kayıt yok (idempotent), testi var
- [ ] Hata mesajları Türkçe ve kullanıcıya ne yapacağını söylüyor
- [ ] Liste döndürüyorsa sayfalı; toplama veritabanında
- [ ] Sır, kişisel veri ya da `.env` commit'e girmedi
- [ ] Uygulanamayan maddeler için gerekçe yazıldı
