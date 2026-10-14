import os, io, json, re, requests, sqlite3, secrets, time, datetime
from functools import wraps
from flask import Flask, request, jsonify, send_file, session
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from google import genai
from google.genai import types
from gtts import gTTS
load_dotenv()
app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)  # set SECRET_KEY in .env or logins reset on every restart
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30))
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
LANGS = {"en": "English", "kn": "Kannada", "hi": "Hindi"}
KB = open("farm_knowledge.txt", encoding="utf-8").read()
KVK = {
 "en": "I am not sure. Please call the Kisan Call Centre 1800-180-1551 or visit your nearest Krishi Vigyan Kendra.",
 "kn": "ನನಗೆ ಖಚಿತವಿಲ್ಲ. ಕಿಸಾನ್ ಕಾಲ್ ಸೆಂಟರ್ 1800-180-1551 ಗೆ ಕರೆ ಮಾಡಿ ಅಥವಾ ಹತ್ತಿರದ ಕೃಷಿ ವಿಜ್ಞಾನ ಕೇಂದ್ರಕ್ಕೆ ಭೇಟಿ ನೀಡಿ.",
 "hi": "मुझे यकीन नहीं है। कृपया किसान कॉल सेंटर 1800-180-1551 पर कॉल करें या नज़दीकी कृषि विज्ञान केंद्र जाएँ।"}
RISK = {
 "low": {"en": "Low disease risk for the next 3 days.", "kn": "ಮುಂದಿನ 3 ದಿನ ರೋಗದ ಅಪಾಯ ಕಡಿಮೆ.", "hi": "अगले 3 दिन रोग का खतरा कम है।"},
 "medium": {"en": "Medium risk. Check your leaves every day.", "kn": "ಮಧ್ಯಮ ಅಪಾಯ. ಪ್ರತಿದಿನ ಎಲೆಗಳನ್ನು ಪರೀಕ್ಷಿಸಿ.", "hi": "मध्यम खतरा। रोज़ पत्तियाँ जाँचें।"},
 "high": {"en": "High blight risk. Avoid watering from above and check leaves daily.", "kn": "ಅಂಗಮಾರಿ ರೋಗದ ಹೆಚ್ಚಿನ ಅಪಾಯ. ಮೇಲಿನಿಂದ ನೀರು ಹಾಕಬೇಡಿ, ಪ್ರತಿದಿನ ಎಲೆ ಪರೀಕ್ಷಿಸಿ.", "hi": "झुलसा रोग का ज़्यादा खतरा। ऊपर से सिंचाई न करें, रोज़ पत्तियाँ जाँचें।"}}


# ---------------- database, accounts ----------------
DB = os.environ.get("DB_PATH", "krishimitra.db")
SLOTS = ["10:00", "11:00", "12:00", "14:00", "15:00", "16:00"]
FAILS = {}  # phone -> (count, first_fail_time) for simple login throttling

def today(): return datetime.date.today().isoformat()

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.executescript("""
        create table if not exists users(id integer primary key autoincrement, name text, phone text unique, pw_hash text,
            role text default 'farmer', village text, crop text, lang text default 'en', speciality text, created text);
        create table if not exists appointments(id integer primary key autoincrement, farmer_id int, expert_id int, day text, slot text,
            note text, status text default 'pending', created text);
        create table if not exists history(id integer primary key autoincrement, user_id int, crop text, disease text,
            confidence int, severity text, created text);
        create table if not exists price_snap(day text, commodity text, state text, avg int, primary key(day, commodity, state));""")
        if not c.execute("select 1 from users where role='expert'").fetchone():  # demo experts so booking works on day one
            for n, ph, sp in (("Dr. Demo Pathologist", "9000000001", "Plant pathology"), ("Demo Soil Scientist", "9000000002", "Soil & nutrients")):
                c.execute("insert into users(name,phone,pw_hash,role,speciality,created) values(?,?,?,?,?,?)",
                          (n, ph, generate_password_hash(secrets.token_hex(8)), "expert", sp, today()))
init_db()

def me():
    uid = session.get("uid")
    if not uid: return None
    with db() as c: return c.execute("select * from users where id=?", (uid,)).fetchone()

def pub(u): return {k: u[k] for k in ("id", "name", "phone", "role", "village", "crop", "lang", "speciality")}

def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            u = me()
            if not u: return jsonify(error="Please log in"), 401
            if role and u["role"] != role: return jsonify(error="Not allowed"), 403
            return fn(u, *a, **k)
        return w
    return deco

