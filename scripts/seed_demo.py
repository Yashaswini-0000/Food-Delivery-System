from datetime import datetime, timedelta
from app.database import SessionLocal
from app.models import User, Restaurant, MenuCategory, MenuItem, DeliveryPartner, Address, Coupon
from app.utils.security import hash_password

PASSWORD = "FoodDemo@123"

def user(db, name, email, phone, role, approved=True):
    obj = db.query(User).filter_by(email=email).first()
    if not obj:
        obj = User(name=name, email=email, phone=phone, password_hash=hash_password(PASSWORD), role=role, is_active=True, is_approved=approved)
        db.add(obj); db.flush()
    return obj

def main():
    db = SessionLocal()
    try:
        admin = user(db,"Demo Admin","admin@example.com","9000000001","Admin",True)
        owner = user(db,"Demo Restaurant Owner","owner@example.com","9000000002","Restaurant Owner",True)
        customer = user(db,"Demo Customer","customer@example.com","9000000003","Customer",True)
        partner_user = user(db,"Demo Delivery Partner","partner@example.com","9000000004","Delivery Partner",True)
        restaurant = db.query(Restaurant).filter_by(name="Hyderabad Demo Kitchen").first()
        if not restaurant:
            restaurant = Restaurant(owner_id=owner.id,name="Hyderabad Demo Kitchen",cuisine_type="South Indian, Biryani",address="Madhapur, Hyderabad",latitude=17.4486,longitude=78.3908,opening_time="00:00",closing_time="23:59",delivery_radius_km=5,min_order_value=100,avg_preparation_minutes=25,average_rating=4.6,is_active=True)
            db.add(restaurant); db.flush()
        category = db.query(MenuCategory).filter_by(restaurant_id=restaurant.id,name="Meals").first()
        if not category:
            category=MenuCategory(restaurant_id=restaurant.id,name="Meals"); db.add(category); db.flush()
        for name,price,veg,pack in [("Veg Biryani",180,True,10),("Chicken Biryani",240,False,12),("Masala Dosa",110,True,8)]:
            if not db.query(MenuItem).filter_by(restaurant_id=restaurant.id,name=name).first():
                db.add(MenuItem(restaurant_id=restaurant.id,category_id=category.id,name=name,description="Demo menu item",price=price,is_veg=veg,packaging_charge=pack,is_available=True))
        partner = db.query(DeliveryPartner).filter_by(user_id=partner_user.id).first()
        if not partner:
            partner=DeliveryPartner(user_id=partner_user.id,vehicle_type="Scooter",vehicle_number="TS09DEMO01",is_online=True,current_latitude=17.4500,current_longitude=78.3900,last_location_at=datetime.utcnow(),is_approved=True); db.add(partner)
        if not db.query(Address).filter_by(customer_id=customer.id,label="Home").first():
            db.add(Address(customer_id=customer.id,label="Home",address_line="Jubilee Hills Road 36",city="Hyderabad",pincode="500033",latitude=17.4550,longitude=78.3950,is_default=True))
        if not db.query(Coupon).filter_by(code="WELCOME50").first():
            db.add(Coupon(code="WELCOME50",description="50% off up to ₹100",discount_type="Percentage",discount_value=50,max_discount=100,min_order_value=199,restaurant_id=None,valid_from=datetime.utcnow()-timedelta(days=1),valid_to=datetime.utcnow()+timedelta(days=365),total_usage_limit=1000,per_user_limit=1,first_order_only=True,created_by=admin.id,is_active=True))
        if not db.query(Coupon).filter_by(code="BIRYANI20").first():
            db.add(Coupon(code="BIRYANI20",description="₹20 off demo restaurant orders",discount_type="Flat",discount_value=20,max_discount=None,min_order_value=100,restaurant_id=restaurant.id,valid_from=datetime.utcnow()-timedelta(days=1),valid_to=datetime.utcnow()+timedelta(days=365),total_usage_limit=1000,per_user_limit=5,first_order_only=False,created_by=owner.id,is_active=True))
        db.commit()
        print("Demo seed complete. Password for all demo accounts: " + PASSWORD)
        print("admin@example.com | owner@example.com | customer@example.com | partner@example.com")
        print("Restaurant: Hyderabad Demo Kitchen (17.4486, 78.3908); partner: (17.4500, 78.3900); customer address: (17.4550, 78.3950)")
        print("Coupons: WELCOME50, BIRYANI20")
    finally: db.close()

if __name__ == "__main__": main()
