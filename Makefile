SHELL := /bin/bash
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: install migrate seed api worker web temporal test crash-check console-check shell explain reset

install:
	python3 -m venv .venv
	$(PIP) install --upgrade pip -q
	$(PIP) install -q -r backend/requirements.txt
	cd frontend && npm install

migrate:
	cd backend && ../$(PY) manage.py migrate

seed:
	cd backend && ../$(PY) manage.py seed_legacy --accounts 240 --entries 4000

api:
	cd backend && ../$(PY) manage.py runserver 0.0.0.0:8000

worker:
	cd backend && ../$(PY) manage.py run_worker

temporal:
	temporal server start-dev --db-filename /tmp/lb_temporal.db

# Kills the worker mid-load and checks the ledger for duplicates. Needs temporal, the
# api and a seeded database. See scripts/crash_resume_check.py.
crash-check:
	$(PY) scripts/crash_resume_check.py

# Drives the console in a headless Chrome. Needs puppeteer-core installed ad hoc, see
# the header of scripts/console_check.cjs.
console-check:
	node scripts/console_check.cjs

web:
	cd frontend && npm run dev

test:
	cd backend && ../$(PY) -m pytest -q

shell:
	cd backend && ../$(PY) manage.py shell

# Prints the plan for the reconciliation variance query, for the README.
explain:
	cd backend && ../$(PY) manage.py explain_reconcile

# The audit table refuses DELETE at the database level, which is the point of it, so a
# reset has to drop the database rather than flush the rows.
reset:
	dropdb --if-exists ledger_bridge
	createdb ledger_bridge
	$(MAKE) migrate
	$(MAKE) seed
