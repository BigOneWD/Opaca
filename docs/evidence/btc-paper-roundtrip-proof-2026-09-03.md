# REAL ALPACA PAPER CRYPTO EXECUTION PROOF

Date: 2026-09-03

THIS IS NOT AN OPTIONS/WHEEL EXECUTION PROOF.

This artifact records one tightly bounded Alpaca PAPER crypto round trip. The
authorized scope was one approximately `$10.00` BTC BUY followed, only after
confirmed fill, by one SELL of the attributable BTC quantity. No Wheel,
options, equity, SGOV, account-configuration, cancellation, replacement,
exercise, reset, or retry mutation was performed.

## Precheck

- repository branch: `feat/wheel-competition-mode`
- clean repository and `git diff --check`: PASS before execution
- fresh software test precheck: `852 passed, 3 skipped`
- PAPER endpoint exact match: `https://paper-api.alpaca.markets`
- PAPER account status: `ACTIVE`
- PAPER crypto status: `ACTIVE`
- sanitized account fingerprint: `a1facbe1522d`
- installed `alpaca-py`: `0.33.0`
- broker-recognized BTC order symbol: `BTC/USD`
- asset status/tradable: `active` / `true`
- asset fractionable: `true`
- minimum order size: `0.000012924 BTC`
- minimum trade increment: `0.000000001 BTC`
- requested `$10.00` notional: valid above the observed minimum at the
  read-only BTC quote
- crypto MARKET TIF used: `GTC` (the crypto-supported choices are `GTC` and
  `IOC`)
- preexisting BTC position: `NO`
- preexisting BTC open order: `NO`

The broker’s position readback uses the legacy symbol spelling `BTCUSD`; the
order requests used the current recognized pair symbol `BTC/USD`.

## BUY

- requested notional: `$10.00`
- client order id:
  `opaca-btc-smoke-6174eb144ac5-buy`
- broker order id (sanitized suffix): `...922874c954cf`
- submit attempts: `1`
- final status: `FILLED`
- filled quantity: `0.000126189 BTC`
- average fill price: `$77,729.338`
- no retry was performed

## SELL

- client order id:
  `opaca-btc-smoke-6174eb144ac5-sell`
- broker order id (sanitized suffix): `...0fa3587e880c`
- submit attempts: `1`
- final status: `FILLED`
- sell quantity: `0.000125873 BTC`
- filled quantity: `0.000125873 BTC`
- average fill price: `$77,671.08`

The BUY order’s gross filled quantity was `0.000126189 BTC`. Immediately
after that fill, the only BTC position returned by the broker was
`0.000125873 BTC`; that exact attributable position quantity was aligned to
the broker’s `1e-9` increment and was used for the single SELL. No preexisting
BTC was mixed into the round trip.

## Final readback

- final BTC position: absent / zero
- unresolved BTC orders: `NO` (`0` open BTC orders)
- total broker mutation submit attempts: `2`
- SGOV mutations: `NONE`
- option mutations: `NONE`
- automatic retries: `NONE`
- real option orders submitted: `NONE`

## Result

`BTC_PAPER_ROUNDTRIP_PROOF = PASS`
