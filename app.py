import sqlite3
import json
import os
import secrets
import time
from datetime import timedelta
from threading import Lock
import click
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

def load_local_env():
    """Load local key=value settings without adding another dependency."""
    env_file = Path(__file__).with_name(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

load_local_env()
app = Flask(__name__)
is_production = os.environ.get("FLASK_ENV") == "production"
configured_secret = os.environ.get("SECRET_KEY")
if is_production and not configured_secret:
    raise RuntimeError("SECRET_KEY must be set in production.")
app.config.update(
    SECRET_KEY=configured_secret or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_production,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=True,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)
if is_production:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config["OPENWEATHER_API_KEY"] = os.environ.get("OPENWEATHER_API_KEY", "")
app.config["DATABASE"] = Path(os.environ.get("DATABASE_PATH", Path(app.root_path) / "smart_agri.db"))
app.config["UPLOAD_FOLDER"] = Path(app.static_folder) / "uploads"
app.config["DATABASE"].parent.mkdir(parents=True, exist_ok=True)
app.config["UPLOAD_FOLDER"].mkdir(exist_ok=True)

# District headquarters covered by the Karnataka farmer dashboard.  Keeping this
# list here makes it simple to extend when the application is rolled out elsewhere.
KARNATAKA_DISTRICTS = (
    "Bagalkote", "Ballari", "Belagavi", "Bengaluru Rural", "Bengaluru Urban",
    "Bidar", "Chamarajanagar", "Chikkaballapur", "Chikkamagaluru", "Chitradurga",
    "Dakshina Kannada", "Davanagere", "Dharwad", "Gadag", "Hassan", "Haveri",
    "Kalaburagi", "Kodagu", "Kolar", "Koppal", "Mandya", "Mysuru", "Raichur",
    "Ramanagara", "Shivamogga", "Tumakuru", "Udupi", "Uttara Kannada",
    "Vijayapura", "Vijayanagara", "Yadgir",
)
WEATHER_CACHE_SECONDS = 600
MINIMUM_ORDER_AMOUNT = 99.0
weather_cache = {"data": [], "updated_at": None, "error": None}
login_attempts = {}
login_attempts_lock = Lock()

def password_is_safe(password):
    return (len(password) >= 12 and any(char.islower() for char in password)
            and any(char.isupper() for char in password) and any(char.isdigit() for char in password))

def login_is_rate_limited(email):
    """Allow at most five attempts per account/IP in 15 minutes."""
    now, key = time.monotonic(), f"{request.remote_addr}:{email}"
    with login_attempts_lock:
        attempts = [stamp for stamp in login_attempts.get(key, []) if now - stamp < 900]
        if len(attempts) >= 5:
            login_attempts[key] = attempts
            return True
        attempts.append(now)
        login_attempts[key] = attempts
    return False

@app.context_processor
def security_context():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf_token"], "minimum_order_amount": MINIMUM_ORDER_AMOUNT}

@app.before_request
def verify_csrf_token():
    if is_production and not request.is_secure:
        return redirect(request.url.replace("http://", "https://", 1), code=301)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            flash("For your protection, that request was blocked.", "error")
            return redirect(request.referrer or url_for("index"))
        if request.endpoint == "checkout":
            expected, supplied = session.get("csrf_token", ""), request.form.get("csrf_token", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                flash("Your form session expired. Please try again.", "error")
                return redirect(url_for("checkout"))

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), payment=(), usb=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: https://*.tile.openstreetmap.org; connect-src 'self'"
    if is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

def fetch_district_weather(district, api_key):
    """Fetch one district headquarters' current weather from OpenWeather."""
    url = "https://api.openweathermap.org/data/2.5/weather?" + urlencode({
        "q": f"{district},IN", "appid": api_key, "units": "metric"
    })
    with urlopen(url, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {
        "district": district,
        "temperature": round(data["main"]["temp"]),
        "feels_like": round(data["main"]["feels_like"]),
        "condition": data["weather"][0]["description"].title(),
        "icon": data["weather"][0]["icon"],
        "humidity": data["main"]["humidity"],
        "wind": data["wind"]["speed"],
        "rain": data.get("rain", {}).get("1h", 0),
    }

def get_district_weather():
    """Return cached live weather and refresh it at most once every 10 minutes."""
    now = datetime.now()
    cached_at = weather_cache["updated_at"]
    if cached_at and (now - cached_at).total_seconds() < WEATHER_CACHE_SECONDS:
        return weather_cache["data"], weather_cache["error"], cached_at

    api_key = app.config["OPENWEATHER_API_KEY"]
    if not api_key:
        return [], "Add an OpenWeather API key to enable live district weather.", cached_at

    results = []
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_district_weather, district, api_key): district for district in KARNATAKA_DISTRICTS}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except (HTTPError, URLError, TimeoutError, KeyError, json.JSONDecodeError):
                    # A single unavailable district should not stop the whole dashboard.
                    continue
        if not results:
            raise URLError("No district weather responses")
        results.sort(key=lambda item: KARNATAKA_DISTRICTS.index(item["district"]))
        weather_cache.update({"data": results, "updated_at": now, "error": None})
    except HTTPError as exc:
        message = "OpenWeather rejected the API key. Check the key in .env and restart the server." if exc.code == 401 else "Weather service is currently unavailable."
        weather_cache["error"] = message
    except (URLError, TimeoutError):
        weather_cache["error"] = "Could not retrieve district weather. Check the internet connection and try again."
    return weather_cache["data"], weather_cache["error"], weather_cache["updated_at"]

