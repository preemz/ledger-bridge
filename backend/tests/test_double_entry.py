"""The database must not accept books that do not balance.

These are database level tests on purpose. An application level check is a convention;
a deferred constraint trigger is a guarantee, and the difference matters when the write
comes from somewhere other than the service that was supposed to validate it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.ledger.models import Account, AuditEvent, JournalEntry, JournalLine


@pytest.fixture
def accounts(db):
    cash = Account.objects.create(code="1000", name="Cash", type="ASSET")
    revenue = Account.objects.create(code="4000", name="Revenue", type="REVENUE")
    return cash, revenue


@pytest.mark.django_db(transaction=True)
def test_a_balanced_entry_commits(accounts):
    cash, revenue = accounts
    with transaction.atomic():
        entry = JournalEntry.objects.create(source_system="t", external_ref="E-BAL", entry_date="2026-01-31")
        JournalLine.objects.create(entry=entry, account=cash, debit=Decimal("100.0000"))
        JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("100.0000"))
    assert JournalLine.objects.filter(entry=entry).count() == 2


@pytest.mark.django_db(transaction=True)
def test_an_unbalanced_entry_is_rejected_at_commit(accounts):
    cash, revenue = accounts
    with pytest.raises(IntegrityError) as excinfo:
        with transaction.atomic():
            entry = JournalEntry.objects.create(
                source_system="t", external_ref="E-UNBAL", entry_date="2026-01-31"
            )
            JournalLine.objects.create(entry=entry, account=cash, debit=Decimal("100.0000"))
            JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("99.0000"))

    assert "is not balanced" in str(excinfo.value)
    # The transaction rolled back, so neither the entry nor its lines survived.
    assert JournalEntry.objects.filter(external_ref="E-UNBAL").count() == 0


@pytest.mark.django_db(transaction=True)
def test_a_line_cannot_be_both_a_debit_and_a_credit(accounts):
    cash, _ = accounts
    entry = JournalEntry.objects.create(source_system="t", external_ref="E-BOTH", entry_date="2026-01-31")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            JournalLine.objects.create(
                entry=entry, account=cash, debit=Decimal("10.0000"), credit=Decimal("10.0000")
            )


@pytest.mark.django_db(transaction=True)
def test_the_audit_log_is_append_only(accounts):
    event = AuditEvent.objects.create(event_type="test", message="original")

    with pytest.raises(IntegrityError) as excinfo:
        with transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).update(message="tampered")
    assert "append-only" in str(excinfo.value)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AuditEvent.objects.filter(pk=event.pk).delete()

    event.refresh_from_db()
    assert event.message == "original"


@pytest.mark.django_db
def test_normal_balance_is_derived_from_the_account_type():
    asset = Account.objects.create(code="1010", name="Bank", type="ASSET")
    liability = Account.objects.create(code="2010", name="Loan", type="LIABILITY")
    revenue = Account.objects.create(code="4010", name="Fees", type="REVENUE")

    assert asset.normal_balance == "DEBIT"
    assert liability.normal_balance == "CREDIT"
    assert revenue.normal_balance == "CREDIT"
