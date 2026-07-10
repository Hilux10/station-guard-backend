"""
מערכת שמירת תחנות - Backend
Flask + Supabase (PostgreSQL) + SMS (textbee.dev דרך מכשיר אנדרואיד אישי, או SMS4Free)
מיועד לדיפלוי על Render.
"""
import os
import functools
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify, session, render_template, redirect, url_for
from flask_cors import CORS
from supabase import create_client, Client
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# הגדרות / חיבורים
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
FLASK_SECRET = os.environ.get("FLASK_SECRET", "dev-secret-change-in-render-env")

# איזה ספק SMS להשתמש בו: 'textbee' (מכשיר אנדרואיד אישי, ברירת מחדל) או 'sms4free'
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "textbee").strip().lower()

# textbee.dev - שולח SMS דרך אפליקציה שמותקנת על מכשיר אנדרואיד (למשל הסמסונג שלך)
# באמצעות ה-SIM וחבילת הסלולר שכבר יש לך שם - חינם מעבר לחבילה הקיימת.
TEXTBEE_API_KEY = os.environ.get("TEXTBEE_API_KEY", "")
TEXTBEE_DEVICE_ID = os.environ.get("TEXTBEE_DEVICE_ID", "")

# SMS4Free - חלופה בתשלום זול אם לא תרצה להסתמך על מכשיר פיזי דלוק/מחובר
SMS4FREE_KEY = os.environ.get("SMS4FREE_KEY", "")
SMS4FREE_USER = os.environ.get("SMS4FREE_USER", "")
SMS4FREE_PASS = os.environ.get("SMS4FREE_PASS", "")
SMS4FREE_SENDER = os.environ.get("SMS4FREE_SENDER", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)
app.secret_key = FLASK_SECRET
CORS(app, supports_credentials=True)  # מאפשר קריאות מה-PWA שיושב ב-GitHub Pages (דומיין אחר)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_il_phone(phone: str) -> str:
    """
    ממיר מספר ישראלי מקומי (05X-XXXXXXX) לפורמט בינלאומי (+9725XXXXXXXX)
    שרוב שירותי ה-SMS (כולל textbee) מצפים לו.
    """
    if not phone:
        return phone
    p = phone.strip().replace("-", "").replace(" ", "")
    if p.startswith("+972"):
        return p
    if p.startswith("972"):
        return "+" + p
    if p.startswith("0"):
        return "+972" + p[1:]
    return p


