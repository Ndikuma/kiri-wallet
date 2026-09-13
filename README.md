# BTC Wallet Backend

Django + Django REST Framework backend for a Bitcoin/Lightning wallet app. This is the initial scaffold: user accounts (JWT auth), wallet balances, and deposit/withdrawal request tracking.

## Apps

- `accounts` — custom user model, registration, JWT login (`/api/auth/`)
- `wallet` — `Wallet` and `Transaction` models, balance + deposit/withdrawal endpoints (`/api/wallet/`)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## API

- `POST /api/auth/register/` — `{username, email, password}`
- `POST /api/auth/token/` — `{username, password}` → `{access, refresh}`
- `POST /api/auth/token/refresh/` — `{refresh}` → `{access}`
- `GET  /api/auth/me/` — current user
- `GET  /api/wallet/` — current user's wallet balance
- `GET  /api/wallet/transactions/` — current user's transaction history
- `POST /api/wallet/deposit/` — `{amount_sats, memo}` — creates a pending deposit
- `POST /api/wallet/withdraw/` — `{amount_sats, lightning_invoice | onchain_address}` — reserves balance and creates a pending withdrawal

All wallet/auth endpoints except register/token require `Authorization: Bearer <access_token>`.

## Not yet wired up (next steps)

- Real Lightning node integration (LND/Blink or similar) to actually issue invoices and pay out withdrawals, and to mark deposits confirmed on payment received.
- On-chain Bitcoin address generation and confirmation tracking.
- Admin-configurable withdrawal fees.

Amounts are stored as integer satoshis everywhere to avoid floating-point rounding issues.