@app.post("/api/signup")
def signup():
    j = request.get_json(silent=True) or {}
    name = str(j.get("name", "")).strip()[:60]; phone = str(j.get("phone", "")).strip(); pw = str(j.get("password", ""))
    role = j.get("role", "farmer"); lang = j.get("lang", "en")
    if not name: return jsonify(error="Enter your name"), 400
    if not re.fullmatch(r"[6-9]\d{9}", phone): return jsonify(error="Enter a valid 10-digit mobile number"), 400
    if len(pw) < 6: return jsonify(error="Password must be at least 6 characters"), 400
    if role not in ("farmer", "expert"): return jsonify(error="Bad role"), 400
    if lang not in LANGS: lang = "en"
    if role == "expert":
        code = os.environ.get("EXPERT_CODE")
        if not code or j.get("expert_code") != code: return jsonify(error="Wrong expert code"), 403
    try:
        with db() as c:
            cur = c.execute("insert into users(name,phone,pw_hash,role,village,crop,lang,speciality,created) values(?,?,?,?,?,?,?,?,?)",
                (name, phone, generate_password_hash(pw), role, str(j.get("village", ""))[:60], str(j.get("crop", ""))[:40], lang,
                 str(j.get("speciality", ""))[:60] if role == "expert" else "", today()))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return jsonify(error="This phone number is already registered. Please log in."), 409
    session.clear(); session["uid"] = uid; session.permanent = True
    return jsonify(user=pub(me()))

@app.post("/api/login")
def login():
    j = request.get_json(silent=True) or {}; phone = str(j.get("phone", "")).strip()
    n, t0 = FAILS.get(phone, (0, 0))
    if n >= 5 and time.time() - t0 < 300: return jsonify(error="Too many tries. Wait 5 minutes."), 429
    with db() as c: u = c.execute("select * from users where phone=?", (phone,)).fetchone()
    if not u or not check_password_hash(u["pw_hash"], str(j.get("password", ""))):
        FAILS[phone] = (n + 1 if time.time() - t0 < 300 else 1, t0 if n and time.time() - t0 < 300 else time.time())
        return jsonify(error="Wrong phone number or password"), 401
    FAILS.pop(phone, None); session.clear(); session["uid"] = u["id"]; session.permanent = True
    return jsonify(user=pub(u))

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def whoami():
    u = me(); return jsonify(user=pub(u) if u else None)

@app.get("/api/history")
@login_required()
def get_history(u):
    with db() as c:
        rows = c.execute("select created,crop,disease,confidence,severity from history where user_id=? order by id desc limit 30", (u["id"],)).fetchall()
    return jsonify(history=[dict(r) for r in rows])

# ---------------- experts and appointments ----------------
@app.get("/api/experts")
def experts():
    u = me()
    with db() as c: rows = c.execute("select id,name,speciality,phone from users where role='expert' order by id").fetchall()
    return jsonify(experts=[{"id": r["id"], "name": r["name"], "speciality": r["speciality"], **({"phone": r["phone"]} if u else {})} for r in rows])

def free_slots(c, expert_id, day):
    taken = {r["slot"] for r in c.execute("select slot from appointments where expert_id=? and day=? and status in ('pending','confirmed')", (expert_id, day))}
    return [s for s in SLOTS if s not in taken]

def valid_day(day):
    try: d = datetime.date.fromisoformat(str(day))
    except ValueError: return False
    return datetime.date.today() <= d <= datetime.date.today() + datetime.timedelta(days=30)

@app.get("/api/slots")
def slots():
    a = request.args
    if not valid_day(a.get("date")): return jsonify(free=[])
    with db() as c: return jsonify(free=free_slots(c, a.get("expert_id", type=int), a["date"]))

@app.post("/api/appointments")
@login_required("farmer")
def book(u):
    j = request.get_json(silent=True) or {}; eid = j.get("expert_id"); day = j.get("date"); slot = j.get("time")
    if not valid_day(day) or slot not in SLOTS: return jsonify(error="Choose a valid date (next 30 days) and time"), 400
    with db() as c:
        if not c.execute("select 1 from users where id=? and role='expert'", (eid,)).fetchone(): return jsonify(error="Expert not found"), 404
        if slot not in free_slots(c, eid, day): return jsonify(error="That time is already booked. Choose another."), 409
        c.execute("insert into appointments(farmer_id,expert_id,day,slot,note,created) values(?,?,?,?,?,?)",
                  (u["id"], eid, day, slot, str(j.get("note", ""))[:200], today()))
    return jsonify(ok=True)

