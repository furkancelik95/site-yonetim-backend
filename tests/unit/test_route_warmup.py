"""Yönlendirme ısıtması (test raporu #6): her sürecin ilk site isteği FastAPI'nin tembel
yönlendirme kurulumunu ödemesin."""

from site_yonetim.main import create_app, warm_routes


def test_yonlendiriciler_acilista_kurulur() -> None:
    app = create_app()
    # 0 dönerse FastAPI iç yapısı değişmiştir: ısıtma artık çalışmıyor, yeniden bakılmalı.
    assert warm_routes(app) > 50
    assert warm_routes(app) > 50  # tekrar güvenli (FastAPI kendi önbelleğini kullanır)
