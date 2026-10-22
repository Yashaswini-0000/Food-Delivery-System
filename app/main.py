from contextlib import asynccontextmanager
from datetime import datetime, timedelta, time
from math import ceil
from secrets import randbelow
from typing import Any
import asyncio

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import FastAPI, Request, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth import current_user, require_roles
from app.config import settings
from app.database import Base, SessionLocal, engine, get_db
from app.models import (Address, Cart, CartItem, Coupon, CouponUsage, DeliveryOffer,
    DeliveryPartner, FoodOrder, MenuCategory, MenuItem, OrderItem, OrderStatusHistory,
    Rating, Restaurant, User)
from app.utils.geo import haversine_km
from app.utils.security import create_access_token, hash_password, verify_password, decode_token

connections: dict[int, set[WebSocket]] = {}
scheduler = BackgroundScheduler(timezone="UTC")


async def broadcast(order_id: int, payload: dict):
    stale = []
    for ws in list(connections.get(order_id, set())):
        try:
            await ws.send_json(payload)
        except Exception:
            stale.append(ws)
    for ws in stale:
        connections.get(order_id, set()).discard(ws)


def set_status(db: Session, order: FoodOrder, status: str, actor: str, note: str | None = None):
    old = order.status
    order.status = status
    db.add(OrderStatusHistory(order_id=order.id, old_status=old, new_status=status, changed_by=actor, note=note))


def ensure_cart(db: Session, customer_id: int) -> Cart:
    cart = db.query(Cart).filter_by(customer_id=customer_id).first()
    if not cart:
        cart = Cart(customer_id=customer_id)
        db.add(cart)
        db.flush()
    return cart


def cart_lines(db: Session, cart: Cart):
    rows = db.query(CartItem, MenuItem).join(MenuItem, CartItem.menu_item_id == MenuItem.id).filter(CartItem.cart_id == cart.id).all()
    return rows


def coupon_discount(db: Session, coupon: Coupon, customer_id: int, restaurant_id: int, item_total: float) -> float:
    now = datetime.utcnow()
    if not coupon.is_active:
        raise HTTPException(400, "This coupon is inactive")
    if now < coupon.valid_from or now > coupon.valid_to:
        raise HTTPException(400, "This coupon has expired or is not active yet")
    if coupon.restaurant_id and coupon.restaurant_id != restaurant_id:
        raise HTTPException(400, "This coupon is not valid for this restaurant")
    if item_total < coupon.min_order_value:
        raise HTTPException(400, f"Minimum order value is ₹{coupon.min_order_value:g}")
    count = db.query(CouponUsage).filter_by(coupon_id=coupon.id).count()
    used_by_customer = db.query(CouponUsage).filter_by(coupon_id=coupon.id, customer_id=customer_id).count()
    if coupon.total_usage_limit is not None and count >= coupon.total_usage_limit:
        raise HTTPException(400, "This coupon has reached its usage limit")
    if used_by_customer >= coupon.per_user_limit:
        raise HTTPException(400, "You have reached this coupon's usage limit")
    if coupon.first_order_only and db.query(FoodOrder).filter_by(customer_id=customer_id, status="Delivered").count():
        raise HTTPException(400, "This coupon is only for your first delivered order")
    amount = item_total * coupon.discount_value / 100 if coupon.discount_type.lower() == "percentage" else coupon.discount_value
    if coupon.discount_type.lower() == "percentage" and coupon.max_discount is not None:
        amount = min(amount, coupon.max_discount)
    return round(min(amount, item_total), 2)


