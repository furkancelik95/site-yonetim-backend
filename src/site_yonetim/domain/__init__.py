"""SAF iş kuralları (docs/02-mimari.md §2).

Bu paket veritabanına, HTTP'ye ve saate DOKUNMAZ: `sqlalchemy`, `fastapi`, `starlette`,
`datetime.now()` burada kullanılmaz. Tarih gerekiyorsa parametre olarak gelir.
Kural `tests/architecture/` altında testle korunur.
"""
