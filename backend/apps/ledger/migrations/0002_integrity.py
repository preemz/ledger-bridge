"""Database level integrity: the rules that must hold whatever writes the data.

Django constraints cover per-row rules. Three of the guarantees this ledger needs span
rows or span time, so they are expressed in the database:

1. A journal entry must balance. Enforced by a constraint trigger that is
   DEFERRABLE INITIALLY DEFERRED, so the check runs once at COMMIT. That lets a load
   insert debits and credits in any order, inserts, updates and deletes included.
2. The audit log is append only. A BEFORE UPDATE OR DELETE trigger refuses the write.
3. Account balances are readable without aggregating the journal on every request,
   through a materialized view that is refreshed concurrently after a load.

Reverse of the whole migration is a drop, which is correct: these objects are defined
here and nowhere else.
"""

from __future__ import annotations

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION ledger_check_entry_balanced() RETURNS trigger AS $$
DECLARE
    target_entry bigint;
    total_debit numeric(20,4);
    total_credit numeric(20,4);
BEGIN
    target_entry := COALESCE(NEW.entry_id, OLD.entry_id);

    SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
      INTO total_debit, total_credit
      FROM ledger_journalline
     WHERE entry_id = target_entry;

    IF total_debit <> total_credit THEN
        RAISE EXCEPTION
            'journal entry % is not balanced: debits % <> credits %',
            target_entry, total_debit, total_credit
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_journalline_balanced ON ledger_journalline;
CREATE CONSTRAINT TRIGGER ledger_journalline_balanced
    AFTER INSERT OR UPDATE OR DELETE ON ledger_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION ledger_check_entry_balanced();

CREATE OR REPLACE FUNCTION ledger_deny_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_auditevent is append-only'
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_auditevent_append_only ON ledger_auditevent;
CREATE TRIGGER ledger_auditevent_append_only
    BEFORE UPDATE OR DELETE ON ledger_auditevent
    FOR EACH ROW
    EXECUTE FUNCTION ledger_deny_mutation();

DROP MATERIALIZED VIEW IF EXISTS ledger_account_balance;
CREATE MATERIALIZED VIEW ledger_account_balance AS
SELECT
    a.id                                              AS account_id,
    a.code                                            AS code,
    a.name                                            AS name,
    a.type                                            AS type,
    a.normal_balance                                  AS normal_balance,
    a.currency                                        AS currency,
    COALESCE(SUM(posted.debit), 0)::numeric(20,4)     AS total_debit,
    COALESCE(SUM(posted.credit), 0)::numeric(20,4)    AS total_credit,
    (COALESCE(SUM(posted.debit), 0)
        - COALESCE(SUM(posted.credit), 0))::numeric(20,4) AS net_debit,
    COUNT(posted.id)                                  AS line_count
FROM ledger_account a
LEFT JOIN (
    SELECT jl.id, jl.account_id, jl.debit, jl.credit
      FROM ledger_journalline jl
      JOIN ledger_journalentry e ON e.id = jl.entry_id
     WHERE e.status = 'POSTED'
) posted ON posted.account_id = a.id
GROUP BY a.id, a.code, a.name, a.type, a.normal_balance, a.currency;

-- A unique index is what makes REFRESH MATERIALIZED VIEW CONCURRENTLY legal, and the
-- concurrent form is what keeps the operator console readable during a load.
CREATE UNIQUE INDEX ledger_account_balance_account_id_idx
    ON ledger_account_balance (account_id);
CREATE INDEX ledger_account_balance_code_idx ON ledger_account_balance (code);
"""

REVERSE = """
DROP MATERIALIZED VIEW IF EXISTS ledger_account_balance;
DROP TRIGGER IF EXISTS ledger_auditevent_append_only ON ledger_auditevent;
DROP FUNCTION IF EXISTS ledger_deny_mutation();
DROP TRIGGER IF EXISTS ledger_journalline_balanced ON ledger_journalline;
DROP FUNCTION IF EXISTS ledger_check_entry_balanced();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
