# Food Delivery System API

FastAPI + SQLAlchemy + MySQL food ordering backend with JWT role checks, restaurant discovery, menus, coupons, cart and checkout, order handling, delivery partner offers, OTP delivery confirmation, WebSockets, APScheduler jobs, ratings, and reports.

## Setup (Windows PowerShell)

1. Install Python 3.10 or newer and MySQL 8.
2. In MySQL Workbench, run:

   ```sql
   CREATE DATABASE food_delivery_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   ```

3. Open this project folder in VS Code and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements-dev.txt
   Copy-Item .env.example .env
   ```

4. Edit `.env`. Set `DATABASE_URL` to your MySQL username, password, host, and database. If the password includes URL characters such as `@`, encode them (`@` becomes `%40`). Change `JWT_SECRET_KEY` and the admin password before sharing/deploying.
5. Create tables only through Alembic:

   ```powershell
   python -m alembic upgrade head
   python -m scripts.seed_demo
   python -m uvicorn app.main:app --reload
   ```

6. Open <http://127.0.0.1:8000/docs>. `GET /` is the health check. The default `.env.example` uses MySQL; without a `.env`, local development falls back to `food_delivery.db` (SQLite).

### Alembic commands

```powershell
python -m alembic current
python -m alembic history
python -m alembic revision --autogenerate -m "describe_schema_change"
python -m alembic upgrade head
python -m alembic downgrade -1
```

Never use `Base.metadata.create_all()` to set up the application database. Model changes belong in versioned migrations.

## Demo users and sample data

Run `python -m scripts.seed_demo` after migrating. All demo accounts use **`FoodDemo@123`**.

| Role | Email | Notes |
|---|---|---|
| Admin | `admin@example.com` | Can approve users, manage platform coupons, view platform orders/reports |
| Restaurant Owner | `owner@example.com` | Approved; owns the seeded restaurant |
| Customer | `customer@example.com` | Has a saved default address |
| Delivery Partner | `partner@example.com` | Approved, online, with a demo scooter |

The seed script is idempotent. For a fresh database, create the demo records before capturing screenshots. The seed admin uses the same demo password; set a private password before using a deployed instance.

### Test locations and coupons

- Demo restaurant `Hyderabad Demo Kitchen`: latitude `17.4486`, longitude `78.3908`; delivery radius 5 km; open `00:00`–`23:59` for repeatable testing.
- Demo partner: latitude `17.4500`, longitude `78.3900`.
- Customer default address in Jubilee Hills: latitude `17.4550`, longitude `78.3950`, pincode `500033`.
- Menu: Veg Biryani ₹180, Chicken Biryani ₹240, Masala Dosa ₹110.
- `WELCOME50`: platform wide, 50% off up to ₹100, minimum item total ₹199, first delivered order only.
- `BIRYANI20`: restaurant-specific flat ₹20 off, minimum ₹100.

## Swagger smoke-test sequence

1. Use `POST /auth/login` with a demo account. Copy `access_token`, click **Authorize**, and enter `Bearer <token>` if Swagger does not add the prefix automatically.
2. As customer: `GET /restaurants/nearby?latitude=17.4550&longitude=78.3950`; `GET /restaurants/{restaurant_id}/menu`; `POST /cart/items` with `{"menu_item_id":1,"quantity":1}`; optionally `POST /cart/apply-coupon` with `{"code":"BIRYANI20"}`; `POST /orders` with `{"address_id":1,"payment_method":"Online","tip_amount":10}`.
3. Save the returned `order.id` and `delivery_otp`. The OTP is visible only to the order's customer. Online payment is a mock marked Paid.
4. As owner: accept with `PUT /orders/{id}/accept` and `{"estimated_preparation_minutes":25}`; progress with `/preparing`, then `/ready`.
5. As partner: check `GET /partners/me/offers`, accept `PUT /offers/{offer_id}/accept`, then use `PUT /orders/{id}/pickup` and `/out-for-delivery`.
6. As delivery partner, finish with `PUT /orders/{id}/deliver` and `{"otp":"<customer-visible 4-digit OTP>"}`. A wrong OTP is rejected; after three incorrect attempts verification locks.
7. As customer, optionally submit `POST /orders/{id}/rating` with restaurant_rating, partner_rating, and comment.

Role-specific authorization is required for each action. Swagger login tokens expire after 30 minutes.

## WebSocket live tracking

WebSockets are not listed in Swagger. Use a current order ID and access tokens from each relevant role. The test page at <http://127.0.0.1:8000/tracking-test> can connect to the order stream. Enter order ID and customer access token, then connect. It displays status and partner location messages.

Browser DevTools Console example:

```javascript
const ws = new WebSocket("ws://127.0.0.1:8000/ws/orders/1?token=PASTE_ACCESS_TOKEN");
ws.onmessage = event => console.log("Live order update", JSON.parse(event.data));
```

For an end-to-end location test: connect as the customer; log in as the delivery partner in a second Swagger session; call `PUT /partners/me/location` repeatedly with changing latitude/longitude values (or send a JSON location over `/ws/orders/{id}` as that order's assigned partner). Messages include current status, coordinates, distance remaining, and estimated arrival time. Also test the restaurant and partner channels at `/ws/restaurant?token=...` and `/ws/partner?token=...`.

## Screenshot checklist for submission

Create a `screenshots/` folder locally and save real browser captures (do not edit response values):

1. `01-login-customer.png` — successful login and authorized Swagger session.
2. `02-nearby-restaurants.png` — nearby result with distance and ETA.
3. `03-cart-coupon.png` — cart lines, coupon discount, and total.
4. `04-order-created.png` — successful order response with amount breakdown (hide OTP in any public copy).
5. `05-restaurant-order-flow.png` — owner accepts and marks the order ready.
6. `06-partner-offer.png` — partner offer with distance and expected earnings.
7. `07-live-tracking.png` — tracking page connected as customer, showing a location update.
8. `08-delivery-confirmed.png` — successful OTP-verified delivery.
9. `09-reports.png` — dashboard or partner earnings response.

For API screenshots, keep the Swagger endpoint, request body, HTTP status, and response visible. For tracking, show a customer tracking tab and a partner location update; take two captures with different partner coordinates so movement is evident. No real screenshots are fabricated or included in this source ZIP.

## Automated checks

```powershell
python -m pytest -q
```

## Main API groups

- Authentication and admin approval: `/auth/*`, `/admin/*`
- Restaurants, menu, nearby search: `/restaurants/*`, `/menu-items/*`
- Customer address and cart: `/addresses/*`, `/cart*`
- Coupons: `/coupons/*`
- Orders and restaurant operations: `/orders/*`, `/restaurants/me/orders`
- Partner availability, offers, delivery, earnings: `/partners/*`, `/offers/*`
- Live tracking and alerts: `/ws/orders/{order_id}`, `/ws/restaurant`, `/ws/partner`
- Rating and reports: `/orders/{order_id}/rating`, `/reports/*`

## Assumptions and implementation notes

- Online payment is intentionally mocked; checkout records it as Paid. There is no real payment gateway or refund provider.
- Distance is straight-line Haversine distance. ETA assumes travel speed 20 km/h. Delivery fees are ₹20 for the first 3 km plus ₹8 for every started extra km; peak hours apply a 1.5 multiplier.
- Restaurant hours use the server's local time. Store/host timezone should be set consistently for production.
- Offer expiry and order auto-rejection are handled by an in-process APScheduler. Run one API worker for this demo; multiple workers would each start a scheduler unless moved to a separate worker service.
- Offer retry skips partners who previously received, declined, or expired an offer. Retry after timeout is scheduler-driven. Admin alert delivery, real push notifications, and external messaging are not configured.
- Order OTP is customer-visible only through the customer order response. It is required to mark delivery complete; three incorrect attempts lock verification.
- Database uses SQLAlchemy sync sessions for broad local compatibility. MySQL 8 is the intended database; SQLite fallback is for quick evaluation only.
- Demo credentials and seed data are for local evaluation. Change passwords and secrets before deployment.
- GitHub publishing requires the user's GitHub repository/account. This ZIP contains the repository-ready source, README, migrations, and tests.
