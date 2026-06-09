# Cheats Game Checkout Handoff

## Workspace paths

- Provided workspace root: `/Users/mac/Documents/Codex/2026-06-09/continue-existing-cheats-game-workspace-do`
- Active backend source: `/Users/mac/Documents/Codex/2026-06-08/https-github-com-cheatsadmin-cheatsgame-api`
- Active frontend source: `/Users/mac/Documents/Codex/2026-06-08/cheatsgame_frontend-main`

The provided workspace root was empty and not a git repo. The running Nuxt process pointed to the frontend source above, and the Django backend source was found beside it.

## Runtime state

- Django backend was restarted on `127.0.0.1:8000`.
- Nuxt frontend was already running on `localhost:4174`.
- Final seeded checkout rows:
  - `Order 1`: `schedule_id=1`, physical order.
  - `Order 2`: `schedule_id=None`, physical order.
  - `DeliveryData 1`: `type_id=2`, `schedule_id=2`, `address_id=1`, `is_used=False`.
  - `DeliverySchedule 2`: order schedule, capacity `7`, start `2026-06-15 14:00:00+00:00`.

## Investigation result

This is primarily a backend/data issue, not a frontend payload issue.

`SubmitOrderApi` creates order rows with `schedule=None` by design. The frontend then continues in `pages/BasketDateSelect.vue` and does the expected two-step request:

1. `POST /api/shop/book-time/`
2. `PUT /api/shop/order-detail/{id}/` with `{ "schedule": bookTimeResp.id }`

The failing seeded flow selected address `1` and schedule `2`, but `DeliveryData 1` for that same address/schedule already existed and was already attached to `Order 1`. Before the fix, `POST /api/shop/book-time/` tried to create the same `(address_id, schedule_id)` row again and crashed:

`UNIQUE constraint failed: shop_deliverydata.address_id, shop_deliverydata.schedule_id`

Because `book-time` returned HTTP 500, the frontend never reached `PUT /api/shop/order-detail/2/`, so `Order 2` stayed `schedule:null`.

## Backend changes made

- `cheatgame/shop/apis/delivery_schedule.py`
  - Reuses an existing unassigned `DeliveryData` for the same address/schedule.
  - Rejects an already-used or already-attached `DeliveryData` with HTTP 400 instead of crashing with HTTP 500.
  - Catches duplicate insert `IntegrityError` and returns HTTP 400.

- `cheatgame/shop/apis/cart.py`
  - Rejects attaching a `DeliveryData` already attached to another order.

- `cheatgame/shop/services/order.py`
  - Marks `DeliveryData.is_used=True` when an order schedule is attached.
  - Avoids clearing an existing schedule during discount-only updates.
  - Releases an old schedule if an order is moved and no other order still references it.

- `cheatgame/shop/tests.py`
  - Added focused checkout scheduling tests for duplicate booking, already-attached booking rejection, order schedule assignment, and `is_used` marking.

Pre-existing local backend changes were left intact:

- `config/django/base.py` had CORS changes before this handoff.
- `cheatgame/shop/apis/cart.py` already had unrelated `IsBoughtProductAPIView` schema edits.

## Verification

Passed:

- `.venv/bin/python manage.py test cheatgame.shop`
- `.venv/bin/python manage.py test`
- `.tools/node20/bin/node .tools/npm/bin/npm-cli.js run test`
- `.tools/node20/bin/node .tools/npm/bin/npm-cli.js run lint`
- `.tools/node20/bin/node node_modules/nuxi/bin/nuxi.mjs prepare`

Notes:

- Full backend tests initially failed because `Faker` and `factory-boy` were declared but missing from the venv.
- `pip install -r requirements_dev.txt` failed because `requirements/base.txt` contains invalid syntax: `django-js-asset=2.1.0`; it should use `==`.
- Installed the missing declared packages directly: `Faker==15.1.1` and `factory-boy==3.2.1`.
- Frontend `test` and `lint` scripts are placeholders that echo `test` and `lint`.
- `nuxi prepare` passed with existing duplicate-import warnings.
- Live API repro now returns HTTP 400 for the stale seeded slot instead of HTTP 500.

## Remaining notes

- `Order 2` still has `schedule:null` because the only seeded order delivery slot for address `1` is already represented by `DeliveryData 1` and attached to `Order 1`.
- Existing seeded `DeliveryData 1` still has `is_used=False`; future order updates will mark delivery data used, but existing rows may need a one-off cleanup if the local seed should be fully consistent.
- To manually complete `Order 2`, seed another order schedule/delivery slot or clear the conflicting existing order relationship, then submit the checkout flow again.