@app.get("/api/appointments")
@login_required()
def my_appointments(u):
    if u["role"] == "expert":
        q = ("select a.id,a.day as date,a.slot as time,a.note,a.status,f.name as farmer_name,f.village,f.crop,f.phone "
             "from appointments a join users f on f.id=a.farmer_id where a.expert_id=? order by a.day,a.slot")
    else:
        q = ("select a.id,a.day as date,a.slot as time,a.note,a.status,e.name as expert_name,e.speciality,e.phone "
             "from appointments a join users e on e.id=a.expert_id where a.farmer_id=? order by a.day,a.slot")
    with db() as c: return jsonify(appointments=[dict(r) for r in c.execute(q, (u["id"],))])

@app.post("/api/appointments/<int:aid>/status")
@login_required()
def set_status(u, aid):
    s = (request.get_json(silent=True) or {}).get("status")
    allowed = {"expert": ("confirmed", "declined"), "farmer": ("cancelled",)}[u["role"]]
    col = "expert_id" if u["role"] == "expert" else "farmer_id"
    if s not in allowed: return jsonify(error="Not allowed"), 403
    with db() as c:
        n = c.execute(f"update appointments set status=? where id=? and {col}=? and status in ('pending','confirmed')", (s, aid, u["id"])).rowcount
    return jsonify(ok=True) if n else (jsonify(error="Appointment not found"), 404)

# ---------------- market prices ----------------
DEMO_BASE = {"Tomato": 2000, "Onion": 1800, "Potato": 2200, "Green Chilli": 4500, "Brinjal": 2500}  # Rs/quintal, SAMPLE numbers
DEMO_MARKETS = (("Mysuru", 1.0), ("Bengaluru", 1.12), ("Hassan", 0.93), ("Mandya", 0.97), ("Kolar", 1.2))
MKT_CACHE = {}

def fetch_mandi(state, commodity):
    key = os.environ.get("DATA_GOV_KEY")
    if not key: return None
    r = requests.get("https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070", timeout=10, params={
        "api-key": key, "format": "json", "limit": 300, "filters[state.keyword]": state, "filters[commodity]": commodity})
    recs = r.json().get("records", [])
    for x in recs: x["_d"] = datetime.datetime.strptime(x["arrival_date"], "%d/%m/%Y").date()
    if not recs: return None
    latest = max(x["_d"] for x in recs); by = {}
    for x in recs:
        if x["_d"] != latest: continue
        try: by[x["market"]] = dict(market=x["market"], district=x["district"], min=int(float(x["min_price"])), max=int(float(x["max_price"])), modal=int(float(x["modal_price"])))
        except (ValueError, KeyError, TypeError): pass
    return (latest.isoformat(), list(by.values())) if by else None

@app.get("/api/market")
def market():
    commodity = request.args.get("commodity", "Tomato"); state = request.args.get("state", "Karnataka")[:30]
    if commodity not in DEMO_BASE: return jsonify(error="unknown commodity"), 400
    hit = MKT_CACHE.get((state, commodity))
    if hit and time.time() - hit[0] < 1800: return jsonify(hit[1])
    try: got = fetch_mandi(state, commodity)
    except Exception: got = None
    demo = got is None
    if demo:
        b = DEMO_BASE[commodity]; day = today()
        rows = [dict(market=m, district=m, min=round(b * f * .7), max=round(b * f * 1.3), modal=round(b * f)) for m, f in DEMO_MARKETS]
    else: day, rows = got
    avg = round(sum(r["modal"] for r in rows) / len(rows)); best = max(rows, key=lambda r: r["modal"]); change = None
    if not demo:
        with db() as c:
            prev = c.execute("select avg from price_snap where commodity=? and state=? and day<? order by day desc limit 1", (commodity, state, day)).fetchone()
            c.execute("insert or replace into price_snap values(?,?,?,?)", (day, commodity, state, avg))
        if prev and prev["avg"]: change = round((avg - prev["avg"]) * 100 / prev["avg"], 1)
    out = dict(demo=demo, date=day, commodity=commodity, rows=sorted(rows, key=lambda r: -r["modal"]), avg=avg, best=dict(market=best["market"], modal=best["modal"]), change=change)
    MKT_CACHE[(state, commodity)] = (time.time(), out)
    return jsonify(out)

