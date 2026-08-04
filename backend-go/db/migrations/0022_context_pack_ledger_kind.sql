ALTER TABLE go_run_ledger_entries
    DROP CONSTRAINT IF EXISTS go_run_ledger_entries_entry_kind_check;

ALTER TABLE go_run_ledger_entries
    DROP CONSTRAINT IF EXISTS ck_go_run_ledger_entries_entry_kind;

ALTER TABLE go_run_ledger_entries
    ADD CONSTRAINT ck_go_run_ledger_entries_entry_kind
    CHECK (entry_kind IN ('MODEL','CAPABILITY','LOCAL_TRANSITION','LOCAL_DERIVATION'));