def restaurant_is_open(restaurant: Restaurant, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    current = now.strftime("%H:%M")
    open_time, close_time = restaurant.opening_time, restaurant.closing_time
    in_window = open_time <= current <= close_time if open_time <= close_time else (current >= open_time or current <= close_time)
    return bool(restaurant.is_active and not restaurant.is_temporarily_closed and in_window)


def owner_restaurant(db: Session, restaurant_id: int, owner_id: int) -> Restaurant:
    r = db.get(Restaurant, restaurant_id)
    if not r:
        raise HTTPException(404, "Restaurant not found")
    if r.owner_id != owner_id:
        raise HTTPException(403, "You can manage only your own restaurants")
    return r


def assign_nearest_partner(db: Session, order: FoodOrder):
    restaurant = db.get(Restaurant, order.restaurant_id)
    now = datetime.utcnow()

    offer_rows = (
        db.query(
            DeliveryOffer.partner_id,
            DeliveryOffer.status,
            DeliveryOffer.expires_at,
        )
        .filter(DeliveryOffer.order_id == order.id)
        .all()
    )

    already_offered = {
        partner_id
        for partner_id, status, expires_at in offer_rows
        if status == "Declined"
        or (status == "Pending" and expires_at and expires_at > now)
    }

    active_ids = {
        partner_id
        for (partner_id,) in (
            db.query(FoodOrder.delivery_partner_id)
            .filter(
                FoodOrder.delivery_partner_id.isnot(None),
                FoodOrder.status.in_(["Picked Up", "Out for Delivery"]),
            )
            .all()
        )
    }

    candidates = (
        db.query(DeliveryPartner)
        .filter_by(is_online=True, is_approved=True)
        .all()
    )

    candidates = [
        partner
        for partner in candidates
        if partner.user_id not in active_ids
        and partner.id not in already_offered
        and partner.current_latitude is not None
        and partner.current_longitude is not None
        and haversine_km(
            restaurant.latitude,
            restaurant.longitude,
            partner.current_latitude,
            partner.current_longitude,
        ) <= 5
    ]

    candidates.sort(
        key=lambda partner: haversine_km(
            restaurant.latitude,
            restaurant.longitude,
            partner.current_latitude,
            partner.current_longitude,
        )
    )

    if not candidates:
        return None

    partner = candidates[0]
    db.add(
        DeliveryOffer(
            order_id=order.id,
            partner_id=partner.id,
            status="Pending",
            offered_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    db.commit()
    return partner


def scheduled_jobs():
    db = SessionLocal()
    now = datetime.utcnow()
    try:
        # Restaurant non-response after five minutes.
        pending = db.query(FoodOrder).filter(FoodOrder.status == "Placed", FoodOrder.placed_at <= now - timedelta(minutes=5)).all()
        for order in pending:
            set_status(db, order, "Rejected", "System", "Restaurant did not respond")
            order.rejection_reason = "Restaurant did not respond"
            if order.payment_method == "Online":
                order.payment_status = "Refunded"
            db.query(CouponUsage).filter_by(order_id=order.id).delete()
        offers = db.query(DeliveryOffer).filter(DeliveryOffer.status == "Pending", DeliveryOffer.expires_at <= now).all()
        retry_ids = set()
        for offer in offers:
            offer.status = "Expired"
            retry_ids.add(offer.order_id)
        db.query(Coupon).filter(Coupon.is_active.is_(True), Coupon.valid_to < now).update({Coupon.is_active: False})
        partners = db.query(DeliveryPartner).filter(DeliveryPartner.is_online.is_(True), DeliveryPartner.last_location_at.isnot(None), DeliveryPartner.last_location_at < now - timedelta(minutes=10)).all()
        for p in partners:
            p.is_online = False
        db.commit()
        for oid in retry_ids:
            order = db.get(FoodOrder, oid)
            if order and order.status in ("Accepted", "Preparing", "Ready for Pickup") and not order.delivery_partner_id:
                assign_nearest_partner(db, order)
    except Exception:
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.start_scheduler:
        if not scheduler.running:
            scheduler.add_job(scheduled_jobs, "interval", seconds=10, id="food-jobs", replace_existing=True)
            scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {"name": settings.app_name, "docs": "/docs", "health": "ok"}


@app.get("/tracking-test", include_in_schema=False)
def tracking_test_page():
    return FileResponse("static/tracking_test.html")


@app.post("/auth/register", status_code=201)
def register(data: dict = Body(...), db: Session = Depends(get_db)):
    role = str(data.get("role", "Customer")).title()
    if role not in {"Customer", "Restaurant Owner", "Delivery Partner"}:
        raise HTTPException(422, "Public registration supports Customer, Restaurant Owner, or Delivery Partner")
    if db.query(User).filter(or_(User.email == data.get("email", "").lower(), User.phone == data.get("phone", ""))).first():
        raise HTTPException(409, "Email or phone is already registered")
    phone = str(data.get("phone", ""))
    if len(phone) != 10 or not phone.isdigit():
        raise HTTPException(422, "Phone number must contain 10 digits")
    if len(data.get("password", "")) < 8:
        raise HTTPException(422, "Password must contain at least 8 characters")
    user = User(name=data.get("name", "").strip(), email=data["email"].lower(), phone=phone,
                password_hash=hash_password(data["password"]), role=role, is_active=True,
                is_approved=(role == "Customer"))
    db.add(user); db.flush()
    if role == "Delivery Partner":
        vehicle_number = data.get("vehicle_number")
        if not vehicle_number:
            raise HTTPException(422, "vehicle_number is required for delivery partners")
        db.add(DeliveryPartner(user_id=user.id, vehicle_type=data.get("vehicle_type", "Bike"), vehicle_number=vehicle_number, is_approved=False))
    db.commit(); db.refresh(user)
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "is_approved": user.is_approved}


@app.post("/auth/login")
async def login(request: Request, db: Session = Depends(get_db)):
    if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        form = await request.form()
        email = form.get("username", form.get("email", ""))
        password = form.get("password", "")
    else:
        data = await request.json()
        email = data.get("email", "")
        password = data.get("password", "")

    user = db.query(User).filter(func.lower(User.email) == str(email).lower()).first()
    if not user or not verify_password(str(password), user.password_hash) or not user.is_active:
        raise HTTPException(401, "Incorrect email or password")

    token = create_access_token(str(user.id), user.role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
    }

@app.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "name": user.name, "email": user.email, "phone": user.phone, "role": user.role, "is_active": user.is_active, "is_approved": user.is_approved}


@app.put("/admin/users/{user_id}/approve")
def approve_user(user_id: int, data: dict = Body(default={}), db: Session = Depends(get_db), admin: User = Depends(require_roles("Admin"))):
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, "User not found")
    user.is_approved = bool(data.get("approved", True))
    partner = db.query(DeliveryPartner).filter_by(user_id=user_id).first()
    if partner: partner.is_approved = user.is_approved
    db.commit()
    return {"user_id": user.id, "is_approved": user.is_approved}