@app.get("/")
def home(): return app.send_static_file("index.html")

@app.post("/api/diagnose")
def diagnose():
    f = request.files.get("image"); lang = request.form.get("lang", "en")
    if not f: return jsonify(error="no image"), 400
    prompt = f"""You are an agriculture assistant for small farmers in India. Look at this plant photo.
Reply ONLY with JSON keys: is_plant (bool), crop, disease (name), confidence (0-100 integer), severity (low|medium|high),
cause (one short sentence), treatment (array of 3 short steps), prevention (array of 3 short steps).
Write all text values in {LANGS.get(lang, 'English')} using simple words. Prefer organic and cultural steps.
Never give pesticide doses; tell the farmer to ask a Krishi Vigyan Kendra for the dose."""
    try:
        r = client.models.generate_content(model=MODEL, config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
            contents=[types.Part.from_bytes(data=f.read(), mime_type=f.mimetype or "image/jpeg"), prompt])
        d = json.loads(re.sub(r"^```json|```$", "", r.text.strip()).strip())
    except Exception:
        return jsonify(uncertain=True, message=KVK.get(lang, KVK["en"]))
    if not d.get("is_plant") or int(d.get("confidence", 0)) < 60:
        d["uncertain"] = True; d["message"] = KVK.get(lang, KVK["en"])
    u = me()
    if u and not d.get("uncertain"):
        try:
            with db() as c: c.execute("insert into history(user_id,crop,disease,confidence,severity,created) values(?,?,?,?,?,?)",
                (u["id"], str(d.get("crop", ""))[:60], str(d.get("disease", ""))[:100], int(d.get("confidence", 0)), str(d.get("severity", ""))[:10], today()))
        except Exception: pass
    return jsonify(d)

@app.post("/api/ask")
def ask():
    j = request.json; lang = j.get("lang", "en")
    prompt = f"""Answer the farmer's question in {LANGS.get(lang, 'English')} in at most 3 short sentences, using ONLY these facts.
If the facts do not cover it, reply exactly NOT_SURE.
Facts:
{KB}
Question: {j.get('q', '')}"""
    try: t = client.models.generate_content(model=MODEL, contents=prompt).text.strip()
    except Exception: t = "NOT_SURE"
    return jsonify(answer=KVK.get(lang, KVK["en"]) if "NOT_SURE" in t else t)

@app.get("/api/risk")
def risk():
    a = request.args; lang = a.get("lang", "en")
    w = requests.get("https://api.open-meteo.com/v1/forecast", timeout=10, params=dict(
        latitude=a.get("lat", 12.2958), longitude=a.get("lon", 76.6394), forecast_days=3,
        hourly="temperature_2m,relative_humidity_2m,precipitation")).json()["hourly"]
    n = len(w["time"]); t = sum(w["temperature_2m"]) / n; h = sum(w["relative_humidity_2m"]) / n; p = sum(w["precipitation"])
    s = (2 if h >= 85 else 1 if h >= 75 else 0) + (1 if 18 <= t <= 28 else 0) + (1 if p >= 10 else 0)
    lvl = "high" if s >= 3 else "medium" if s == 2 else "low"
    return jsonify(level=lvl, temp=round(t), humidity=round(h), rain=round(p, 1), message=RISK[lvl].get(lang, RISK[lvl]["en"]))

@app.get("/api/communities")
def communities(): return jsonify(json.load(open("communities.json", encoding="utf-8")))

@app.post("/api/communities")
@login_required()
def add_community(u):
    j = request.json; db = json.load(open("communities.json", encoding="utf-8"))
    if not re.fullmatch(r"https://chat\.whatsapp\.com/[A-Za-z0-9]+", str(j.get("link", ""))): return jsonify(error="invalid link"), 400
    db["communities"].append({k: str(j.get(k, ""))[:80] if k != "link" else j["link"] for k in ("name", "crop", "village", "link")})
    json.dump(db, open("communities.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return jsonify(ok=True)

@app.post("/api/tts")
def tts():
    j = request.json; b = io.BytesIO()
    gTTS(j["text"][:500], lang=j.get("lang", "en")).write_to_fp(b); b.seek(0)
    return send_file(b, mimetype="audio/mpeg")

if __name__ == "__main__": app.run(debug=True, port=5000)
