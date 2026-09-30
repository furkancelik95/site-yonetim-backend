"""İşletme projesi (bütçe) durum geçişleri — saf (docs/04 §3, KMK m.37).

draft ──tebliğ──> notified ──7 gün itiraz süresi──> finalized ──yenisi kesinleşince──> superseded
"""

from datetime import date, timedelta

from site_yonetim.domain.finance import BudgetStatus, FinanceRuleError
from site_yonetim.domain.text import format_date_tr

OBJECTION_DAYS = 7


def ensure_editable(status: BudgetStatus) -> None:
    """Kalemler yalnız taslakta değişir; tebliğ edilen proje sakinlere gönderilmiş belgedir."""
    if status is not BudgetStatus.DRAFT:
        raise FinanceRuleError(
            "budget_not_editable",
            "Yalnız taslak işletme projesinin kalemleri değiştirilebilir. Kesinleşmiş proje "
            "değiştirilemez; değişiklik gerekiyorsa yeni proje hazırlayın.",
            conflict=True,
        )


def objection_deadline(notified_on: date) -> date:
    return notified_on + timedelta(days=OBJECTION_DAYS)


def check_notify(status: BudgetStatus, item_count: int, notified_on: date, today: date) -> date:
    """Tebliğ: taslaktan, en az bir kalemle, ileri tarih olmadan. İtiraz son günü döner."""
    if status is not BudgetStatus.DRAFT:
        raise FinanceRuleError(
            "budget_not_draft", "Yalnız taslak işletme projesi tebliğ edilebilir.", conflict=True
        )
    if item_count == 0:
        raise FinanceRuleError(
            "budget_empty",
            "Kalemi olmayan işletme projesi tebliğ edilemez. Önce kalem ekleyin.",
            conflict=True,
        )
    if notified_on > today:
        raise FinanceRuleError(
            "notified_in_future", "Tebliğ tarihi ileri bir tarih olamaz.", field="notified_on"
        )
    return objection_deadline(notified_on)


def check_finalize(status: BudgetStatus, deadline: date | None, today: date) -> None:
    """Kesinleşme: tebliğden sonra, 7 günlük itiraz süresi **dolduktan** sonra (son gün hariç)."""
    if status is not BudgetStatus.NOTIFIED or deadline is None:
        raise FinanceRuleError(
            "budget_not_notified",
            "İşletme projesi kesinleşmeden önce kat maliklerine tebliğ edilmeli.",
            conflict=True,
        )
    if today <= deadline:
        raise FinanceRuleError(
            "objection_period_open",
            f"İtiraz süresi {format_date_tr(deadline)} günü doluyor; proje ertesi gün "
            "kesinleştirilebilir.",
            conflict=True,
        )