@app.post("/restaurants", status_code=201)
def create_restaurant(data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    if not owner.is_approved: raise HTTPException(403, "Restaurant owner approval is required")
    r = Restaurant(owner_id=owner.id, **{k: v for k, v in data.items() if k in Restaurant.__table__.columns.keys() and k not in {"id", "owner_id", "created_at", "updated_at"}})
    db.add(r); db.commit(); db.refresh(r); return r


@app.get("/restaurants/me")
def my_restaurants(db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    return db.query(Restaurant).filter_by(owner_id=owner.id).all()


@app.get("/restaurants/nearby")
def nearby(latitude: float, longitude: float, name: str | None = None, cuisine_type: str | None = None,
           veg_only: bool = False, min_rating: float = 0, has_offers: bool = False,
           sort_by: str = "distance", order: str = "asc", skip: int = 0, limit: int = 20,
           db: Session = Depends(get_db)):
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180: raise HTTPException(422, "Invalid coordinates")
    restaurants = db.query(Restaurant).filter(Restaurant.is_active.is_(True)).all()
    results = []
    for r in restaurants:
        dist = haversine_km(latitude, longitude, r.latitude, r.longitude)
        if not restaurant_is_open(r) or dist > r.delivery_radius_km or r.average_rating < min_rating: continue
        if name and name.lower() not in r.name.lower(): continue
        if cuisine_type and cuisine_type.lower() not in r.cuisine_type.lower(): continue
        if veg_only and not db.query(MenuItem).filter_by(restaurant_id=r.id, is_veg=True, is_available=True, is_deleted=False).first(): continue
        offer = db.query(Coupon).filter(Coupon.is_active.is_(True), Coupon.valid_to >= datetime.utcnow(), or_(Coupon.restaurant_id == r.id, Coupon.restaurant_id.is_(None))).first()
        if has_offers and not offer: continue
        results.append({"id": r.id, "name": r.name, "cuisine_type": r.cuisine_type, "address": r.address,
                        "latitude": r.latitude, "longitude": r.longitude, "average_rating": r.average_rating,
                        "distance_km": round(dist, 2), "estimated_delivery_minutes": r.avg_preparation_minutes + ceil(dist / 20 * 60), "has_offers": bool(offer)})
    key = {"distance": "distance_km", "rating": "average_rating", "estimated_delivery_minutes": "estimated_delivery_minutes"}.get(sort_by, "distance_km")
    results.sort(key=lambda x: x[key], reverse=(order.lower() == "desc"))
    return results[max(skip, 0):max(skip, 0) + min(max(limit, 1), 100)]


@app.get("/restaurants/{restaurant_id}")
def get_restaurant(restaurant_id: int, db: Session = Depends(get_db)):
    r = db.get(Restaurant, restaurant_id)
    if not r: raise HTTPException(404, "Restaurant not found")
    return {"restaurant": r, "is_open": restaurant_is_open(r)}


@app.put("/restaurants/{restaurant_id}")
def update_restaurant(restaurant_id: int, data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    r = owner_restaurant(db, restaurant_id, owner.id)
    for k, v in data.items():
        if k in {"name", "cuisine_type", "address", "latitude", "longitude", "opening_time", "closing_time", "is_temporarily_closed", "delivery_radius_km", "min_order_value", "avg_preparation_minutes", "is_active"}: setattr(r, k, v)
    db.commit(); db.refresh(r); return r


@app.put("/restaurants/{restaurant_id}/open-status")
def open_status(restaurant_id: int, data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    r = owner_restaurant(db, restaurant_id, owner.id); r.is_temporarily_closed = bool(data.get("is_temporarily_closed", False)); db.commit(); return {"id": r.id, "is_open": restaurant_is_open(r)}


@app.post("/restaurants/{restaurant_id}/categories", status_code=201)
def add_category(restaurant_id: int, data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    owner_restaurant(db, restaurant_id, owner.id); c = MenuCategory(restaurant_id=restaurant_id, name=data["name"]); db.add(c); db.commit(); db.refresh(c); return c


@app.post("/restaurants/{restaurant_id}/menu-items", status_code=201)
def add_menu_item(restaurant_id: int, data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    owner_restaurant(db, restaurant_id, owner.id)
    category = db.get(MenuCategory, data["category_id"])
    if not category or category.restaurant_id != restaurant_id: raise HTTPException(400, "Category does not belong to this restaurant")
    if float(data.get("price", 0)) <= 0: raise HTTPException(422, "Price must be greater than 0")
    item = MenuItem(restaurant_id=restaurant_id, **{k: v for k, v in data.items() if k in {"category_id", "name", "description", "price", "is_veg", "packaging_charge", "is_available"}})
    db.add(item); db.commit(); db.refresh(item); return item


@app.get("/restaurants/{restaurant_id}/menu")
def menu(restaurant_id: int, db: Session = Depends(get_db)):
    r = db.get(Restaurant, restaurant_id)
    if not r: raise HTTPException(404, "Restaurant not found")
    categories = db.query(MenuCategory).filter_by(restaurant_id=restaurant_id).all()
    return [{"category": c.name, "category_id": c.id, "items": db.query(MenuItem).filter_by(category_id=c.id, is_deleted=False).all()} for c in categories]


@app.put("/menu-items/{item_id}")
def update_item(item_id: int, data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    item = db.get(MenuItem, item_id)
    if not item: raise HTTPException(404, "Menu item not found")
    owner_restaurant(db, item.restaurant_id, owner.id)
    for k, v in data.items():
        if k in {"name", "description", "price", "is_veg", "packaging_charge", "is_available", "category_id"}: setattr(item, k, v)
    db.commit(); db.refresh(item); return item


@app.put("/menu-items/{item_id}/availability")
def item_availability(item_id: int, data: dict = Body(...), db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    item = db.get(MenuItem, item_id)
    if not item: raise HTTPException(404, "Menu item not found")
    owner_restaurant(db, item.restaurant_id, owner.id); item.is_available = bool(data.get("is_available", True)); db.commit(); return item


@app.delete("/menu-items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    item = db.get(MenuItem, item_id)
    if not item: raise HTTPException(404, "Menu item not found")
    owner_restaurant(db, item.restaurant_id, owner.id); item.is_deleted = True; item.is_available = False; db.commit(); return {"message": "Menu item archived"}


@app.post("/addresses", status_code=201)
def create_address(data: dict = Body(...), db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    lat, lon, pin = float(data["latitude"]), float(data["longitude"]), str(data["pincode"])
    if not -90 <= lat <= 90 or not -180 <= lon <= 180: raise HTTPException(422, "Invalid latitude or longitude")
    if len(pin) != 6 or not pin.isdigit(): raise HTTPException(422, "Pincode must contain 6 digits")
    make_default = bool(data.get("is_default", False))
    if make_default:
        db.query(Address).filter_by(customer_id=customer.id).update({Address.is_default: False})
    addr = Address(customer_id=customer.id, label=data.get("label", "Home"), address_line=data["address_line"], city=data["city"], pincode=pin, latitude=lat, longitude=lon, is_default=make_default)
    db.add(addr); db.commit(); db.refresh(addr); return addr


@app.get("/addresses")
def list_addresses(db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    return db.query(Address).filter_by(customer_id=customer.id).order_by(Address.is_default.desc(), Address.id).all()


@app.put("/addresses/{address_id}")
def update_address(address_id: int, data: dict = Body(...), db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    a = db.get(Address, address_id)
    if not a or a.customer_id != customer.id: raise HTTPException(404, "Address not found")
    for k, v in data.items():
        if k in {"label", "address_line", "city", "pincode", "latitude", "longitude"}: setattr(a, k, v)
    if not -90 <= a.latitude <= 90 or not -180 <= a.longitude <= 180: raise HTTPException(422, "Invalid coordinates")
    if len(str(a.pincode)) != 6: raise HTTPException(422, "Pincode must contain 6 digits")
    db.commit(); db.refresh(a); return a


@app.delete("/addresses/{address_id}")
def delete_address(address_id: int, db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    a = db.get(Address, address_id)
    if not a or a.customer_id != customer.id: raise HTTPException(404, "Address not found")
    db.delete(a); db.commit(); return {"message": "Address deleted"}


@app.put("/addresses/{address_id}/default")
def default_address(address_id: int, db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    a = db.get(Address, address_id)
    if not a or a.customer_id != customer.id: raise HTTPException(404, "Address not found")
    db.query(Address).filter_by(customer_id=customer.id).update({Address.is_default: False}); a.is_default = True; db.commit(); return a


def cart_view(db: Session, cart: Cart):
    rows = cart_lines(db, cart)
    items = []
    total = packaging = 0.0
    for ci, item in rows:
        line = round(item.price * ci.quantity, 2); pack = round(item.packaging_charge * ci.quantity, 2)
        total += line; packaging += pack
        items.append({"cart_item_id": ci.id, "menu_item_id": item.id, "name": item.name, "price": item.price, "quantity": ci.quantity, "line_total": line, "packaging_charge": pack})
    discount = 0.0
    coupon = db.get(Coupon, cart.coupon_id) if cart.coupon_id else None
    if coupon and cart.restaurant_id:
        try: discount = coupon_discount(db, coupon, cart.customer_id, cart.restaurant_id, total)
        except HTTPException: discount = 0.0
    return {"restaurant_id": cart.restaurant_id, "items": items, "item_total": round(total, 2), "applied_coupon": coupon.code if coupon else None, "discount": round(discount, 2), "packaging_charges": round(packaging, 2), "item_total_after_discount": round(total - discount, 2)}


@app.get("/cart")
def get_cart(db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    return cart_view(db, ensure_cart(db, customer.id))


@app.post("/cart/items", status_code=201)
def add_cart_item(data: dict = Body(...), db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    quantity = int(data.get("quantity", 1))
    if not 1 <= quantity <= 10: raise HTTPException(422, "Quantity must be between 1 and 10")
    item = db.get(MenuItem, int(data["menu_item_id"]))
    if not item or item.is_deleted: raise HTTPException(404, "Menu item not found")
    if not item.is_available: raise HTTPException(409, "This menu item is unavailable")
    cart = ensure_cart(db, customer.id)
    if cart.restaurant_id and cart.restaurant_id != item.restaurant_id:
        raise HTTPException(409, "Your cart has items from another restaurant. Clear the cart to continue")
    cart.restaurant_id = item.restaurant_id
    existing = db.query(CartItem).filter_by(cart_id=cart.id, menu_item_id=item.id).first()
    if existing:
        if existing.quantity + quantity > 10: raise HTTPException(422, "Quantity must be between 1 and 10")
        existing.quantity += quantity
    else: db.add(CartItem(cart_id=cart.id, menu_item_id=item.id, quantity=quantity))
    db.commit(); return cart_view(db, cart)


@app.put("/cart/items/{item_id}")
def update_cart_item(item_id: int, data: dict = Body(...), db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    ci = db.get(CartItem, item_id); cart = ensure_cart(db, customer.id)
    if not ci or ci.cart_id != cart.id: raise HTTPException(404, "Cart item not found")
    quantity = int(data["quantity"])
    if not 1 <= quantity <= 10: raise HTTPException(422, "Quantity must be between 1 and 10")
    ci.quantity = quantity; db.commit(); return cart_view(db, cart)


@app.delete("/cart/items/{item_id}")
def remove_cart_item(item_id: int, db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    cart = ensure_cart(db, customer.id); ci = db.get(CartItem, item_id)
    if not ci or ci.cart_id != cart.id: raise HTTPException(404, "Cart item not found")
    db.delete(ci); db.flush()
    if not db.query(CartItem).filter_by(cart_id=cart.id).first(): cart.restaurant_id = None; cart.coupon_id = None
    db.commit(); return cart_view(db, cart)


@app.delete("/cart")
def clear_cart(db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    cart = ensure_cart(db, customer.id); db.query(CartItem).filter_by(cart_id=cart.id).delete(); cart.restaurant_id = None; cart.coupon_id = None; db.commit(); return {"message": "Cart cleared"}


@app.post("/coupons", status_code=201)
def create_coupon(data: dict = Body(...), db: Session = Depends(get_db), user: User = Depends(require_roles("Admin", "Restaurant Owner"))):
    if user.role == "Restaurant Owner":
        rid = data.get("restaurant_id")
        if not rid: raise HTTPException(403, "Restaurant owners must create restaurant-specific coupons")
        owner_restaurant(db, int(rid), user.id)
    code = str(data["code"]).upper().strip()
    if db.query(Coupon).filter(func.upper(Coupon.code) == code).first(): raise HTTPException(409, "Coupon code already exists")
    valid_from = datetime.fromisoformat(data["valid_from"]); valid_to = datetime.fromisoformat(data["valid_to"])
    if valid_to <= valid_from: raise HTTPException(422, "valid_to must be after valid_from")
    d_type = str(data["discount_type"]).title(); value = float(data["discount_value"])
    if d_type not in {"Percentage", "Flat"} or value <= 0 or (d_type == "Percentage" and value > 100): raise HTTPException(422, "Invalid coupon discount")
    c = Coupon(code=code, description=data.get("description", ""), discount_type=d_type, discount_value=value,
        max_discount=data.get("max_discount"), min_order_value=float(data.get("min_order_value", 0)), restaurant_id=data.get("restaurant_id"),
        valid_from=valid_from, valid_to=valid_to, total_usage_limit=data.get("total_usage_limit"), per_user_limit=int(data.get("per_user_limit", 1)),
        first_order_only=bool(data.get("first_order_only", False)), created_by=user.id, is_active=True)
    db.add(c); db.commit(); db.refresh(c); return c


@app.get("/coupons")
def list_coupons(restaurant_id: int | None = None, is_active: bool | None = None, discount_type: str | None = None,
                 skip: int = 0, limit: int = 20, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(Coupon)
    if user.role == "Restaurant Owner":
        own_ids = [r.id for r in db.query(Restaurant).filter_by(owner_id=user.id).all()]
        q = q.filter(or_(Coupon.restaurant_id.in_(own_ids), Coupon.restaurant_id.is_(None)))
    elif user.role != "Admin":
        q = q.filter(Coupon.is_active.is_(True))
    if restaurant_id is not None: q = q.filter(or_(Coupon.restaurant_id == restaurant_id, Coupon.restaurant_id.is_(None)))
    if is_active is not None: q = q.filter(Coupon.is_active == is_active)
    if discount_type: q = q.filter(func.lower(Coupon.discount_type) == discount_type.lower())
    return q.order_by(Coupon.id.desc()).offset(max(skip, 0)).limit(min(max(limit, 1), 100)).all()


@app.put("/coupons/{coupon_id}")
def update_coupon(coupon_id: int, data: dict = Body(...), db: Session = Depends(get_db), user: User = Depends(require_roles("Admin", "Restaurant Owner"))):
    c = db.get(Coupon, coupon_id)
    if not c: raise HTTPException(404, "Coupon not found")
    if user.role == "Restaurant Owner": owner_restaurant(db, c.restaurant_id, user.id)
    allowed = {"description", "discount_value", "max_discount", "min_order_value", "valid_from", "valid_to", "total_usage_limit", "per_user_limit", "first_order_only", "is_active"}
    for k, v in data.items():
        if k in allowed: setattr(c, k, datetime.fromisoformat(v) if k in {"valid_from", "valid_to"} and isinstance(v, str) else v)
    db.commit(); db.refresh(c); return c


@app.put("/coupons/{coupon_id}/deactivate")
def deactivate_coupon(coupon_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("Admin", "Restaurant Owner"))):
    c = db.get(Coupon, coupon_id)
    if not c: raise HTTPException(404, "Coupon not found")
    if user.role == "Restaurant Owner": owner_restaurant(db, c.restaurant_id, user.id)
    c.is_active = False; db.commit(); return {"id": c.id, "is_active": c.is_active}


@app.get("/coupons/available")
def available_coupons(db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    cart = ensure_cart(db, customer.id); view = cart_view(db, cart)
    if not cart.restaurant_id: return []
    coupons = db.query(Coupon).filter(Coupon.is_active.is_(True), Coupon.valid_from <= datetime.utcnow(), Coupon.valid_to >= datetime.utcnow(), or_(Coupon.restaurant_id == cart.restaurant_id, Coupon.restaurant_id.is_(None))).all()
    result = []
    for c in coupons:
        try: result.append({"coupon": c, "discount": coupon_discount(db, c, customer.id, cart.restaurant_id, view["item_total"])})
        except HTTPException: pass
    return result


@app.post("/cart/apply-coupon")
def apply_coupon(data: dict = Body(...), db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    cart = ensure_cart(db, customer.id)
    if not cart.restaurant_id: raise HTTPException(400, "Add items before applying a coupon")
    coupon = db.query(Coupon).filter(func.upper(Coupon.code) == str(data["code"]).upper()).first()
    if not coupon: raise HTTPException(400, "Coupon code is invalid")
    view = cart_view(db, cart); discount = coupon_discount(db, coupon, customer.id, cart.restaurant_id, view["item_total"])
    cart.coupon_id = coupon.id; db.commit(); return {**cart_view(db, cart), "discount": discount}


@app.delete("/cart/coupon")
def remove_coupon(db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    cart = ensure_cart(db, customer.id); cart.coupon_id = None; db.commit(); return cart_view(db, cart)


@app.post("/orders", status_code=201)
def place_order(data: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    cart = ensure_cart(db, customer.id); rows = cart_lines(db, cart)
    if not rows: raise HTTPException(400, "Cart is empty")
    restaurant = db.get(Restaurant, cart.restaurant_id)
    if not restaurant or not restaurant_is_open(restaurant): raise HTTPException(400, "Restaurant is closed")
    address = db.get(Address, int(data["address_id"]))
    if not address or address.customer_id != customer.id: raise HTTPException(404, "Address not found")
    distance = haversine_km(restaurant.latitude, restaurant.longitude, address.latitude, address.longitude)
    if distance > restaurant.delivery_radius_km: raise HTTPException(400, "Address is outside the restaurant delivery radius")
    item_total = packaging = 0.0
    for ci, item in rows:
        if not item.is_available or item.is_deleted: raise HTTPException(409, f"{item.name} is no longer available")
        item_total += item.price * ci.quantity; packaging += item.packaging_charge * ci.quantity
    if item_total < restaurant.min_order_value: raise HTTPException(400, f"Minimum order value is ₹{restaurant.min_order_value:g}")
    coupon = db.get(Coupon, cart.coupon_id) if cart.coupon_id else None
    discount = coupon_discount(db, coupon, customer.id, restaurant.id, item_total) if coupon else 0
    delivery_fee = 20 + max(0, ceil(distance) - 3) * 8
    local_hour = datetime.now().hour
    if (12 <= local_hour < 14) or (19 <= local_hour < 22): delivery_fee *= 1.5
    platform_fee = 5; gst = round((item_total - discount) * .05, 2)
    tip = float(data.get("tip_amount", 0))
    if tip < 0: raise HTTPException(422, "Tip cannot be negative")
    method = str(data.get("payment_method", "COD")).title()
    if method not in {"Cod", "Online"}: raise HTTPException(422, "payment_method must be COD or Online")
    method = "COD" if method == "Cod" else method
    now = datetime.utcnow()
    order = FoodOrder(order_number=f"ORD-{now:%Y%m%d}-{db.query(FoodOrder).count()+1:04d}", customer_id=customer.id,
        restaurant_id=restaurant.id, address_id=address.id, item_total=round(item_total,2), coupon_code=coupon.code if coupon else None,
        discount_amount=discount, packaging_charges=round(packaging,2), delivery_fee=round(delivery_fee,2), platform_fee=platform_fee,
        gst_amount=gst, tip_amount=tip, grand_total=round(item_total-discount+packaging+delivery_fee+platform_fee+gst+tip,2),
        payment_method=method, payment_status="Paid" if method == "Online" else "Pending", status="Placed", distance_km=round(distance,2), delivery_otp=f"{randbelow(10000):04d}")
    try:
        db.add(order); db.flush()
        db.add(OrderStatusHistory(order_id=order.id, old_status=None, new_status="Placed", changed_by=f"User:{customer.id}"))
        for ci, item in rows:
            db.add(OrderItem(order_id=order.id, menu_item_id=item.id, item_name=item.name, unit_price=item.price, quantity=ci.quantity, packaging_charge=item.packaging_charge))
        if coupon: db.add(CouponUsage(coupon_id=coupon.id, customer_id=customer.id, order_id=order.id, discount_amount=discount))
        for ci, _ in rows: db.delete(ci)
        cart.restaurant_id = None; cart.coupon_id = None
        db.commit(); db.refresh(order)
    except Exception:
        db.rollback(); raise
    if background_tasks: background_tasks.add_task(broadcast, order.id, {"type":"status", "order_id":order.id, "status":"Placed"})
    return {"order": order, "delivery_otp": order.delivery_otp}


def order_view(db: Session, order: FoodOrder, viewer: User):
    result = {k: getattr(order, k) for k in ["id", "order_number", "customer_id", "restaurant_id", "address_id", "delivery_partner_id", "item_total", "coupon_code", "discount_amount", "packaging_charges", "delivery_fee", "platform_fee", "gst_amount", "tip_amount", "grand_total", "payment_method", "payment_status", "status", "distance_km", "placed_at", "delivered_at"]}
    result["items"] = db.query(OrderItem).filter_by(order_id=order.id).all()
    result["status_history"] = db.query(OrderStatusHistory).filter_by(order_id=order.id).order_by(OrderStatusHistory.id).all()
    if viewer.id == order.customer_id: result["delivery_otp"] = order.delivery_otp
    return result


@app.get("/orders")
def list_orders(status: str | None = None, restaurant_id: int | None = None, from_date: datetime | None = None, to_date: datetime | None = None,
                skip: int = 0, limit: int = 20, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(FoodOrder)
    if user.role == "Customer": q = q.filter_by(customer_id=user.id)
    elif user.role == "Restaurant Owner": q = q.filter(FoodOrder.restaurant_id.in_([r.id for r in db.query(Restaurant).filter_by(owner_id=user.id)]))
    elif user.role == "Delivery Partner": q = q.filter_by(delivery_partner_id=user.id)
    elif user.role != "Admin": raise HTTPException(403, "Forbidden")
    if status: q = q.filter(FoodOrder.status == status)
    if restaurant_id: q = q.filter(FoodOrder.restaurant_id == restaurant_id)
    if from_date: q = q.filter(FoodOrder.placed_at >= from_date)
    if to_date: q = q.filter(FoodOrder.placed_at <= to_date)
    return [order_view(db, o, user) for o in q.order_by(FoodOrder.id.desc()).offset(max(skip,0)).limit(min(max(limit,1),100)).all()]


@app.get("/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    o = db.get(FoodOrder, order_id)
    if not o: raise HTTPException(404, "Order not found")
    allowed = user.role == "Admin" or o.customer_id == user.id or o.delivery_partner_id == user.id
    r = db.get(Restaurant, o.restaurant_id)
    if r and r.owner_id == user.id: allowed = True
    if not allowed: raise HTTPException(403, "You cannot view this order")
    return order_view(db, o, user)


@app.put("/orders/{order_id}/cancel")
def cancel_order(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    o = db.get(FoodOrder, order_id)
    if not o or o.customer_id != customer.id: raise HTTPException(404, "Order not found")
    if o.status != "Placed": raise HTTPException(400, "Orders can be cancelled only before restaurant acceptance")
    set_status(db, o, "Cancelled", f"User:{customer.id}")
    if o.payment_method == "Online": o.payment_status = "Refunded"
    db.query(CouponUsage).filter_by(order_id=o.id).delete(); db.commit(); background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status}); return order_view(db, o, customer)


@app.post("/orders/{order_id}/reorder")
def reorder(order_id: int, db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    o = db.get(FoodOrder, order_id)
    if not o or o.customer_id != customer.id: raise HTTPException(404, "Order not found")
    cart = ensure_cart(db, customer.id); db.query(CartItem).filter_by(cart_id=cart.id).delete(); cart.coupon_id = None; cart.restaurant_id = o.restaurant_id
    added = skipped = 0
    for previous in db.query(OrderItem).filter_by(order_id=o.id).all():
        item = db.get(MenuItem, previous.menu_item_id)
        if item and item.is_available and not item.is_deleted:
            db.add(CartItem(cart_id=cart.id, menu_item_id=item.id, quantity=min(previous.quantity,10))); added += 1
        else: skipped += 1
    db.commit(); return {"cart": cart_view(db, cart), "items_added": added, "items_skipped": skipped}


def check_order_owner(db: Session, order_id: int, user: User):
    o = db.get(FoodOrder, order_id)
    if not o: raise HTTPException(404, "Order not found")
    r = db.get(Restaurant, o.restaurant_id)
    if not r or r.owner_id != user.id: raise HTTPException(403, "You do not own this order's restaurant")
    return o


@app.get("/restaurants/me/orders")
def restaurant_orders(status: str | None = None, skip: int = 0, limit: int = 20, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    ids = [r.id for r in db.query(Restaurant).filter_by(owner_id=owner.id).all()]
    q = db.query(FoodOrder).filter(FoodOrder.restaurant_id.in_(ids))
    if status: q = q.filter(FoodOrder.status == status)
    return [order_view(db, o, owner) for o in q.order_by(FoodOrder.id.desc()).offset(skip).limit(min(limit,100)).all()]


@app.put("/orders/{order_id}/accept")
def accept_order(order_id: int, data: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    o = check_order_owner(db, order_id, owner)
    if o.status != "Placed": raise HTTPException(400, "Only Placed orders can be accepted")
    prep = int(data.get("estimated_preparation_minutes", 0))
    if prep < 1: raise HTTPException(422, "estimated_preparation_minutes must be positive")
    set_status(db, o, "Accepted", f"User:{owner.id}", f"Preparation estimate: {prep} minutes")
    db.commit(); assign_nearest_partner(db, o)
    if background_tasks: background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status})
    return {"order": order_view(db, o, owner), "delivery_offer_created": db.query(DeliveryOffer).filter_by(order_id=o.id, status="Pending").count() > 0}


@app.put("/orders/{order_id}/reject")
def reject_order(order_id: int, data: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    o = check_order_owner(db, order_id, owner)
    if o.status != "Placed": raise HTTPException(400, "Only Placed orders can be rejected")
    reason = str(data.get("reason", "")).strip()
    if not reason: raise HTTPException(422, "A rejection reason is required")
    set_status(db, o, "Rejected", f"User:{owner.id}", reason); o.rejection_reason = reason
    if o.payment_method == "Online": o.payment_status = "Refunded"
    db.query(CouponUsage).filter_by(order_id=o.id).delete(); db.commit()
    if background_tasks: background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status})
    return order_view(db, o, owner)


def restaurant_transition(order_id: int, target: str, db: Session, owner: User, background_tasks: BackgroundTasks | None = None):
    o = check_order_owner(db, order_id, owner)
    allowed = {"Preparing": "Accepted", "Ready for Pickup": "Preparing"}
    if o.status != allowed[target]: raise HTTPException(400, f"Order must be {allowed[target]} before it can become {target}")
    set_status(db, o, target, f"User:{owner.id}"); db.commit()
    if background_tasks: background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status})
    if target == "Ready for Pickup" and not o.delivery_partner_id: assign_nearest_partner(db, o)
    return order_view(db, o, owner)


@app.put("/orders/{order_id}/preparing")
def preparing(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    return restaurant_transition(order_id, "Preparing", db, owner, background_tasks)


@app.put("/orders/{order_id}/ready")
def ready(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    return restaurant_transition(order_id, "Ready for Pickup", db, owner, background_tasks)


def partner_for_user(db: Session, user: User) -> DeliveryPartner:
    p = db.query(DeliveryPartner).filter_by(user_id=user.id).first()
    if not p: raise HTTPException(404, "Delivery partner profile not found")
    if not p.is_approved: raise HTTPException(403, "Delivery partner approval is required")
    return p


@app.put("/partners/me/availability")
def partner_availability(
    data: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("Delivery Partner")),
):
    p = partner_for_user(db, user)
    p.is_online = bool(data["is_online"])
    db.commit()

    if p.is_online:
        ready_orders = (
            db.query(FoodOrder)
            .filter(
                FoodOrder.status == "Ready for Pickup",
                FoodOrder.delivery_partner_id.is_(None),
            )
            .order_by(FoodOrder.placed_at.asc())
            .all()
        )

        for order in ready_orders:
            assign_nearest_partner(db, order)

    return {"is_online": p.is_online}

@app.put("/partners/me/location")
def partner_location(data: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    p = partner_for_user(db, user); lat, lon = float(data["latitude"]), float(data["longitude"])
    if not -90 <= lat <= 90 or not -180 <= lon <= 180: raise HTTPException(422, "Invalid coordinates")
    p.current_latitude, p.current_longitude, p.last_location_at = lat, lon, datetime.utcnow(); db.commit()
    active = db.query(FoodOrder).filter_by(delivery_partner_id=user.id).filter(FoodOrder.status.in_(["Picked Up", "Out for Delivery"])).all()
    for o in active:
        addr = db.get(Address, o.address_id); remaining = haversine_km(lat, lon, addr.latitude, addr.longitude)
        payload = {"type": "location", "order_id": o.id, "latitude": lat, "longitude": lon, "distance_remaining_km": round(remaining, 2), "estimated_arrival_minutes": ceil(remaining / 20 * 60), "status": o.status}
        if background_tasks: background_tasks.add_task(broadcast, o.id, payload)
    return {"latitude": lat, "longitude": lon, "updated_at": p.last_location_at, "active_orders_updated": len(active)}


@app.get("/partners/me/offers")
def partner_offers(db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    p = partner_for_user(db, user); now = datetime.utcnow()
    offers = db.query(DeliveryOffer).filter_by(partner_id=p.id, status="Pending").filter(DeliveryOffer.expires_at > now).all()
    out = []
    for offer in offers:
        order = db.get(FoodOrder, offer.order_id); r = db.get(Restaurant, order.restaurant_id)
        out.append({"offer_id": offer.id, "order_id": order.id, "restaurant_name": r.name,
            "pickup_distance_km": round(haversine_km(p.current_latitude,p.current_longitude,r.latitude,r.longitude),2),
            "delivery_distance_km": order.distance_km, "expected_earnings": round(25+6*order.distance_km+order.tip_amount,2),
            "seconds_left": max(0, int((offer.expires_at-now).total_seconds()))})
    return out


@app.put("/offers/{offer_id}/accept")
def accept_offer(offer_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    p = partner_for_user(db, user)
    offer = db.query(DeliveryOffer).filter_by(id=offer_id).with_for_update().first()
    if not offer or offer.partner_id != p.id: raise HTTPException(404, "Offer not found")
    if offer.status != "Pending" or offer.expires_at < datetime.utcnow(): raise HTTPException(409, "Offer is no longer available")
    order = db.query(FoodOrder).filter_by(id=offer.order_id).with_for_update().first()
    if order.delivery_partner_id: raise HTTPException(409, "Another partner has accepted this order")
    active = db.query(FoodOrder).filter_by(delivery_partner_id=user.id).filter(FoodOrder.status.in_(["Picked Up", "Out for Delivery"])).first()
    if active: raise HTTPException(409, "You already have an active delivery")
    offer.status = "Accepted"; order.delivery_partner_id = user.id; db.commit()
    return {"message": "Delivery offer accepted", "order_id": order.id, "status": order.status}


@app.put("/offers/{offer_id}/decline")
def decline_offer(offer_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    p = partner_for_user(db, user); offer = db.query(DeliveryOffer).filter_by(id=offer_id, partner_id=p.id).first()
    if not offer or offer.status != "Pending": raise HTTPException(404, "Pending offer not found")
    offer.status = "Declined"; order = db.get(FoodOrder, offer.order_id); db.commit(); assign_nearest_partner(db, order)
    return {"message": "Offer declined"}


def partner_order(order_id: int, user: User, db: Session):
    o = db.get(FoodOrder, order_id)
    if not o: raise HTTPException(404, "Order not found")
    if o.delivery_partner_id != user.id: raise HTTPException(403, "This order is assigned to another partner")
    return o


@app.put("/orders/{order_id}/pickup")
def pickup(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    o = partner_order(order_id, user, db)
    if o.status != "Ready for Pickup": raise HTTPException(400, "Order must be Ready for Pickup")
    set_status(db, o, "Picked Up", f"User:{user.id}"); db.commit(); background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status}); return {"order_id": o.id, "status": o.status}


@app.put("/orders/{order_id}/out-for-delivery")
def out_for_delivery(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    o = partner_order(order_id, user, db)
    if o.status != "Picked Up": raise HTTPException(400, "Order must be Picked Up")
    set_status(db, o, "Out for Delivery", f"User:{user.id}"); db.commit(); background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status}); return {"order_id": o.id, "status": o.status}


@app.put("/orders/{order_id}/deliver")
def deliver(order_id: int, data: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    o = partner_order(order_id, user, db)
    if o.status != "Out for Delivery": raise HTTPException(400, "Order must be Out for Delivery")
    if o.otp_locked: raise HTTPException(423, "Delivery OTP verification is locked; Admin has been notified")
    if str(data.get("otp", "")) != o.delivery_otp:
        o.otp_attempts += 1
        if o.otp_attempts >= 3: o.otp_locked = True
        db.commit(); raise HTTPException(400, "Incorrect delivery OTP" if not o.otp_locked else "Incorrect OTP; verification is now locked")
    set_status(db, o, "Delivered", f"User:{user.id}"); o.delivered_at = datetime.utcnow()
    if o.payment_method == "COD": o.payment_status = "Paid"
    db.commit()
    if background_tasks: background_tasks.add_task(broadcast,o.id,{"type":"status","order_id":o.id,"status":o.status})
    return {"order_id": o.id, "status": o.status, "delivered_at": o.delivered_at}


@app.get("/partners/me/earnings")
def earnings(from_date: datetime | None = None, to_date: datetime | None = None, db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    q = db.query(FoodOrder).filter_by(delivery_partner_id=user.id, status="Delivered")
    if from_date: q = q.filter(FoodOrder.delivered_at >= from_date)
    if to_date: q = q.filter(FoodOrder.delivered_at <= to_date)
    orders = q.all(); rows = [{"order_id": o.id, "delivered_at": o.delivered_at, "distance_km": o.distance_km, "earnings": round(25+6*o.distance_km+o.tip_amount,2)} for o in orders]
    return {"deliveries": rows, "total_earnings": round(sum(x["earnings"] for x in rows),2)}


@app.websocket("/ws/orders/{order_id}")
async def order_tracking(websocket: WebSocket, order_id: int, token: str = Query(...)):
    payload = decode_token(token)
    if not payload or not payload.get("sub"):
        await websocket.close(code=4401); return
    db = SessionLocal()
    try:
        user = db.get(User, int(payload["sub"])); order = db.get(FoodOrder, order_id)
        if not user or not order or not user.is_active:
            await websocket.close(code=4401); return
        restaurant = db.get(Restaurant, order.restaurant_id)
        authorized = user.role == "Admin" or user.id == order.customer_id or user.id == order.delivery_partner_id or (restaurant and restaurant.owner_id == user.id)
        if not authorized:
            await websocket.close(code=4403); return
        await websocket.accept(); connections.setdefault(order_id, set()).add(websocket)
        await websocket.send_json({"type": "snapshot", "order_id": order.id, "status": order.status, "delivery_partner_id": order.delivery_partner_id})
        while True:
            # Partner can send {"latitude": ..., "longitude": ...}; REST updates are also supported.
            msg = await websocket.receive_json()
            if user.role == "Delivery Partner" and user.id == order.delivery_partner_id:
                partner = db.query(DeliveryPartner).filter_by(user_id=user.id).first()
                if partner and "latitude" in msg and "longitude" in msg:
                    partner.current_latitude = float(msg["latitude"]); partner.current_longitude = float(msg["longitude"]); partner.last_location_at = datetime.utcnow(); db.commit()
                    addr = db.get(Address, order.address_id); remaining = haversine_km(partner.current_latitude, partner.current_longitude, addr.latitude, addr.longitude)
                    await broadcast(order_id, {"type":"location", "order_id":order_id, "latitude":partner.current_latitude, "longitude":partner.current_longitude,
                        "distance_remaining_km":round(remaining,2), "estimated_arrival_minutes":ceil(remaining/20*60), "status":order.status})
    except WebSocketDisconnect:
        pass
    except Exception:
        try: await websocket.close(code=1011)
        except Exception: pass
    finally:
        connections.get(order_id, set()).discard(websocket); db.close()


@app.websocket("/ws/restaurant")
async def restaurant_alerts(websocket: WebSocket, token: str = Query(...)):
    payload = decode_token(token); db = SessionLocal()
    try:
        user = db.get(User, int(payload["sub"])) if payload else None
        if not user or user.role != "Restaurant Owner": await websocket.close(code=4403); return
        await websocket.accept(); await websocket.send_json({"message": "Connected. New-order events are available on /ws/orders/{order_id} after order creation."})
        while True: await websocket.receive_text()
    except WebSocketDisconnect: pass
    finally: db.close()


@app.websocket("/ws/partner")
async def partner_alerts(websocket: WebSocket, token: str = Query(...)):
    payload = decode_token(token); db = SessionLocal()
    try:
        user = db.get(User, int(payload["sub"])) if payload else None
        if not user or user.role != "Delivery Partner": await websocket.close(code=4403); return
        await websocket.accept(); await websocket.send_json({"message": "Connected. Check GET /partners/me/offers for pending offers."})
        while True: await websocket.receive_text()
    except WebSocketDisconnect: pass
    finally: db.close()


@app.post("/orders/{order_id}/rating", status_code=201)
def rate_order(order_id: int, data: dict = Body(...), db: Session = Depends(get_db), customer: User = Depends(require_roles("Customer"))):
    o = db.get(FoodOrder, order_id)
    if not o or o.customer_id != customer.id: raise HTTPException(404, "Order not found")
    if o.status != "Delivered": raise HTTPException(400, "Only delivered orders can be rated")
    if db.query(Rating).filter_by(order_id=o.id).first(): raise HTTPException(409, "This order has already been rated")
    rr, pr = int(data["restaurant_rating"]), int(data.get("partner_rating", 5))
    if not 1 <= rr <= 5 or not 1 <= pr <= 5: raise HTTPException(422, "Ratings must be from 1 to 5")
    rating = Rating(order_id=o.id, customer_id=customer.id, restaurant_id=o.restaurant_id, restaurant_rating=rr, partner_rating=pr, comment=data.get("comment", ""))
    db.add(rating); db.flush(); r = db.get(Restaurant, o.restaurant_id)
    r.average_rating = db.query(func.avg(Rating.restaurant_rating)).filter_by(restaurant_id=r.id).scalar() or 0
    db.commit(); db.refresh(rating); return rating


@app.get("/restaurants/{restaurant_id}/ratings")
def restaurant_ratings(restaurant_id: int, skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    return db.query(Rating).filter_by(restaurant_id=restaurant_id).order_by(Rating.id.desc()).offset(skip).limit(min(limit,100)).all()


@app.get("/reports/admin-dashboard")
def admin_dashboard(db: Session = Depends(get_db), admin: User = Depends(require_roles("Admin"))):
    start = datetime.combine(datetime.utcnow().date(), time.min)
    today = db.query(FoodOrder).filter(FoodOrder.placed_at >= start).all()
    delivered = [o for o in today if o.status == "Delivered"]
    online = db.query(DeliveryPartner).filter_by(is_online=True, is_approved=True).count()
    times = [(o.delivered_at-o.placed_at).total_seconds()/60 for o in delivered if o.delivered_at]
    return {"today_orders":len(today), "delivered_orders":len(delivered), "cancelled_rejected_orders":sum(o.status in {"Cancelled","Rejected"} for o in today),
        "revenue":round(sum(o.grand_total for o in delivered),2), "discount_given":round(sum(o.discount_amount for o in delivered),2),
        "online_partners":online, "average_delivery_minutes":round(sum(times)/len(times),1) if times else 0}


@app.get("/reports/top-restaurants")
def top_restaurants(from_date: datetime | None = None, to_date: datetime | None = None, db: Session = Depends(get_db), admin: User = Depends(require_roles("Admin"))):
    q = db.query(FoodOrder.restaurant_id, func.count(FoodOrder.id).label("orders"), func.sum(FoodOrder.grand_total).label("revenue")).group_by(FoodOrder.restaurant_id)
    if from_date: q = q.filter(FoodOrder.placed_at >= from_date)
    if to_date: q = q.filter(FoodOrder.placed_at <= to_date)
    return [{"restaurant_id":rid,"name":db.get(Restaurant,rid).name,"orders":count,"revenue":float(revenue or 0)} for rid,count,revenue in q.order_by(func.sum(FoodOrder.grand_total).desc()).limit(10).all()]


@app.get("/reports/coupon-usage")
def coupon_usage(from_date: datetime | None = None, to_date: datetime | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role not in {"Admin", "Restaurant Owner"}: raise HTTPException(403, "Forbidden")
    q = db.query(CouponUsage, Coupon, FoodOrder).join(Coupon).join(FoodOrder, CouponUsage.order_id == FoodOrder.id)
    if user.role == "Restaurant Owner":
        ids = [r.id for r in db.query(Restaurant).filter_by(owner_id=user.id).all()]; q = q.filter(or_(Coupon.restaurant_id.in_(ids), Coupon.restaurant_id.is_(None)))
    if from_date: q = q.filter(FoodOrder.placed_at >= from_date)
    if to_date: q = q.filter(FoodOrder.placed_at <= to_date)
    rows = q.all(); grouped = {}
    for usage,coupon,order in rows:
        x = grouped.setdefault(coupon.code, {"coupon_code":coupon.code,"times_used":0,"total_discount":0,"revenue":0})
        x["times_used"] += 1; x["total_discount"] += usage.discount_amount; x["revenue"] += order.grand_total
    return list(grouped.values())


@app.get("/reports/restaurant-sales")
def restaurant_sales(restaurant_id: int, db: Session = Depends(get_db), owner: User = Depends(require_roles("Restaurant Owner"))):
    owner_restaurant(db, restaurant_id, owner.id)
    orders = db.query(FoodOrder).filter_by(restaurant_id=restaurant_id).all()
    counts = {}
    prep_times = []
    for o in orders:
        for item in db.query(OrderItem).filter_by(order_id=o.id): counts[item.item_name] = counts.get(item.item_name,0)+item.quantity
    best = sorted(counts.items(), key=lambda x:x[1], reverse=True)[:5]
    return {"daily_sales":{},"total_orders":len(orders),"best_selling_items":[{"name":n,"quantity":q} for n,q in best],"average_preparation_minutes":None}


@app.get("/reports/partner-summary")
def partner_summary(db: Session = Depends(get_db), user: User = Depends(require_roles("Delivery Partner"))):
    orders = db.query(FoodOrder).filter_by(delivery_partner_id=user.id, status="Delivered").all()
    return {"completed_deliveries":len(orders),"total_distance_km":round(sum(o.distance_km for o in orders),2),"total_earnings":round(sum(25+6*o.distance_km+o.tip_amount for o in orders),2)}