# ---------------------------------------------------------------------------
# שליחת SMS - טעינה דינמית של הספק הפעיל
# ---------------------------------------------------------------------------
def _send_via_textbee(phone: str, message: str):
    """
    שולח SMS דרך textbee.dev - מפעיל את האפליקציה שמותקנת על מכשיר האנדרואיד שלך
    (למשל ה-Samsung S25 Ultra) שתשלח את ההודעה דרך ה-SIM הפיזי שנמצא בו.
    התקנה: https://textbee.dev - התקן את אפליקציית האנדרואיד, קבל API key ו-Device ID.
    """
    if not (TEXTBEE_API_KEY and TEXTBEE_DEVICE_ID):
        return False, "חסרים פרטי חיבור ל-textbee (TEXTBEE_API_KEY / TEXTBEE_DEVICE_ID)"
    try:
        resp = requests.post(
            f"https://api.textbee.dev/api/v1/gateway/devices/{TEXTBEE_DEVICE_ID}/send-sms",
            headers={"x-api-key": TEXTBEE_API_KEY},
            json={"recipients": [normalize_il_phone(phone)], "message": message},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return True, None
        return False, f"textbee error {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def _send_via_sms4free(phone: str, message: str):
    """
    שולח SMS דרך SMS4Free.
    ה-API הזה מבוסס על התיעוד הכללי של SMS4Free (https://www.sms4free.co.il/outcome-sms-api).
    לאחר שתירשם ותקבל key/user/pass/sender אמיתיים, מומלץ לוודא מול דף ה-API בחשבון שלך
    שהשמות של השדות לא השתנו, ולעדכן כאן במידת הצורך.
    """
    if not (SMS4FREE_KEY and SMS4FREE_USER and SMS4FREE_PASS):
        return False, "חסרים פרטי חיבור ל-SMS4Free (משתני סביבה)"
    try:
        resp = requests.post(
            "https://api.sms4free.co.il/ApiSMS/v2/SendSMS",
            json={
                "key": SMS4FREE_KEY,
                "user": SMS4FREE_USER,
                "pass": SMS4FREE_PASS,
                "sender": SMS4FREE_SENDER or SMS4FREE_USER,
                "recipient": phone,
                "msg": message,
            },
            timeout=15,
        )
        data = resp.json()
        # לפי תיעוד SMS4Free: status חיובי = הצלחה, שלילי = קוד שגיאה
        status = data.get("status", -999)
        if isinstance(status, (int, float)) and status > 0:
            return True, None
        return False, f"SMS4Free error: {data}"
    except Exception as e:
        return False, str(e)


def send_sms(phone: str, message: str):
    """
    נקודת הכניסה היחידה לשליחת SMS בכל שאר הקוד.
    בוחר ספק לפי משתנה הסביבה SMS_PROVIDER ('textbee' או 'sms4free').
    """
    if SMS_PROVIDER == "sms4free":
        return _send_via_sms4free(phone, message)
    return _send_via_textbee(phone, message)


# ---------------------------------------------------------------------------
# עזרי גישה לדאטה - עובדים / תחנות
# ---------------------------------------------------------------------------
def get_employee_by_number(emp_id: str):
    res = (
        supabase.table("employees")
        .select("*, stations(id,name,briefing_html)")
        .eq("emp_id", emp_id)
        .execute()
    )
    if not res.data:
        return None
    emp = res.data[0]
    if emp.get("suspended"):
        return None
    return emp


def get_active_shift(employee_id: str):
    res = (
        supabase.table("shifts")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("status", "active")
        .order("start_time", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# API - עובד: התחברות
# ---------------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True) or {}
    employee_number = str(body.get("employee_number", "")).strip()
    if not employee_number:
        return jsonify({"ok": False, "error": "יש להזין מספר עובד"}), 400

    emp = get_employee_by_number(employee_number)
    if not emp:
        return jsonify({"ok": False, "error": "מספר עובד לא נמצא או שהעובד אינו פעיל"}), 404

    station = emp.get("stations")
    active_shift = get_active_shift(emp["emp_id"])

    return jsonify({
        "ok": True,
        "employee": {
            "id": emp["emp_id"],
            "name": emp["name"],
            "role": emp["role"],
            "phone": emp.get("phone"),
        },
        "station": {
            "id": station["id"],
            "name": station["name"],
            "briefing_html": station["briefing_html"],
        } if station else None,
        "active_shift": active_shift,
    })


# ---------------------------------------------------------------------------
# API - עובד: אישור קריאת תדריך
# ---------------------------------------------------------------------------
@app.route("/api/acknowledge", methods=["POST"])
def api_acknowledge():
    body = request.get_json(force=True) or {}
    employee_id = body.get("employee_id")
    station_id = body.get("station_id")
    employee_name = body.get("employee_name", "")
    station_name = body.get("station_name", "")

    if not (employee_id and station_id):
        return jsonify({"ok": False, "error": "חסרים נתונים"}), 400

    supabase.table("briefing_acks").insert({
        "employee_id": employee_id,
        "station_id": station_id,
        "employee_name": employee_name,
        "station_name": station_name,
        "acknowledged_at": now_iso(),
    }).execute()

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API - עובד: תחילת / סיום משמרת
# ---------------------------------------------------------------------------
@app.route("/api/shift/start", methods=["POST"])
def api_shift_start():
    body = request.get_json(force=True) or {}
    employee_id = body.get("employee_id")
    station_id = body.get("station_id")
    employee_name = body.get("employee_name", "")
    station_name = body.get("station_name", "")

    existing = get_active_shift(employee_id)
    if existing:
        return jsonify({"ok": True, "shift": existing})

    res = supabase.table("shifts").insert({
        "employee_id": employee_id,
        "station_id": station_id,
        "employee_name": employee_name,
        "station_name": station_name,
        "start_time": now_iso(),
        "status": "active",
    }).execute()

    return jsonify({"ok": True, "shift": res.data[0]})


@app.route("/api/shift/end", methods=["POST"])
def api_shift_end():
    body = request.get_json(force=True) or {}
    shift_id = body.get("shift_id")
    end_notes = body.get("end_notes", "")

    if not shift_id:
        return jsonify({"ok": False, "error": "חסר מזהה משמרת"}), 400

    supabase.table("shifts").update({
        "end_time": now_iso(),
        "end_notes": end_notes,
        "status": "ended",
    }).eq("id", shift_id).execute()

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API - עובד: דוחות סריקה
# ---------------------------------------------------------------------------
@app.route("/api/scan-report", methods=["POST"])
def api_scan_report():
    body = request.get_json(force=True) or {}
    shift_id = body.get("shift_id")
    employee_id = body.get("employee_id")
    station_id = body.get("station_id")
    from_time = body.get("from_time")
    to_time = body.get("to_time")
    status = body.get("status")  # green / yellow / red
    notes = body.get("notes", "")
    photo_base64 = body.get("photo_base64")

    if status not in ("green", "yellow", "red"):
        return jsonify({"ok": False, "error": "סטטוס לא תקין"}), 400
    if status == "red" and not notes.strip():
        return jsonify({"ok": False, "error": "חובה לפרט מה קרה בדיווח על אירוע"}), 400
    if not (shift_id and employee_id and station_id and from_time and to_time):
        return jsonify({"ok": False, "error": "חסרים נתונים בדוח"}), 400

    res = supabase.table("scan_reports").insert({
        "shift_id": shift_id,
        "employee_id": employee_id,
        "station_id": station_id,
        "from_time": from_time,
        "to_time": to_time,
        "status": status,
        "notes": notes,
        "photo_base64": photo_base64,
        "created_at": now_iso(),
    }).execute()

    return jsonify({"ok": True, "report": res.data[0]})


@app.route("/api/scan-reports/<int:shift_id>", methods=["GET"])
def api_scan_reports_for_shift(shift_id):
    res = (
        supabase.table("scan_reports")
        .select("*")
        .eq("shift_id", shift_id)
        .order("created_at")
        .execute()
    )
    return jsonify({"ok": True, "reports": res.data})


# ---------------------------------------------------------------------------
# API - עובד: רשימת תחנות לבחירה עצמית (כאשר האדמין לא שיבץ תחנה קבועה)
# ---------------------------------------------------------------------------
@app.route("/api/stations", methods=["GET"])
def api_stations():
    res = supabase.table("stations").select("id,name").order("name").execute()
    return jsonify({"ok": True, "stations": res.data})


@app.route("/api/stations/<int:station_id>", methods=["GET"])
def api_station_detail(station_id):
    res = supabase.table("stations").select("*").eq("id", station_id).execute()
    if not res.data:
        return jsonify({"ok": False, "error": "תחנה לא נמצאה"}), 404
    return jsonify({"ok": True, "station": res.data[0]})


# ---------------------------------------------------------------------------
# API - עובד: אנשי קשר לחיוג מהיר
# ---------------------------------------------------------------------------
@app.route("/api/contacts", methods=["GET"])
def api_contacts():
    res = supabase.table("contacts").select("*").order("sort_order").execute()
    return jsonify({"ok": True, "contacts": res.data})


# ---------------------------------------------------------------------------
# אדמין - התחברות
# ---------------------------------------------------------------------------
def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "יש להתחבר כאדמין"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "סיסמה שגויה"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template("admin.html")


# ---------------------------------------------------------------------------
# אדמין API - עובדים
# ---------------------------------------------------------------------------
@app.route("/api/admin/employees", methods=["GET"])
@admin_required
def admin_list_employees():
    emps = (
        supabase.table("employees")
        .select("*, stations(id,name)")
        .order("name")
        .execute()
        .data
    )
    shifts = (
        supabase.table("shifts").select("*").eq("status", "active").execute().data
    )
    active_by_emp = {s["employee_id"]: s for s in shifts}

    out = []
    for e in emps:
        shift = active_by_emp.get(e["emp_id"])
        out.append({
            "id": e["emp_id"],
            "employee_number": e["emp_id"],
            "name": e["name"],
            "role": e["role"],
            "phone": e.get("phone"),
            "suspended": bool(e.get("suspended")),
            "sms_enabled": e.get("sms_enabled", True),
            "station": e.get("stations"),
            "shift_status": "במשמרת" if shift else "לא התחיל משמרת",
            "shift_start": shift["start_time"] if shift else None,
            "active_shift_id": shift["id"] if shift else None,
        })
    return jsonify({"ok": True, "employees": out})


@app.route("/api/admin/employees", methods=["POST"])
@admin_required
def admin_create_employee():
    body = request.get_json(force=True) or {}
    emp_id = str(body.get("employee_number", "")).strip()
    if not emp_id:
        return jsonify({"ok": False, "error": "חסר מספר עובד"}), 400
    res = supabase.table("employees").insert({
        "emp_id": emp_id,
        "name": body.get("name", ""),
        "role": body.get("role", "מאבטח"),
        "phone": body.get("phone", ""),
        "suspended": False,
        "sms_enabled": True,
    }).execute()
    return jsonify({"ok": True, "employee": res.data[0]})


@app.route("/api/admin/employees/<emp_id>", methods=["PATCH"])
@admin_required
def admin_update_employee(emp_id):
    body = request.get_json(force=True) or {}
    # emp_id (המפתח הראשי) לא ניתן לעריכה כאן במכוון - הטבלה משותפת עם notification-system
    allowed = ["name", "role", "phone", "suspended", "sms_enabled", "assigned_station_id"]
    update = {k: v for k, v in body.items() if k in allowed}
    if update:
        supabase.table("employees").update(update).eq("emp_id", emp_id).execute()
    return jsonify({"ok": True})


# הערה: במכוון אין כאן endpoint למחיקה קשיחה של עובד - טבלת employees משותפת עם
# מערכת ה-notification-system שלך, ומחיקה כאן הייתה עלולה לשבור אותה. במקום זאת,
# "מחיקה" בפאנל הזה = השעיה (suspended=true) דרך ה-PATCH למעלה, שרק מסמנת את העובד
# כלא-פעיל בלי למחוק את השורה.


# ---------------------------------------------------------------------------
# אדמין API - תחנות
# ---------------------------------------------------------------------------
@app.route("/api/admin/stations", methods=["GET"])
@admin_required
def admin_list_stations():
    res = supabase.table("stations").select("*").order("name").execute()
    return jsonify({"ok": True, "stations": res.data})


@app.route("/api/admin/stations", methods=["POST"])
@admin_required
def admin_create_station():
    body = request.get_json(force=True) or {}
    res = supabase.table("stations").insert({
        "name": body.get("name", ""),
        "briefing_html": body.get("briefing_html", "<p></p>"),
    }).execute()
    return jsonify({"ok": True, "station": res.data[0]})


@app.route("/api/admin/stations/<int:station_id>", methods=["PATCH"])
@admin_required
def admin_update_station(station_id):
    body = request.get_json(force=True) or {}
    allowed = ["name", "briefing_html"]
    update = {k: v for k, v in body.items() if k in allowed}
    if update:
        update["updated_at"] = now_iso()
        supabase.table("stations").update(update).eq("id", station_id).execute()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# אדמין API - דוחות (תצוגה)
# ---------------------------------------------------------------------------
@app.route("/api/admin/reports", methods=["GET"])
@admin_required
def admin_reports():
    station_id = request.args.get("station_id")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    q = supabase.table("shifts").select("*").order("start_time", desc=True)
    if station_id:
        q = q.eq("station_id", station_id)
    if date_from:
        q = q.gte("start_time", date_from)
    if date_to:
        q = q.lte("start_time", date_to)
    shifts = q.limit(300).execute().data

    shift_ids = [s["id"] for s in shifts]
    reports_by_shift = {}
    if shift_ids:
        reports = (
            supabase.table("scan_reports")
            .select("*")
            .in_("shift_id", shift_ids)
            .order("created_at")
            .execute()
            .data
        )
        for r in reports:
            reports_by_shift.setdefault(r["shift_id"], []).append(r)

    acks = supabase.table("briefing_acks").select("*").order(
        "acknowledged_at", desc=True
    ).limit(300).execute().data

    for s in shifts:
        s["scan_reports"] = reports_by_shift.get(s["id"], [])

    return jsonify({"ok": True, "shifts": shifts, "briefing_acks": acks})


# ---------------------------------------------------------------------------
# אדמין API - אנשי קשר
# ---------------------------------------------------------------------------
@app.route("/api/admin/contacts", methods=["GET"])
@admin_required
def admin_list_contacts():
    res = supabase.table("contacts").select("*").order("sort_order").execute()
    return jsonify({"ok": True, "contacts": res.data})


@app.route("/api/admin/contacts", methods=["POST"])
@admin_required
def admin_create_contact():
    body = request.get_json(force=True) or {}
    res = supabase.table("contacts").insert({
        "name": body.get("name", ""),
        "phone": body.get("phone", ""),
        "sort_order": body.get("sort_order", 10),
        "is_emergency": body.get("is_emergency", False),
    }).execute()
    return jsonify({"ok": True, "contact": res.data[0]})


@app.route("/api/admin/contacts/<int:contact_id>", methods=["PATCH"])
@admin_required
def admin_update_contact(contact_id):
    body = request.get_json(force=True) or {}
    allowed = ["name", "phone", "sort_order", "is_emergency"]
    update = {k: v for k, v in body.items() if k in allowed}
    if update:
        supabase.table("contacts").update(update).eq("id", contact_id).execute()
    return jsonify({"ok": True})


@app.route("/api/admin/contacts/<int:contact_id>", methods=["DELETE"])
@admin_required
def admin_delete_contact(contact_id):
    supabase.table("contacts").delete().eq("id", contact_id).execute()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# אדמין API - קמפיינים של SMS אוטומטי
# ---------------------------------------------------------------------------
@app.route("/api/admin/campaigns", methods=["GET"])
@admin_required
def admin_list_campaigns():
    res = (
        supabase.table("sms_campaigns")
        .select("*, stations(name)")
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify({"ok": True, "campaigns": res.data})


@app.route("/api/admin/campaigns", methods=["POST"])
@admin_required
def admin_create_campaign():
    body = request.get_json(force=True) or {}
    res = supabase.table("sms_campaigns").insert({
        "station_id": body.get("station_id"),
        "message": body.get("message", ""),
        "interval_hours": body.get("interval_hours", 2),
        "active": True,
        "created_at": now_iso(),
    }).execute()
    return jsonify({"ok": True, "campaign": res.data[0]})


@app.route("/api/admin/campaigns/<int:campaign_id>/stop", methods=["POST"])
@admin_required
def admin_stop_campaign(campaign_id):
    supabase.table("sms_campaigns").update({"active": False}).eq(
        "id", campaign_id
    ).execute()
    return jsonify({"ok": True})


@app.route("/api/admin/sms-log", methods=["GET"])
@admin_required
def admin_sms_log():
    res = (
        supabase.table("sms_log")
        .select("*")
        .order("sent_at", desc=True)
        .limit(200)
        .execute()
    )
    return jsonify({"ok": True, "log": res.data})


# ---------------------------------------------------------------------------
# תזמון SMS אוטומטי - רץ ברקע כל 10 דקות
# שולח הודעה חוזרת לכל עובד שנמצא במשמרת פעילה בתחנה של הקמפיין,
# רק אם sms_enabled=True עבורו, ורק אם עברו interval_hours מאז ההודעה הקודמת אליו.
# ---------------------------------------------------------------------------
def process_sms_campaigns():
    with app.app_context():
        try:
            campaigns = (
                supabase.table("sms_campaigns")
                .select("*")
                .eq("active", True)
                .execute()
                .data
            )
            if not campaigns:
                return

            active_shifts = (
                supabase.table("shifts").select("*").eq("status", "active").execute().data
            )

            for camp in campaigns:
                station_id = camp.get("station_id")
                relevant_shifts = [
                    s for s in active_shifts
                    if station_id is None or s["station_id"] == station_id
                ]
                for shift in relevant_shifts:
                    emp_id = shift["employee_id"]
                    emp = (
                        supabase.table("employees")
                        .select("*")
                        .eq("emp_id", emp_id)
                        .execute()
                        .data
                    )
                    if not emp:
                        continue
                    emp = emp[0]
                    if not emp.get("sms_enabled", True) or not emp.get("phone"):
                        continue

                    last_sms = (
                        supabase.table("sms_log")
                        .select("*")
                        .eq("employee_id", emp_id)
                        .order("sent_at", desc=True)
                        .limit(1)
                        .execute()
                        .data
                    )
                    should_send = True
                    if last_sms:
                        last_time = datetime.fromisoformat(
                            last_sms[0]["sent_at"].replace("Z", "+00:00")
                        )
                        interval = timedelta(hours=camp.get("interval_hours", 2))
                        if datetime.now(timezone.utc) - last_time < interval:
                            should_send = False

                    if should_send:
                        ok, err = send_sms(emp["phone"], camp["message"])
                        supabase.table("sms_log").insert({
                            "employee_id": emp_id,
                            "phone": emp["phone"],
                            "message": camp["message"],
                            "sent_at": now_iso(),
                            "success": ok,
                            "error": err,
                        }).execute()
        except Exception as e:
            print("שגיאה בעיבוד קמפיין SMS:", e)


scheduler = BackgroundScheduler()
scheduler.add_job(process_sms_campaigns, "interval", minutes=10, id="sms_campaigns")
scheduler.start()


# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({"ok": True, "service": "station-guard-system backend"})


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