def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection: connection.close()

def init_db():
    database = db()
    database.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('farmer','consumer','admin')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL CHECK(price > 0),
            unit TEXT NOT NULL, image TEXT, emoji TEXT DEFAULT '🌿', farmer_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(farmer_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS cart_items (
            consumer_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
            PRIMARY KEY(consumer_id, product_id), FOREIGN KEY(consumer_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, consumer_id INTEGER NOT NULL, address TEXT NOT NULL,
            payment_method TEXT NOT NULL, total REAL NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(consumer_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, product_name TEXT NOT NULL,
            farm_name TEXT NOT NULL, price REAL NOT NULL, unit TEXT NOT NULL, quantity INTEGER NOT NULL, subtotal REAL NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
    """)
    order_columns = {row[1] for row in database.execute("PRAGMA table_info(orders)").fetchall()}
    if "latitude" not in order_columns:
        database.execute("ALTER TABLE orders ADD COLUMN latitude REAL")
        database.execute("ALTER TABLE orders ADD COLUMN longitude REAL")
        database.execute("ALTER TABLE orders ADD COLUMN location_accuracy REAL")
    item_columns = {row[1] for row in database.execute("PRAGMA table_info(order_items)").fetchall()}
    if "farmer_id" not in item_columns:
        database.execute("ALTER TABLE order_items ADD COLUMN farmer_id INTEGER")
    schema = database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
    if "'admin'" not in schema:
        database.execute("PRAGMA foreign_keys = OFF")
        database.executescript("""
            CREATE TABLE users_new (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('farmer','consumer','admin')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO users_new (id, name, email, password_hash, role, created_at)
                SELECT id, name, email, password_hash, role, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
        """)
        database.execute("PRAGMA foreign_keys = ON")
    if database.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        database.executemany("INSERT INTO products (name, price, unit, emoji, farmer_id) VALUES (?, ?, ?, ?, NULL)", [
            ("Organic Tomatoes", 45, "kg", "🍅"), ("Fresh Alphonso Mangoes", 180, "kg", "🥭"), ("Farm Fresh Spinach", 35, "bunch", "🥬")])
    database.commit()

def current_user():
    if not session.get("user_id"): return None
    row = db().execute("SELECT id, name, email, role FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return dict(row) if row else None

def product_rows(farmer_id=None):
    query = """SELECT p.id, p.name, p.price, p.unit, p.image, p.emoji, p.farmer_id,
               COALESCE(u.name, 'Smart Agri Partner Farm') AS farm
               FROM products p LEFT JOIN users u ON p.farmer_id = u.id"""
    params = ()
    if farmer_id:
        query += " WHERE p.farmer_id = ?"; params = (farmer_id,)
    return [dict(row) for row in db().execute(query + " ORDER BY p.id DESC", params).fetchall()]

@app.route("/weather")
def weather():
    user = current_user()
    if not user: return redirect(url_for("index"))
    districts, error, updated_at = get_district_weather()
    return render_template("weather.html", districts=districts, error=error, updated_at=updated_at)

@app.route("/api/weather/districts")
def district_weather_api():
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in to view weather."}), 401
    districts, error, updated_at = get_district_weather()
    return jsonify({
        "districts": districts,
        "error": error,
        "updated_at": updated_at.strftime("%d %b %Y, %I:%M %p") if updated_at else None,
    })

@app.route("/")
def index(): return render_template("school.html" if os.environ.get("SITE_MODE") == "school" else "index.html")

@app.route("/school")
def school():
    photos = [
        ("president-new.webp", "Dr. Sri Sri Sri Shivakumara Swamiji"),
        ("siddalinga-new.jpeg", "Sree Sree Siddalinga Swamiji"),
        ("shivasiddeshwara-new.jpg", "Sree Shivasiddeshwara Swamiji"),
        ("secretary.jpg", "Sri T. K. Nanjundappa"),
        ("students-achievement.jpg", "Student achievement programme"),
        ("teaching-classroom.jpg", "Classroom learning"),
        ("playground.jpg", "School playground"),
        ("yoga-standing.jpg", "Yoga and wellbeing programme"),
        ("school-assembly.jpg", "School assembly"),
        ("school-programme.jpg", "School programme"),
        ("sports-equipment.jpg", "Sports equipment"),
        ("campus-front.jpg", "School campus"),
        ("yoga-group.jpg", "Student yoga session"),
        ("learning-board.jpg", "Learning board"),
        ("yoga-rest.jpg", "Student yoga activity"),
    ]
    page = render_template("school.html")
    gallery = render_template("school_gallery_fragment.html", photos=photos)
    return page.replace("</main>", gallery + "</main>")

@app.route("/register/<role>", methods=["GET", "POST"])
def register(role):
    if role not in {"farmer", "consumer"}: return redirect(url_for("index"))
    if request.method == "POST":
        name, email, password = request.form["name"].strip(), request.form["email"].lower().strip(), request.form["password"]
        if not name or not email or not password: flash("Please complete every field.", "error")
        elif len(name) > 100 or len(email) > 254 or not password_is_safe(password):
            flash("Use a 12+ character password with uppercase, lowercase, and a number.", "error")
        else:
            try:
                cursor = db().execute("INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)", (name, email, generate_password_hash(password), role))
                db().commit(); session["user_id"] = cursor.lastrowid
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError: flash("An account with this email already exists.", "error")
    return render_template("auth.html", mode="Register", role=role)

@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):
    if role not in {"farmer", "consumer", "admin"}: return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        if login_is_rate_limited(email):
            flash("Too many sign-in attempts. Please wait 15 minutes and try again.", "error")
            return render_template("auth.html", mode="Login", role=role)
        user = db().execute("SELECT * FROM users WHERE email = ? AND role = ?", (email, role)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            return redirect(url_for("dashboard"))
        flash(f"We couldn't find a matching {role} account.", "error")
    return render_template("auth.html", mode="Login", role=role)

@app.route("/dashboard")
def dashboard():
    user = current_user()
    if not user: return redirect(url_for("login", role="consumer"))
    # Administrators see their protected platform dashboard at /dashboard too.
    if user["role"] == "admin": return admin_dashboard()
    products = product_rows(user["id"] if user["role"] == "farmer" else None)
    count = db().execute("SELECT COALESCE(SUM(quantity), 0) FROM cart_items WHERE consumer_id = ?", (user["id"],)).fetchone()[0]
    return render_template("dashboard.html", user=user, products=products, cart_count=count)

@app.route("/farmer/deliveries")
def farmer_deliveries():
    user = current_user()
    if not user or user["role"] != "farmer": return redirect(url_for("login", role="farmer"))
    orders = [dict(row) for row in db().execute("""SELECT DISTINCT o.id, o.address, o.created_at, o.latitude, o.longitude, o.location_accuracy, u.name AS consumer
        FROM orders o JOIN order_items oi ON oi.order_id=o.id JOIN users u ON u.id=o.consumer_id
        WHERE oi.farmer_id=? ORDER BY o.id DESC""", (user["id"],)).fetchall()]
    return render_template("farmer_deliveries.html", orders=orders)

@app.route("/admin")
def admin_dashboard():
    user = current_user()
    if not user or user["role"] != "admin":
        flash("Administrator access is required.", "error")
        return redirect(url_for("index"))
    database = db()
    stats = {"farmers": database.execute("SELECT COUNT(*) FROM users WHERE role='farmer'").fetchone()[0], "consumers": database.execute("SELECT COUNT(*) FROM users WHERE role='consumer'").fetchone()[0], "products": database.execute("SELECT COUNT(*) FROM products").fetchone()[0], "orders": database.execute("SELECT COUNT(*) FROM orders").fetchone()[0], "revenue": database.execute("SELECT COALESCE(SUM(total), 0) FROM orders").fetchone()[0]}
    recent_orders = [dict(row) for row in database.execute("""SELECT o.id, u.name AS customer, o.total, o.payment_method, o.created_at, (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id=o.id) AS item_count FROM orders o JOIN users u ON o.consumer_id=u.id ORDER BY o.id DESC LIMIT 8""").fetchall()]
    recent_products = [dict(row) for row in database.execute("""SELECT p.name, p.price, p.unit, COALESCE(u.name, 'Smart Agri Partner Farm') AS farmer FROM products p LEFT JOIN users u ON p.farmer_id=u.id ORDER BY p.id DESC LIMIT 6""").fetchall()]
    return render_template("admin_dashboard.html", user=user, stats=stats, recent_orders=recent_orders, recent_products=recent_products)

@app.cli.command("create-admin")
@click.option("--name", prompt="Administrator name")
@click.option("--email", prompt="Administrator email")
def create_admin(name, email):
    """Create a protected administrator account."""
    password = click.prompt("Administrator password", hide_input=True, confirmation_prompt=True)
    try:
        db().execute("INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'admin')", (name.strip(), email.lower().strip(), generate_password_hash(password)))
        db().commit()
        click.echo("Administrator account created. Log in at /login/admin.")
    except sqlite3.IntegrityError:
        click.echo("That email address already has an account.", err=True)

@app.route("/products/add", methods=["POST"])
def add_product():
    user = current_user()
    if not user or user["role"] != "farmer": return redirect(url_for("index"))
    try:
        price = float(request.form["price"])
        if not (0 < price <= 100000): raise ValueError
    except ValueError:
        flash("Enter a valid price.", "error"); return redirect(url_for("dashboard"))
    name, unit = request.form["name"].strip(), request.form["unit"].strip() or "item"
    if not name or len(name) > 120 or len(unit) > 30:
        flash("Enter a valid product name and unit.", "error"); return redirect(url_for("dashboard"))
    image_name = None; image = request.files.get("image")
    if image and image.filename:
        suffix = Path(secure_filename(image.filename)).suffix.lower()
        signature = image.stream.read(16); image.stream.seek(0)
        valid_image = ((suffix in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff"))
                       or (suffix == ".png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
                       or (suffix == ".webp" and signature.startswith(b"RIFF") and signature[8:12] == b"WEBP"))
        if not valid_image:
            flash("Use a JPG, PNG, or WEBP image.", "error"); return redirect(url_for("dashboard"))
        image_name = f"{datetime.now():%Y%m%d%H%M%S%f}{suffix}"; image.save(app.config["UPLOAD_FOLDER"] / image_name)
    db().execute("INSERT INTO products (name, price, unit, image, farmer_id) VALUES (?, ?, ?, ?, ?)", (name, price, unit, image_name, user["id"]))
    db().commit(); flash("Your product is now live in the marketplace.", "success")
    return redirect(url_for("dashboard"))

@app.route("/cart/add/<int:product_id>", methods=["POST"])
def add_to_cart(product_id):
    user = current_user()
    if not user or user["role"] != "consumer": return redirect(url_for("login", role="consumer"))
    if db().execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
        db().execute("INSERT INTO cart_items (consumer_id, product_id, quantity) VALUES (?, ?, 1) ON CONFLICT(consumer_id, product_id) DO UPDATE SET quantity = quantity + 1", (user["id"], product_id)); db().commit()
        flash("Added to your basket.", "success")
    return redirect(url_for("dashboard"))

def get_cart(consumer_id):
    rows = db().execute("""SELECT p.id, p.name, p.price, p.unit, p.image, p.emoji, p.farmer_id, ci.quantity,
        COALESCE(u.name, 'Smart Agri Partner Farm') AS farm FROM cart_items ci JOIN products p ON ci.product_id = p.id
        LEFT JOIN users u ON p.farmer_id = u.id WHERE ci.consumer_id = ?""", (consumer_id,)).fetchall()
    return [{**dict(row), "total": row["price"] * row["quantity"]} for row in rows]

@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    user = current_user()
    if not user or user["role"] != "consumer": return redirect(url_for("login", role="consumer"))
    items = get_cart(user["id"])
    if not items: flash("Your basket is empty.", "error"); return redirect(url_for("dashboard"))
    total = sum(item["total"] for item in items)
    if request.method == "POST":
        if total < MINIMUM_ORDER_AMOUNT:
            flash(f"The minimum order amount is ₹{MINIMUM_ORDER_AMOUNT:.0f}. Add ₹{MINIMUM_ORDER_AMOUNT - total:.2f} more to continue.", "error")
            return redirect(url_for("checkout"))
        address, payment = request.form["address"].strip(), request.form.get("payment")
        try:
            latitude = float(request.form["latitude"]) if request.form.get("latitude") else None
            longitude = float(request.form["longitude"]) if request.form.get("longitude") else None
            accuracy = float(request.form["location_accuracy"]) if request.form.get("location_accuracy") else None
            if latitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180): raise ValueError
        except (ValueError, TypeError):
            latitude = longitude = accuracy = None
        if not address or len(address) > 500 or payment not in {"UPI", "Card", "Cash on delivery"}: flash("Enter a valid delivery address and choose payment.", "error")
        else:
            now = datetime.now().strftime("%d %b %Y, %I:%M %p")
            cursor = db().execute("INSERT INTO orders (consumer_id, address, payment_method, total, created_at, latitude, longitude, location_accuracy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (user["id"], address, payment, total, now, latitude, longitude, accuracy))
            order_id = cursor.lastrowid
            db().executemany("INSERT INTO order_items (order_id, product_name, farm_name, price, unit, quantity, subtotal, farmer_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(order_id, i["name"], i["farm"], i["price"], i["unit"], i["quantity"], i["total"], i["farmer_id"]) for i in items])
            db().execute("DELETE FROM cart_items WHERE consumer_id = ?", (user["id"],)); db().commit()
            order = {"id": order_id, "buyer": user["name"], "address": address, "payment": payment, "items": items, "total": total, "date": now}
            return render_template("order_success.html", order=order)
    return render_template("checkout.html", items=items, total=total, minimum_order_amount=MINIMUM_ORDER_AMOUNT)

@app.route("/invoice/<int:order_id>.pdf")
def invoice(order_id):
    user = current_user()
    order = db().execute("SELECT * FROM orders WHERE id = ? AND consumer_id = ?", (order_id, user["id"] if user else -1)).fetchone()
    if not order: return redirect(url_for("index"))
    items = db().execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    lines = ["SMART AGRI - ORDER INVOICE", "", f"Invoice no: SA-{order_id}", f"Date: {order['created_at']}", f"Delivery: {order['address']}", f"Payment: {order['payment_method']}", "", "ITEMS"]
    lines += [f"{i['product_name']} x {i['quantity']} @ Rs. {i['price']:.2f} = Rs. {i['subtotal']:.2f}" for i in items]
    lines += ["", f"TOTAL PAID: Rs. {order['total']:.2f}", "", "Thank you for supporting local farmers."]
    content = "BT\n/F1 12 Tf\n50 760 Td\n" + "\n".join(f"({line.replace('(', '[').replace(')', ']')}) Tj 0 -18 Td" for line in lines) + "\nET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>", "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream"]
    pdf, offsets = "%PDF-1.4\n", []
    for n, obj in enumerate(objects, 1): offsets.append(len(pdf.encode())); pdf += f"{n} 0 obj\n{obj}\nendobj\n"
    start = len(pdf.encode()); pdf += f"xref\n0 6\n0000000000 65535 f \n" + "".join(f"{x:010d} 00000 n \n" for x in offsets) + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF"
    return send_file(BytesIO(pdf.encode()), mimetype="application/pdf", as_attachment=True, download_name=f"smart-agri-invoice-{order_id}.pdf")

def recommend_crops(nitrogen, phosphorus, potassium, ph, temperature, rainfall):
    crops = [("Rice",75,45,45,6.2,28,180,"Warm, wet conditions with balanced soil nutrients suit paddy cultivation."),("Wheat",70,40,35,6.8,21,75,"Cooler conditions and moderate rainfall are favourable for wheat."),("Maize",80,50,45,6.3,25,100,"Maize performs well in warm weather with balanced NPK levels."),("Cotton",85,40,45,7.2,29,65,"Warm, relatively dry conditions are a strong fit for cotton."),("Chickpea",25,45,35,7.3,22,55,"Lower nitrogen and drier conditions support this nitrogen-fixing pulse."),("Sugarcane",95,55,55,6.5,28,150,"High nutrients, warmth, and reliable moisture favour sugarcane.")]
    ranked=[]
    for name,n,p,k,ideal_ph,temp,rain,reason in crops:
        score=max(45,round(100-(abs(nitrogen-n)/45+abs(phosphorus-p)/35+abs(potassium-k)/35+abs(ph-ideal_ph)/1.4+abs(temperature-temp)/12+abs(rainfall-rain)/110)*9)); ranked.append({"name":name,"score":score,"reason":reason})
    return sorted(ranked,key=lambda crop:crop["score"],reverse=True)[:3]

def fertilizer_plan(crop):
    """Create a baseline per-acre plan when only the crop is known.

    Soil-test results are deliberately not guessed.  Farmers can use this as a
    starting guide and should adjust it with a local soil-health recommendation.
    """
    targets = {"Rice": (50, 25, 25), "Wheat": (45, 23, 18), "Maize": (60, 30, 25), "Cotton": (48, 24, 24), "Sugarcane": (80, 35, 35), "Vegetables": (55, 28, 30)}
    target_n, target_p, target_k = targets[crop]
    products = [
        ("Urea (46% N)", target_n / .46, "Split into 2–3 applications; avoid applying before heavy rain."),
        ("DAP (18-46-0)", target_p / .46, "Apply mainly as a basal dose during sowing or transplanting."),
        ("MOP (0-0-60)", target_k / .60, "Apply as a basal dose or split according to crop stage."),
    ]
    return [{"name": name, "amount": round(amount, 1), "timing": timing} for name, amount, timing in products]

@app.route("/fertilizer-guide")
def fertilizer_guide():
    user = current_user()
    if not user or user["role"] != "farmer": return redirect(url_for("login", role="farmer"))
    crops = ("Rice", "Wheat", "Maize", "Cotton", "Sugarcane", "Vegetables")
    crop = request.args.get("crop", "Rice")
    if crop not in crops: crop = "Rice"
    return render_template("fertilizer_guide.html", plan=fertilizer_plan(crop), crop=crop, crops=crops)

@app.route("/api/fertilizer-guide/<crop>")
def fertilizer_guide_api(crop):
    user = current_user()
    if not user or user["role"] != "farmer":
        return jsonify({"error": "Farmer sign-in is required."}), 401
    crops = {"Rice", "Wheat", "Maize", "Cotton", "Sugarcane", "Vegetables"}
    if crop not in crops:
        return jsonify({"error": "Crop not supported."}), 404
    return jsonify({"crop": crop, "plan": fertilizer_plan(crop)})

@app.route("/government-schemes")
def government_schemes():
    user = current_user()
    if not user or user["role"] != "farmer": return redirect(url_for("login", role="farmer"))
    schemes = [
        {"name": "PM-KISAN Samman Nidhi", "icon": "₹", "summary": "Income-support scheme for eligible landholding farmer families. Registration, status, and eKYC are available on the official portal.", "link": "https://pmkisan.gov.in/", "action": "Check eligibility / register"},
        {"name": "Soil Health Card", "icon": "◒", "summary": "Get soil nutrient status and fertilizer or soil-amendment advice based on your holding’s soil test.", "link": "https://soilhealth.dac.gov.in/", "action": "Visit Soil Health portal"},
        {"name": "e-NAM", "icon": "↗", "summary": "National Agriculture Market connects participating mandis for transparent price discovery, wider market access, and digital trade.", "link": "https://enam.gov.in/web/stakeholders-Involved/farmers", "action": "Explore farmer registration"},
        {"name": "Kisan Credit Card", "icon": "□", "summary": "Access the official PM-KISAN portal for KCC form and scheme-related farmer resources. Verify local bank eligibility before applying.", "link": "https://pmkisan.gov.in/", "action": "Open official resources"},
    ]
    return render_template("government_schemes.html", schemes=schemes)

@app.route("/crop-recommendation", methods=["GET", "POST"])
def crop_recommendation():
    user = current_user()
    if not user or user["role"] != "farmer": return redirect(url_for("login", role="farmer"))
    recommendations, values = None, {}
    if request.method == "POST":
        try:
            values={key:float(request.form[key]) for key in ("nitrogen","phosphorus","potassium","ph","temperature","rainfall")}; recommendations=recommend_crops(**values)
        except (KeyError, ValueError): flash("Please enter valid numeric values for all soil parameters.", "error")
    return render_template("crop_recommendation.html", recommendations=recommendations, values=values)

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("index"))

with app.app_context(): init_db()
if __name__ == "__main__": app.run(host="0.0.0.0", port=8080, debug=False)
