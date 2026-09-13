# BTC Wallet Backend

Django + Django REST Framework backend for a Bitcoin/Lightning wallet, with
a BIF (Burundian Franc) exchange and point-of-sale layer on top, and a BIF
top-up rail through AmatoPay's merchant checkout API.

## Apps

- `accounts` — custom user model, registration, JWT login (`/api/auth/`)
- `wallet` — everything else, under `/api/wallet/`:
  - **Lightning wallet** (real implementation, via [Blink](https://blink.sv)'s custodial API): deposit invoices, withdrawals to a Lightning invoice/address/LNURL, real-time settlement over a WebSocket subscriber, admin-configurable withdrawal fees.
  - **On-chain Bitcoin** (real implementation, via [btclib](https://github.com/btclib-org/btclib) + blockstream.info/mempool.space): one BIP32 HD wallet for the whole platform, per-user deposit addresses, deposit scanning, and on-chain withdrawal (build → sign → locally re-verify → broadcast a real P2PKH transaction). See the security note below before pointing this at mainnet.
  - **BIF exchange**: an admin-configurable BTC↔BIF rate; convert between a user's sats and BIF ledger balances.
  - **POS charges**: quote a Lightning invoice for `amount_sats`, lock in the BIF equivalent at creation time, and credit the merchant's BIF balance (not sats) once it's paid — so the merchant isn't exposed to BTC price moves between charge and settlement.
  - **AmatoPay BIF top-up**: create an AmatoPay hosted-checkout session against a payer's mobile-money alias; poll it and credit `bif_balance` once AmatoPay reports the payment collected.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then fill in BLINK_API_KEY etc.
python manage.py migrate
python manage.py seed_withdrawal_fees
python manage.py seed_exchange_rate 150000000.00   # 1 BTC = 150,000,000 BIF, adjust to the real rate
python manage.py createsuperuser
python manage.py runserver
```

Run the Lightning payment monitor (a separate long-lived process, e.g. a systemd unit or a second container) alongside the API:

```bash
python manage.py blink_ws --backfill
```

This is what actually notices an incoming Lightning payment and credits the wallet (or settles a POS charge as BIF) in real time. A Celery task (`wallet.celery_tasks.poll_blink_invoice_update`) exists as a polling fallback if you'd rather run that on a schedule via `django-celery-beat` instead of (or in addition to) the WebSocket subscriber:

```bash
celery -A config worker -l info
celery -A config beat -l info    # if scheduling poll_blink_invoice_update periodically
```

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `BLINK_API_KEY` | Blink custodial Lightning wallet API key ([dashboard.blink.sv](https://dashboard.blink.sv)). Required for all Lightning deposit/withdraw/POS features. |
| `LND_REST_URL`, `LND_MACAROON`, `LND_CERT_PATH` | Optional direct LND node access (`wallet/lnd_service.py`), not required for the Blink-based flows. |
| `AMATOPAY_API_KEY`, `AMATOPAY_BASE_URL` | AmatoPay merchant secret key (`sk_...`) and base URL, for BIF top-ups. |
| `BITCOIN_NETWORK` | `testnet` (default) or `mainnet` — which chain the platform's HD wallet and every derived address belong to. |
| `WALLET_ENCRYPTION_KEY` | Fernet key encrypting the platform's BIP32 root xprv at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Back this up along with the database** — losing it loses every on-chain address's funds. |
| `REDIS_URL` | Celery broker, only needed if you run the polling fallback task. |

## API

Auth (`/api/auth/`): `register/`, `token/`, `token/refresh/`, `me/`.

Wallet (`/api/wallet/`), all requiring `Authorization: Bearer <access_token>`:

- `GET  /` — wallet balances (sats + BIF)
- `GET  /transactions/` — unified ledger (sats and BIF transactions both, tagged by `currency`)
- `POST /deposit/` `{amount, memo}` — create a Lightning deposit invoice
- `GET  /deposit_status/?payment_hash=` — poll a deposit's settlement status
- `POST /withdraw/decode/` `{target}` — parse a Lightning invoice/address/LNURL/Bitcoin address
- `POST /withdraw/fees/` `{target, amount}` — quote the withdrawal fee
- `POST /withdraw/` `{target, amount, memo}` — pay out over Lightning or on-chain
- `GET|POST /bitcoin/` — fetch/generate the user's on-chain deposit address
- `GET  /blink/`, `/onchain/`, `/amatopay/` — provider connectivity status
- `GET  /exchange/rate/` — current BTC→BIF rate
- `POST /exchange/quote/` `{amount_sats | amount_bif}` — convert, without moving any balance
- `POST /exchange/convert/` `{direction, amount}` — actually move sats↔BIF between the wallet's own balances (`direction` is `sats_to_bif` or `bif_to_sats`)
- `POST /pos/charge/` `{amount_sats, memo}` — create a POS charge (Lightning invoice + locked-in BIF quote)
- `GET  /pos/charge/<id>/` — poll a POS charge's status
- `POST /bif/topup/` `{amount_bif, payer_alias}` — start an AmatoPay checkout session
- `GET  /bif/topup/<session_id>/` — poll it; credits `bif_balance` once AmatoPay reports it paid

## On-chain Bitcoin: architecture and security notes

- **One HD wallet, not one key per address.** `BitcoinHDWallet` holds a single
  BIP32 root xprv (generated once, encrypted at rest with `WALLET_ENCRYPTION_KEY`).
  Every address — a user's deposit address, or an internal change address
  created during a withdrawal — is a deterministic child at
  `m/44'/<coin_type>'/0'/0/<index>`, tracked in `PlatformBitcoinAddress`. There
  is exactly one secret to back up, rotate, and restrict access to.
- **Withdrawals are verified before they're broadcast.** Building a raw P2PKH
  transaction, signing each input, and then running it back through btclib's
  own consensus script engine (`verify_transaction`) means a signing bug
  raises a local error instead of broadcasting a transaction that fails or
  (worse) spends incorrectly.
- **Coin selection is address-by-address**, scanning every `PlatformBitcoinAddress`'s
  UTXOs on each withdrawal via blockstream.info/mempool.space. Fine for a
  first version; a production deployment should maintain a local UTXO cache
  updated by the deposit scanner instead of re-querying an explorer for every
  address on every withdrawal.
- **`bolt11`** (used only for validating a *withdrawal* Lightning invoice's
  amount) depends on the native `coincurve` package, which has no prebuilt
  wheel yet for this machine's Python version (3.14 — very new). It's guarded
  with `try/except ImportError`, so that one check degrades to a clear error
  instead of crashing; nothing else (deposits, on-chain, POS, exchange) is
  affected. Run under Python 3.11/3.12 and `pip install bolt11` to enable it.

**Get an independent security review before pointing `BITCOIN_NETWORK` at
`mainnet`.** This is a standard custodial hot-wallet model (the platform
holds the one key that derives every address), which is fine for a first
version but concentrates real risk in `WALLET_ENCRYPTION_KEY` and its
backup — that's exactly why the default network is `testnet`.

Amounts are stored as integers everywhere (satoshis for BTC, whole Francs
for BIF) to avoid floating-point rounding issues.
