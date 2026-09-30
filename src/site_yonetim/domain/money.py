"""Para ve kuruş kaybı olmadan dağıtım (docs/04-is-kurallari.md §1–2).

- Para her zaman `Decimal`; asla `float`.
- Yuvarlama yarım yukarı (sıfırdan uzağa): `ROUND_HALF_UP`. Python'un varsayılanı olan
  `ROUND_HALF_EVEN` yanlış sonuç verir ve bu projede yasaktır (ruff kuralıyla engelli).
- Bir tutarı paylaştırmak için **yalnız** `distribute` kullanılır; elle `toplam / adet` yazılmaz.
"""

from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")
# Karşılaştırmalarda "sıfırdan büyük" yerine kullanılan tolerans (§1).
EPSILON = Decimal("0.005")


def round_money(value: Decimal) -> Decimal:
    """Kuruşa yuvarlar: yarım yukarı (sıfırdan uzağa). 2.345 → 2.35, -2.345 → -2.35"""
    if not isinstance(value, Decimal):
        raise TypeError("Para Decimal olmalı; float ya da int kabul edilmez.")
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def is_positive(value: Decimal) -> bool:
    """Tutar sıfırdan (tolerans dahilinde) büyük mü?"""
    return value > EPSILON


def distribute(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """`total`'ı `weights` oranında paylaştırır; `sum(sonuç) == total` her zaman.

    Yöntem: en büyük kalan (largest remainder).
      1. ham[i] = total × weights[i] / Σweights
      2. pay[i] = ham[i] kuruşa **sıfıra doğru** kesilir
      3. eksik kalan kuruşlar, kesilen kısmı (mutlak değerce) en büyük olandan başlayarak
         birer birer dağıtılır; eşitlikte ağırlığı büyük olan, o da eşitse listedeki sıra.

    Negatif tutar pozitifin aynasıdır: `distribute(-x, w) == [-p for p in distribute(x, w)]`.
    """
    if not isinstance(total, Decimal):
        raise TypeError("Para Decimal olmalı; float ya da int kabul edilmez.")
    if not weights:
        return []
    if any(w < ZERO for w in weights):
        raise ValueError("Ağırlıklar negatif olamaz.")
    weight_sum = sum(weights, ZERO)
    if weight_sum <= ZERO:
        raise ValueError("Ağırlıklar toplamı sıfır veya negatif olamaz.")

    if total != round_money(total):
        raise ValueError("Dağıtılacak tutar kuruşa yuvarlanmış olmalı (en fazla 2 ondalık).")

    raw = [total * w / weight_sum for w in weights]
    result = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]

    remaining_cents = int(
        ((total - sum(result, ZERO)) / CENT).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    if remaining_cents == 0:
        return result

    # sorted kararlıdır: eşit anahtarlarda listedeki sıra korunur.
    order = sorted(
        range(len(weights)),
        key=lambda i: (abs(raw[i] - result[i]), weights[i]),
        reverse=True,
    )
    step = CENT if remaining_cents > 0 else -CENT
    for k in range(abs(remaining_cents)):
        result[order[k % len(order)]] += step
    return result
