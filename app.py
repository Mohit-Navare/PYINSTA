from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps
import io
import logging
import mimetypes
import os
from pathlib import Path
import re
import uuid
import zipfile

from bson.objectid import ObjectId
from dotenv import load_dotenv
from email_validator import EmailNotValidError, validate_email
from flask import (Flask, abort, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from gridfs import GridFS
from gridfs.errors import NoFile
import requests
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import db as mongo_db
from config import posts_collection as posts
from config import users_collection as users

# ── Setup ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "cloud.env", override=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "pyinsta_secret_key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

FEED_PAGE_SIZE       = 20
FEED_MAINT_INTERVAL  = int(os.getenv("FEED_MAINTENANCE_INTERVAL_SECONDS", "3600"))
MEDIA_CACHE_AGE      = 86400
NOTIF_POLL_MS        = int(os.getenv("NOTIFICATION_POLL_INTERVAL_MS", "15000"))
NOTIF_FETCH_LIMIT    = int(os.getenv("NOTIFICATION_POPUP_FETCH_LIMIT", "8"))
_last_maintenance: datetime | None = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
fs  = GridFS(mongo_db) if mongo_db is not None else None

DEFAULT_AVATARS = {
    "Female": "uploads/profile/female_default.svg",
    "Male":   "uploads/profile/male_default.svg",
    "Other":  "uploads/profile/other_default.svg",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".mov", ".mkv", ".avi", ".flv"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS


# ── Models ─────────────────────────────────────────────────────────────────────
class User:
    VALID_GENDERS = ["Male", "Female", "Other"]

    @staticmethod
    def validate_username(u):
        if not u or not (3 <= len(u) <= 20):
            return False, "Username must be 3–20 characters"
        if not re.match(r"^[a-zA-Z0-9_]+$", u):
            return False, "Username can only contain letters, numbers, and underscore"
        return True, ""

    @staticmethod
    def validate_email(e):
        try:
            validate_email(e, check_deliverability=False)
            return True, ""
        except EmailNotValidError as ex:
            return False, f"Invalid email: {ex}"

    @staticmethod
    def validate_password(p):
        if not p or len(p) < 6:
            return False, "Password must be at least 6 characters"
        if not re.search(r"[a-zA-Z]", p):
            return False, "Password must contain at least one letter"
        if not re.search(r"\d", p):
            return False, "Password must contain at least one number"
        return True, ""

    @staticmethod
    def validate_phone(p):
        if not p:
            return True, ""
        clean = re.sub(r"[\s\-\+\(\)]", "", p)
        if not clean.isdigit() or len(clean) < 10:
            return False, "Phone must be at least 10 digits"
        return True, ""

    @staticmethod
    def validate_gender(g):
        return g in User.VALID_GENDERS

    @staticmethod
    def new(name, username, email, phone, gender, pw_hash, avatar):
        now = datetime.now()
        return dict(name=name, username=username, email=email, phone=phone,
                    gender=gender, password=pw_hash, profile_image=avatar,
                    bio="", followers=[], following=[], saved_posts=[],
                    notifications=[], created_at=now, updated_at=now)


class Snap:
    @staticmethod
    def validate_caption(c):
        if c and len(str(c).strip()) > 2000:
            return False, "Caption must be less than 2000 characters"
        return True, ""

    @staticmethod
    def new(username, images, caption="", music=None):
        now = datetime.now()
        return dict(username=username, images=images,
                    caption=caption.strip() if caption else "",
                    music=music or {}, likes=[], comments=[],
                    created_at=now, updated_at=now)

    @staticmethod
    def new_comment(username, text):
        return dict(username=username, text=text.strip(), created_at=datetime.now())


# ── Guards & session helpers ───────────────────────────────────────────────────
def ensure_db():
    if users is None or posts is None:
        abort(503)

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if "username" not in session:
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper

def is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"

def current_user() -> dict | None:
    u = session.get("username")
    return _with_defaults(users.find_one({"username": u})) if u and users is not None else None

def require_user() -> dict | None:
    u = current_user()
    if u:
        return u
    session.clear()
    flash("Your account was not found. Please log in again.", "warning")
    return None

@app.context_processor
def _inject_globals():
    unread = 0
    u = session.get("username")
    if u and users is not None:
        doc = users.find_one({"username": u}, {"notifications": 1})
        unread = sum(1 for n in (doc or {}).get("notifications", []) if not n.get("is_read"))
    return dict(unread_notifications=unread, notification_poll_interval_ms=NOTIF_POLL_MS)


# ── Media helpers ──────────────────────────────────────────────────────────────
def _ext(filename):
    return os.path.splitext(filename or "")[1].lower()

def _is_gridfs(v):
    return isinstance(v, dict) and v.get("storage") == "gridfs" and v.get("file_id") is not None

def _is_db(v):
    return isinstance(v, dict) and v.get("storage") == "db" and "data" in v

def _media_filename(v):
    if isinstance(v, dict): return (v.get("filename") or "").strip()
    if isinstance(v, str):  return os.path.basename(v.strip())
    return ""

def _content_type(v):
    if isinstance(v, dict) and (v.get("content_type") or "").strip():
        return v["content_type"].strip()
    ct, _ = mimetypes.guess_type(_media_filename(v))
    return ct or "application/octet-stream"

def _is_video(v):
    return _content_type(v).startswith("video/") or _ext(_media_filename(v)) in VIDEO_EXTS

def _norm_path(p):
    if not p: return ""
    return p if p.startswith("uploads/") else f"uploads/{p.lstrip('/')}"

def _default_avatar(gender):
    return DEFAULT_AVATARS.get(gender, DEFAULT_AVATARS["Male"])

def _is_default_avatar(v):
    return not isinstance(v, dict) and _norm_path(v) in DEFAULT_AVATARS.values()

def _avatar_url(user_doc):
    if not user_doc:
        return url_for("static", filename=_norm_path(_default_avatar("Male")))
    u = user_doc.get("username", "")
    if u:
        return url_for("serve_profile_media", username=u)
    pf = user_doc.get("profile_image", "")
    if _is_db(pf):
        return url_for("static", filename=_norm_path(_default_avatar(user_doc.get("gender", "Male"))))
    return url_for("static", filename=_norm_path(pf or _default_avatar(user_doc.get("gender", "Male"))))

def _post_media_items(post_doc):
    pid = str(post_doc.get("_id", ""))
    return [
        dict(url=url_for("serve_post_media", post_id=pid, media_index=i),
             filename=_media_filename(v), content_type=_content_type(v), is_video=_is_video(v))
        for i, v in enumerate(post_doc.get("images", [])) if v
    ]

def _cache(response, max_age=MEDIA_CACHE_AGE):
    response.cache_control.private = True
    response.cache_control.max_age = max_age
    response.cache_control.no_store = False
    response.expires = datetime.utcnow() + timedelta(seconds=max_age)
    return response

def _send_media(v, name=None, attach=False):
    if _is_gridfs(v):
        if fs is None: abort(503)
        try:
            g = fs.get(v["file_id"])
        except NoFile:
            abort(404)
        return _cache(send_file(io.BytesIO(g.read()),
                                mimetype=v.get("content_type") or getattr(g, "content_type", None) or _content_type(v),
                                as_attachment=attach,
                                download_name=name or v.get("filename") or getattr(g, "filename", None) or "media"))
    if _is_db(v):
        return _cache(send_file(io.BytesIO(bytes(v.get("data", b""))),
                                mimetype=_content_type(v), as_attachment=attach,
                                download_name=name or _media_filename(v) or "media"))
    return None  # caller handles legacy path

def _send_gridfs_by_id(file_id: ObjectId, attach=False):
    if fs is None: abort(503)
    try:
        g = fs.get(file_id)
    except NoFile:
        abort(404)
    fname = secure_filename((getattr(g, "filename", None) or "").strip()) or f"{file_id}.bin"
    mt    = getattr(g, "content_type", None) or mimetypes.guess_type(fname)[0] or "application/octet-stream"
    return _cache(send_file(io.BytesIO(g.read()), mimetype=mt, as_attachment=attach, download_name=fname))

def _store_file(file_obj, image_only=False):
    fname = (file_obj.filename or "").strip()
    if not fname: return None, "No file selected"
    if image_only and _ext(fname) not in IMAGE_EXTS: return None, "Please upload an image file"
    if _ext(fname) not in MEDIA_EXTS: return None, f"Invalid file type: {fname}"
    file_obj.seek(0, os.SEEK_END)
    if file_obj.tell() > 50 * 1024 * 1024: return None, f"File too large: {fname}"
    file_obj.seek(0)
    if fs is None: return None, "File storage is unavailable"
    safe = secure_filename(fname) or f"upload{_ext(fname)}"
    ct   = (getattr(file_obj, "mimetype", None) or "").strip() or _content_type(safe)
    fid  = fs.put(file_obj.stream, filename=safe, content_type=ct)
    file_obj.seek(0)
    return dict(storage="gridfs", file_id=fid, filename=safe, content_type=ct,
                kind="video" if _ext(safe) in VIDEO_EXTS else "image"), None

def _safe_delete(v):
    if _is_gridfs(v):
        if fs:
            try: fs.delete(v["file_id"])
            except Exception as e: log.warning("GridFS delete failed: %s", e)
        return
    if _is_db(v): return
    p = os.path.normpath(os.path.join("static", _norm_path(v)))
    if p.startswith(os.path.normpath("static")) and os.path.exists(p):
        os.remove(p)

def _delete_post(post_doc):
    if not post_doc or not post_doc.get("_id"): return False
    for v in post_doc.get("images", []): _safe_delete(v)
    result = posts.delete_one({"_id": post_doc["_id"]})
    pid = str(post_doc["_id"])
    users.update_many({}, {"$pull": {"saved_posts": pid, "notifications": {"post_id": pid}}})
    return result.deleted_count > 0


# ── User helpers ───────────────────────────────────────────────────────────────
def _with_defaults(doc):
    if not doc: return None
    updates = {k: v for k, v in [
        ("bio", ""), ("followers", []), ("following", []),
        ("saved_posts", []), ("notifications", []),
    ] if k not in doc}
    if not doc.get("profile_image"):
        updates["profile_image"] = _default_avatar(doc.get("gender", "Male"))
    if updates:
        updates["updated_at"] = datetime.now()
        users.update_one({"_id": doc["_id"]}, {"$set": updates})
        doc.update(updates)
    doc["profile_image_url"] = _avatar_url(doc)
    return doc

def _enrich_posts(items, viewer):
    if not items: return []
    unames   = {p.get("username") for p in items if p.get("username")}
    owner_map = {d["username"]: _with_defaults(d)
                 for d in users.find({"username": {"$in": list(unames)}},
                                     {"username":1,"profile_image":1,"gender":1,"name":1})}
    saved = set((viewer or {}).get("saved_posts", []))
    for p in items:
        od = owner_map.get(p.get("username"))
        p["owner_profile_image_url"] = _avatar_url(od)
        p["media_items"]   = _post_media_items(p)
        p["primary_media"] = p["media_items"][0] if p["media_items"] else None
        p["image_urls"]    = [m["url"] for m in p["media_items"] if not m["is_video"]]
        p["is_saved"]      = str(p.get("_id")) in saved
    return items

def _build_user_cards(usernames):
    ordered = list(dict.fromkeys(u for u in (usernames or []) if isinstance(u, str) and u.strip()))
    if not ordered: return []
    docs = {d["username"]: _with_defaults(d)
            for d in users.find({"username": {"$in": ordered}},
                                {"username":1,"name":1,"profile_image":1,"gender":1})
            if d.get("username")}
    return [dict(username=d["username"], name=d.get("name",""),
                 profile_image_url=_avatar_url(d))
            for u in ordered if (d := docs.get(u))]

def _profile_insights(user_doc, user_posts):
    user_doc, user_posts = user_doc or {}, user_posts or []
    likes    = sum(len(p.get("likes",[])) for p in user_posts)
    comments = sum(len(p.get("comments",[])) for p in user_posts)
    checks = [
        ("Display name",    bool((user_doc.get("name") or "").strip())),
        ("Email",           bool((user_doc.get("email") or "").strip())),
        ("Phone",           bool((user_doc.get("phone") or "").strip())),
        ("Bio",             bool((user_doc.get("bio") or "").strip())),
        ("Custom photo",    bool(user_doc.get("profile_image")) and not _is_default_avatar(user_doc.get("profile_image"))),
    ]
    done = sum(1 for _, ok in checks if ok)
    return dict(
        total_posts=len(user_posts), total_likes=likes, total_comments=comments,
        media_items=sum(len(p.get("images",[])) for p in user_posts),
        saved_posts=len(user_doc.get("saved_posts",[]) or []),
        average_engagement=round((likes+comments)/len(user_posts),1) if user_posts else 0,
        completion_percent=int(done/len(checks)*100) if checks else 0,
        completion_items=checks,
        missing_items=[l for l, ok in checks if not ok],
        joined_at=user_doc.get("created_at"),
        last_posted_at=user_posts[0].get("created_at") if user_posts else None,
    )

def _apply_profile_updates(user_doc, *, include_account=False):
    data   = {"updated_at": datetime.now()}
    errors = []

    bio = request.form.get("bio", "").strip()
    if len(bio) > 500: errors.append("Bio must be 500 characters or less.")
    else: data["bio"] = bio

    req_gender  = request.form.get("gender", "").strip()
    cur_gender  = user_doc.get("gender", "Male")
    new_gender  = req_gender if req_gender and User.validate_gender(req_gender) else cur_gender
    if req_gender and not User.validate_gender(req_gender): errors.append("Invalid gender.")
    if new_gender != cur_gender: data["gender"] = new_gender

    if include_account:
        name, email, phone = (request.form.get(k, "").strip() for k in ("name","email","phone"))
        if not name: errors.append("Name is required.")
        ok, msg = User.validate_email(email)
        if not ok: errors.append(msg)
        ok, msg = User.validate_phone(phone)
        if not ok: errors.append(msg)
        if users.find_one({"email": email, "username": {"$ne": user_doc["username"]}}):
            errors.append("Email is already in use.")
        if not errors:
            data.update(name=name, email=email, phone=phone)

    if errors: return errors

    cur_img = user_doc.get("profile_image", "")
    if request.form.get("reset_profile_image") == "1":
        data["profile_image"] = _default_avatar(new_gender)
        if cur_img and not _is_default_avatar(cur_img): _safe_delete(cur_img)

    img = request.files.get("profile_image")
    if img and img.filename:
        saved, err = _store_file(img, image_only=True)
        if err: errors.append(err)
        elif saved:
            data["profile_image"] = saved
            if cur_img and not _is_default_avatar(cur_img): _safe_delete(cur_img)
    elif "profile_image" not in data and (not cur_img or _is_default_avatar(cur_img)) and new_gender != cur_gender:
        data["profile_image"] = _default_avatar(new_gender)

    if errors: return errors
    if not any(user_doc.get(k) != v for k, v in data.items() if k != "updated_at"):
        return ["No changes were submitted."]
    users.update_one({"_id": user_doc["_id"]}, {"$set": data})
    return []


# ── Notifications ──────────────────────────────────────────────────────────────
_NOTIF_TEXT = {
    "follow":  "{} started following you.",
    "like":    "{} liked your post.",
    "comment": "{} commented on your post.",
}

def _add_notification(target, actor, ntype, post_id=None):
    if not target or target == actor: return
    n = dict(id=uuid.uuid4().hex, type=ntype, actor=actor,
             text=_NOTIF_TEXT.get(ntype, "You have a new notification.").format(actor),
             post_id=post_id, is_read=False, created_at=datetime.now())
    users.update_one({"username": target},
                     {"$push": {"notifications": {"$each": [n], "$position": 0, "$slice": 100}}})

def _enrich_notifications(items):
    items = [i for i in (items or []) if isinstance(i, dict)]
    if not items: return []
    actors = {i["actor"] for i in items if isinstance(i.get("actor"), str)}
    amap   = {d["username"]: d
              for d in users.find({"username": {"$in": list(actors)}},
                                  {"username":1,"profile_image":1,"gender":1})} if actors else {}
    result = []
    for item in items:
        ca  = item.get("created_at")
        doc = amap.get(item.get("actor"))
        raw_id = str(item.get("id") or "").strip()
        ntype  = str(item.get("type") or "")
        actor  = str(item.get("actor") or "")
        link   = url_for("view_profile", username=actor) if ntype == "follow" and actor else url_for("notifications")
        result.append({**item,
                       "id": raw_id or f"{actor}:{ntype}:{item.get('post_id','')}:{ca.isoformat() if isinstance(ca,datetime) else ''}",
                       "actor_profile_image_url": _avatar_url(doc),
                       "created_at_label": ca.strftime("%d %b %Y, %H:%M") if isinstance(ca, datetime) else "",
                       "created_at_iso":   ca.isoformat() if isinstance(ca, datetime) else "",
                       "link_url": link})
    return result


# ── Feed maintenance ───────────────────────────────────────────────────────────
def _sanitize_usernames(raw, valid, exclude=None):
    seen, out = set(), []
    for v in (raw or []):
        v = (v or "").strip() if isinstance(v, str) else ""
        if v and v in valid and v != exclude and v not in seen:
            seen.add(v); out.append(v)
    return out

def _run_maintenance():
    global _last_maintenance
    now = datetime.now()
    if _last_maintenance and (now - _last_maintenance).total_seconds() < FEED_MAINT_INTERVAL:
        return
    # remove orphan posts
    all_posts = list(posts.find({}, {"_id":1,"username":1,"images":1}))
    if all_posts:
        valid_users = {d["username"] for d in users.find({"username":{"$in": list({p.get("username","") for p in all_posts})}}, {"username":1})}
        removed = sum(1 for p in all_posts if p.get("username") not in valid_users and _delete_post(p))
        if removed: log.info("Removed %s orphan posts", removed)
    # clean stale references
    valid_u = {d["username"] for d in users.find({},{"username":1}) if isinstance(d.get("username"),str)}
    valid_p = {str(d["_id"]) for d in posts.find({},{"_id":1})}
    uu = pp = 0
    for d in users.find({},{"_id":1,"username":1,"followers":1,"following":1,"saved_posts":1,"notifications":1}):
        u    = d.get("username","")
        upd  = {}
        fl   = _sanitize_usernames(d.get("followers",[]), valid_u, u)
        fw   = _sanitize_usernames(d.get("following",[]), valid_u, u)
        notifs = [n for n in d.get("notifications",[]) if isinstance(n,dict) and (not n.get("actor") or n["actor"] in valid_u)]
        saved  = list(dict.fromkeys(s for s in (d.get("saved_posts",[]) or []) if str(s).strip() in valid_p))
        if fl != d.get("followers"):   upd["followers"]     = fl
        if fw != d.get("following"):   upd["following"]     = fw
        if notifs != d.get("notifications"): upd["notifications"] = notifs
        if saved  != d.get("saved_posts"):   upd["saved_posts"]   = saved
        if upd: upd["updated_at"] = now; users.update_one({"_id":d["_id"]},{"$set":upd}); uu += 1
    for d in posts.find({},{"_id":1,"likes":1,"comments":1}):
        upd = {}
        lk  = _sanitize_usernames(d.get("likes",[]), valid_u)
        cm  = [c for c in d.get("comments",[]) if isinstance(c,dict) and c.get("username") in valid_u]
        if lk != d.get("likes"):     upd["likes"]    = lk
        if cm != d.get("comments"):  upd["comments"] = cm
        if upd: upd["updated_at"] = now; posts.update_one({"_id":d["_id"]},{"$set":upd}); pp += 1
    if uu or pp: log.info("Maintenance repaired users=%s posts=%s", uu, pp)
    _last_maintenance = now


# ── DB indexes ─────────────────────────────────────────────────────────────────
try:
    if users is not None:
        users.create_index("username", unique=True)
        users.create_index("email", unique=True)
    if posts is not None:
        posts.create_index("username")
        posts.create_index("created_at")
        posts.create_index([("username",1),("created_at",-1)])
except Exception as e:
    log.warning("Index creation warning: %s", e)


# ── Auth routes ────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
@app.route("/login", methods=["GET","POST"])
def login():
    ensure_db()
    if request.method == "POST":
        uname = request.form.get("username","").strip()
        pw    = request.form.get("password","")
        if not uname or not pw:
            flash("Username and password are required.", "error")
        else:
            try:
                doc = users.find_one({"username": uname})
                if doc and check_password_hash(doc["password"], pw):
                    _with_defaults(doc)
                    session["username"] = uname
                    return redirect(url_for("home"))
                flash("Invalid username or password.", "error")
            except Exception as e:
                log.error("Login error: %s", e)
                flash("An error occurred while logging in.", "error")
    return render_template("auth/login.html")


@app.route("/register", methods=["GET","POST"])
def register():
    ensure_db()
    if request.method == "POST":
        f       = request.form
        name    = f.get("name","").strip()
        uname   = f.get("username","").strip()
        email   = f.get("email","").strip()
        phone   = f.get("phone","").strip()
        gender  = f.get("gender","Male").strip()
        pw      = f.get("password","")
        pw2     = f.get("confirm_password","")
        errors  = []

        if not name: errors.append("Name is required")
        for ok, msg in [User.validate_username(uname), User.validate_email(email),
                        User.validate_phone(phone), User.validate_password(pw)]:
            if not ok: errors.append(msg)
        if pw != pw2: errors.append("Passwords do not match")
        if not User.validate_gender(gender): errors.append("Invalid gender selection")
        if not errors and users.find_one({"username": uname}): errors.append("Username already exists")
        if not errors and users.find_one({"email": email}):    errors.append("Email already registered")

        if not errors:
            avatar = _default_avatar(gender)
            img = request.files.get("profile_image")
            if img and img.filename:
                saved, err = _store_file(img, image_only=True)
                if err: flash(err, "warning")
                elif saved: avatar = saved
            try:
                users.insert_one(User.new(name, uname, email, phone, gender, generate_password_hash(pw), avatar))
                session["username"] = uname
                flash("Account created successfully.", "success")
                return redirect(url_for("home"))
            except Exception as e:
                log.error("Register error: %s", e)
                errors.append("An error occurred during registration.")

        for err in errors: flash(err, "error")
    return render_template("auth/register.html")


@app.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    ensure_db()
    if "username" in session: return redirect(url_for("settings"))
    if request.method == "POST":
        uname = request.form.get("username","").strip()
        email = request.form.get("email","").strip()
        npw   = request.form.get("new_password","")
        cpw   = request.form.get("confirm_password","")
        errors = []
        if not uname: errors.append("Username is required.")
        ok, msg = User.validate_email(email)
        if not ok: errors.append(msg)
        ok, msg = User.validate_password(npw)
        if not ok: errors.append(msg)
        if npw != cpw: errors.append("Passwords do not match.")
        if errors:
            for e in errors: flash(e, "error")
        else:
            doc = users.find_one({"username": uname})
            if not doc or doc.get("email","").strip().lower() != email.lower():
                flash("Username and email do not match any account.", "error")
            else:
                users.update_one({"_id": doc["_id"]},
                                  {"$set": {"password": generate_password_hash(npw), "updated_at": datetime.now()}})
                flash("Password reset successful. Please log in.", "success")
                return redirect(url_for("login"))
    return render_template("auth/forgot_password.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Feed & post routes ─────────────────────────────────────────────────────────
@app.route("/home")
@login_required
def home():
    ensure_db()
    try:
        user_doc = require_user()
        if not user_doc: return redirect(url_for("login"))
        _run_maintenance()
        feed = _enrich_posts(
            list(posts.find({},{"username":1,"images":1,"likes":1,"comments":1,"caption":1,"music":1,"created_at":1})
                 .sort("created_at",-1).limit(FEED_PAGE_SIZE)), user_doc)
        following = set(user_doc.get("following",[]))
        suggestions = list(users.find(
            {"username": {"$nin": list(following | {session["username"]})}},
            {"username":1,"name":1,"profile_image":1,"gender":1}).limit(5))
        for s in suggestions: s["profile_image_url"] = _avatar_url(s)
        stats = [
            {"label":"Posts",        "value": posts.count_documents({"username": session["username"]})},
            {"label":"Following",    "value": len(user_doc.get("following",[]))},
            {"label":"Saved",        "value": len(user_doc.get("saved_posts",[]))},
            {"label":"Unread alerts","value": sum(1 for n in user_doc.get("notifications",[]) if not n.get("is_read"))},
        ]
        return render_template("feed/home.html", user=user_doc, feed=feed, suggestions=suggestions, dashboard_stats=stats)
    except Exception as e:
        log.error("Home error: %s", e)
        flash("Unable to load feed.", "error")
        return render_template("feed/home.html", user=None, feed=[], suggestions=[], dashboard_stats=[])


@app.route("/create-snap", methods=["GET","POST"])
@login_required
def create_snap():
    ensure_db()
    if request.method == "POST": return upload()
    return render_template("create_snap.html")


@app.route("/search-music")
@login_required
def search_music():
    ensure_db()
    q = request.args.get("q","").strip()
    if not q: return jsonify({"data":[]})
    try:
        r = requests.get("https://api.deezer.com/search", params={"q":q}, timeout=10)
        r.raise_for_status()
        tracks = [dict(id=t.get("id"), title=t.get("title"),
                       artist=(t.get("artist") or {}).get("name"),
                       cover=(t.get("album") or {}).get("cover_medium"),
                       preview=t.get("preview"))
                  for t in r.json().get("data",[])[:10]]
        return jsonify({"data": tracks})
    except requests.RequestException as e:
        log.error("Deezer error: %s", e)
        return jsonify({"data":[], "error":"Unable to fetch music."}), 502


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    ensure_db()
    if "images" not in request.files:
        flash("Select at least one image or video.", "error")
        return redirect(url_for("create_snap"))
    caption = request.form.get("caption","").strip()
    ok, msg = Snap.validate_caption(caption)
    if not ok: flash(msg, "error"); return redirect(url_for("create_snap"))

    music = {k: request.form.get(v,"").strip() for k,v in
             [("track_id","track_id"),("title","track_title"),("artist","artist_name"),
              ("cover","album_cover"),("preview","preview_url")]}
    music = music if any(music.values()) else {}

    media = []
    for f in request.files.getlist("images"):
        if not f or not f.filename: continue
        saved, err = _store_file(f)
        if err: flash(err, "warning")
        elif saved: media.append(saved)

    if not media: flash("No files uploaded successfully.", "error"); return redirect(url_for("create_snap"))
    try:
        posts.insert_one(Snap.new(session["username"], media, caption, music))
        flash("Post published.", "success")
        return redirect(url_for("home"))
    except Exception as e:
        log.error("Upload error: %s", e); flash("Error creating post.", "error")
        return redirect(url_for("create_snap"))


@app.route("/delete_post/<post_id>", methods=["POST"])
@login_required
def delete_post(post_id):
    ensure_db()
    try: doc = posts.find_one({"_id": ObjectId(post_id)})
    except Exception: doc = None
    if not doc: flash("Post not found.", "error"); return redirect(url_for("home"))
    if doc.get("username") != session["username"]:
        flash("You can only delete your own posts.", "error")
    elif _delete_post(doc):
        flash("Post deleted.", "success")
    else:
        flash("Post already deleted.", "warning")
    return redirect(url_for("view_profile", username=session["username"]))


@app.route("/edit_post/<post_id>", methods=["POST"])
@login_required
def edit_post(post_id):
    ensure_db()
    caption = request.form.get("caption","").strip()
    ok, msg = Snap.validate_caption(caption)
    if not ok: flash(msg,"error"); return redirect(request.referrer or url_for("home"))
    try:
        doc = posts.find_one({"_id": ObjectId(post_id)})
        if not doc: flash("Post not found.","error")
        elif doc.get("username") != session["username"]: flash("You can only edit your own posts.","error")
        else:
            posts.update_one({"_id": ObjectId(post_id)}, {"$set":{"caption":caption,"updated_at":datetime.now()}})
            flash("Caption updated.", "success")
    except Exception as e:
        log.error("Edit post error: %s", e); flash("Unable to update post.","error")
    return redirect(request.referrer or url_for("home"))


@app.route("/saved")
@login_required
def saved_posts():
    ensure_db()
    user_doc = require_user()
    if not user_doc: return redirect(url_for("login"))
    ids  = [ObjectId(str(i)) for i in user_doc.get("saved_posts",[]) if _is_valid_oid(str(i))]
    feed = []
    if ids:
        order = {str(i): idx for idx, i in enumerate(user_doc["saved_posts"])}
        feed  = sorted(posts.find({"_id":{"$in":ids}}), key=lambda p: order.get(str(p.get("_id")), 0))
        feed  = _enrich_posts(feed, user_doc)
    return render_template("feed/saved.html", saved_posts=feed, user=user_doc)


# ── Profile & social routes ────────────────────────────────────────────────────
@app.route("/profile")
@login_required
def profile():
    return redirect(url_for("view_profile", username=session["username"]))


@app.route("/profile/<username>")
@login_required
def view_profile(username):
    ensure_db()
    try:
        user_doc = _with_defaults(users.find_one({"username": username}))
        if not user_doc: abort(404)
        viewer      = _with_defaults(users.find_one({"username": session["username"]}))
        user_posts  = _enrich_posts(list(posts.find({"username":username}).sort("created_at",-1)), viewer)
        is_owner    = session["username"] == username
        followers   = user_doc.get("followers",[])
        return render_template("profile.html", user=user_doc, posts=user_posts,
                               followers_count=len(followers),
                               following_count=len(user_doc.get("following",[])),
                               is_owner=is_owner,
                               is_following=(session["username"] in followers) if not is_owner else False,
                               insights=_profile_insights(user_doc, user_posts))
    except HTTPException: raise
    except Exception as e:
        log.error("Profile error for %s: %s", username, e)
        flash("Error loading profile.", "error"); return redirect(url_for("home"))


@app.route("/profile/<username>/connections")
@login_required
def profile_connections(username):
    ensure_db()
    try:
        user_doc = _with_defaults(users.find_one({"username": username}))
        if not user_doc: abort(404)
        tab  = request.args.get("tab","followers").lower()
        if tab not in {"followers","following"}: tab = "followers"
        fc   = _build_user_cards(user_doc.get("followers",[]))
        fwc  = _build_user_cards(user_doc.get("following",[]))
        return render_template("profile_connections.html", user=user_doc, active_tab=tab,
                               followers=fc, following=fwc, active_list=fc if tab=="followers" else fwc)
    except HTTPException: raise
    except Exception as e:
        log.error("Connections error: %s", e); flash("Unable to load list.","error")
        return redirect(url_for("view_profile", username=username))


@app.route("/follow/<username>", methods=["POST"])
@login_required
def follow(username):
    ensure_db()
    me = require_user()
    if not me: return redirect(url_for("login"))
    if me["username"] == username:
        flash("You cannot follow yourself.","warning")
        return redirect(url_for("view_profile", username=username))
    try:
        if not users.find_one({"username": username}):
            flash("User not found.","error"); return redirect(url_for("home"))
        if username in me.get("following",[]):
            users.update_one({"username":me["username"]},{"$pull":{"following":username}})
            users.update_one({"username":username},{"$pull":{"followers":me["username"]}})
        else:
            users.update_one({"username":me["username"]},{"$addToSet":{"following":username}})
            users.update_one({"username":username},{"$addToSet":{"followers":me["username"]}})
            _add_notification(username, me["username"], "follow")
    except Exception as e:
        log.error("Follow error: %s", e); flash("Error updating follow status.","error")
    return redirect(url_for("view_profile", username=username))


@app.route("/change_profile_pic", methods=["POST"])
@login_required
def change_profile_pic():
    ensure_db()
    user_doc = require_user()
    if not user_doc: return redirect(url_for("login"))
    errs = _apply_profile_updates(user_doc, include_account=False)
    for e in errs: flash(e, "warning" if e == "No changes were submitted." else "error")
    if not errs: flash("Profile updated.", "success")
    return redirect(url_for("view_profile", username=user_doc["username"]))


# ── Interaction routes ─────────────────────────────────────────────────────────
@app.route("/like_post/<post_id>", methods=["POST"])
@login_required
def like_post(post_id):
    ensure_db()
    try:
        doc = posts.find_one({"_id": ObjectId(post_id)})
        if not doc:
            return (jsonify({"success":False,"error":"Post not found"}),404) if is_ajax() else (flash("Post not found.","error"), redirect(url_for("home")))[1]
        uname = session["username"]
        liked = uname not in doc.get("likes",[])
        op    = "$addToSet" if liked else "$pull"
        posts.update_one({"_id":ObjectId(post_id)},{op:{"likes":uname}})
        if liked: _add_notification(doc.get("username",""), uname, "like", post_id)
        count = len(doc.get("likes",[])) + (1 if liked else -1)
        if is_ajax(): return jsonify({"success":True,"liked":liked,"likes_count":max(count,0)})
    except Exception as e:
        log.error("Like error: %s", e)
        if is_ajax(): return jsonify({"success":False,"error":"Like failed"}),500
        flash("Error liking post.","error")
    return redirect(request.referrer or url_for("home"))


@app.route("/save_post/<post_id>", methods=["POST"])
@login_required
def save_post(post_id):
    ensure_db()
    try:
        if not posts.find_one({"_id":ObjectId(post_id)},{"_id":1}):
            return (jsonify({"success":False,"error":"Post not found"}),404) if is_ajax() else (flash("Post not found.","error"), redirect(request.referrer or url_for("home")))[1]
        user_doc = _with_defaults(users.find_one({"username":session["username"]}))
        saved    = post_id in (user_doc or {}).get("saved_posts",[])
        op       = "$pull" if saved else "$addToSet"
        users.update_one({"username":session["username"]},{op:{"saved_posts":post_id},"$set":{"updated_at":datetime.now()}})
        is_saved = not saved
        if is_ajax(): return jsonify({"success":True,"saved":is_saved})
        flash("Post saved." if is_saved else "Post removed from saved.","success")
    except Exception as e:
        log.error("Save post error: %s", e)
        if is_ajax(): return jsonify({"success":False,"error":"Save failed"}),500
        flash("Unable to update saved posts.","error")
    return redirect(request.referrer or url_for("home"))


@app.route("/comment_post/<post_id>", methods=["POST"])
@login_required
def comment_post(post_id):
    ensure_db()
    text = request.form.get("comment","").strip()
    if not text or len(text) > 500:
        msg = "Comment cannot be empty." if not text else "Comment must be 500 characters or less."
        return (jsonify({"success":False,"error":msg}),400) if is_ajax() else (flash(msg,"error"), redirect(request.referrer or url_for("home")))[1]
    try:
        doc = posts.find_one({"_id":ObjectId(post_id)})
        if not doc:
            return (jsonify({"success":False,"error":"Post not found"}),404) if is_ajax() else (flash("Post not found.","error"), redirect(url_for("home")))[1]
        c = Snap.new_comment(session["username"], text)
        posts.update_one({"_id":ObjectId(post_id)},{"$push":{"comments":c}})
        _add_notification(doc.get("username",""), session["username"], "comment", post_id)
        if is_ajax():
            return jsonify({"success":True,
                            "comment":{"username":c["username"],"text":c["text"],
                                       "created_at_label":c["created_at"].strftime("%d %b %Y")},
                            "comments_count":len(doc.get("comments",[]))+1})
        flash("Comment added.","success")
    except Exception as e:
        log.error("Comment error: %s", e)
        if is_ajax(): return jsonify({"success":False,"error":"Error adding comment"}),500
        flash("Error adding comment.","error")
    return redirect(request.referrer or url_for("home"))


@app.route("/delete_comment/<post_id>/<int:idx>", methods=["POST"])
@login_required
def delete_comment(post_id, idx):
    ensure_db()
    try:
        doc = posts.find_one({"_id":ObjectId(post_id)})
        if not doc: flash("Post not found.","error"); return redirect(request.referrer or url_for("home"))
        comments = doc.get("comments",[])
        if not (0 <= idx < len(comments)): flash("Comment not found.","error"); return redirect(request.referrer or url_for("home"))
        if comments[idx].get("username") != session["username"]:
            flash("You can only delete your own comments.","error"); return redirect(request.referrer or url_for("home"))
        posts.update_one({"_id":ObjectId(post_id)},{"$unset":{f"comments.{idx}":1}})
        posts.update_one({"_id":ObjectId(post_id)},{"$pull":{"comments":None}})
        flash("Comment deleted.","success")
    except Exception as e:
        log.error("Delete comment error: %s", e); flash("Error deleting comment.","error")
    return redirect(request.referrer or url_for("home"))


# ── Media serving ──────────────────────────────────────────────────────────────
@app.route("/media/profile/<username>")
@login_required
def serve_profile_media(username):
    ensure_db()
    doc = users.find_one({"username":username},{"profile_image":1,"gender":1})
    if not doc: abort(404)
    pf  = doc.get("profile_image")
    r   = _send_media(pf)
    if r: return r
    return redirect(url_for("static", filename=_norm_path(pf or _default_avatar(doc.get("gender","Male")))))


@app.route("/media/post/<post_id>/<int:media_index>")
@login_required
def serve_post_media(post_id, media_index):
    ensure_db()
    try: doc = posts.find_one({"_id":ObjectId(post_id)},{"images":1})
    except Exception: doc = None
    if not doc: abort(404)
    entries = doc.get("images",[])
    if not (0 <= media_index < len(entries)): abort(404)
    v = entries[media_index]
    r = _send_media(v)
    if r: return r
    lp = _norm_path(v)
    if not lp: abort(404)
    return redirect(url_for("static", filename=lp))


@app.route("/media/gridfs/<file_id>")
@app.route("/media/<file_id>")
@login_required
def serve_gridfs_file(file_id):
    ensure_db()
    try: oid = ObjectId(file_id)
    except Exception: abort(404)
    return _send_gridfs_by_id(oid, attach=request.args.get("download","").lower() in {"1","true","yes"})


# ── Search & download ──────────────────────────────────────────────────────────
@app.route("/search")
@login_required
def search():
    ensure_db()
    q   = request.args.get("q","").strip()
    results = []
    try:
        if q:
            esc     = re.escape(q)
            results = list(users.find(
                {"username":{"$ne":session["username"]},
                 "$or":[{"username":{"$regex":esc,"$options":"i"}},
                        {"name":    {"$regex":esc,"$options":"i"}}]},
                {"username":1,"name":1,"profile_image":1,"gender":1,"followers":1}).limit(30))
            for u in results:
                u["profile_image_url"] = _avatar_url(u)
                u["followers_count"]   = len(u.get("followers",[]))
    except Exception as e:
        log.error("Search error: %s", e); flash("Search unavailable.","error")
    return render_template("feed/search.html", query=q, results=results)


@app.route("/download_post/<post_id>")
@login_required
def download_post(post_id):
    ensure_db()
    try: doc = posts.find_one({"_id":ObjectId(post_id)})
    except Exception: doc = None
    if not doc: flash("Post not found.","error"); return redirect(request.referrer or url_for("home"))
    try:
        entries = [v for v in doc.get("images",[]) if v]
        if not entries: flash("No media found.","error"); return redirect(request.referrer or url_for("home"))
        if len(entries) == 1:
            r = _send_media(entries[0], attach=True)
            if r: return r
            lp = _norm_path(entries[0]); sp = os.path.normpath(os.path.join("static",lp))
            if not sp.startswith(os.path.normpath("static")) or not os.path.exists(sp):
                flash("No downloadable media.","error"); return redirect(request.referrer or url_for("home"))
            return send_file(sp, as_attachment=True, download_name=os.path.basename(sp))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as zf:
            for i, v in enumerate(entries,1):
                name = secure_filename(_media_filename(v)) or f"file_{i:02d}{_ext(_media_filename(v)) or '.bin'}"
                if _is_gridfs(v):
                    try: g = fs.get(v["file_id"]) if fs else None
                    except NoFile: g = None
                    if g: zf.writestr(name, g.read())
                elif _is_db(v): zf.writestr(name, bytes(v.get("data",b"")))
                else:
                    sp = os.path.normpath(os.path.join("static",_norm_path(v)))
                    if sp.startswith(os.path.normpath("static")) and os.path.exists(sp):
                        with open(sp,"rb") as fh: zf.writestr(name, fh.read())
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"{doc.get('username','post')}_{post_id}.zip")
    except Exception as e:
        log.error("Download error: %s", e); flash("Download failed.","error")
        return redirect(url_for("home"))


# ── Notifications ──────────────────────────────────────────────────────────────
@app.route("/notifications")
@login_required
def notifications():
    ensure_db()
    try:
        user_doc = require_user()
        if not user_doc: return redirect(url_for("login"))
        return render_template("feed/notifications.html",
                               notifications=_enrich_notifications(user_doc.get("notifications",[])))
    except Exception as e:
        log.error("Notifications error: %s", e); flash("Unable to load notifications.","error")
        return redirect(url_for("home"))


@app.route("/notifications/live")
def notifications_live():
    ensure_db()
    if not session.get("username"): return jsonify({"success":False,"error":"Auth required"}),401
    user_doc = current_user()
    if not user_doc: session.clear(); return jsonify({"success":False,"error":"Session expired"}),401
    try:
        nd     = user_doc.get("notifications",[])
        unread = sum(1 for n in nd if not n.get("is_read"))
        items  = _enrich_notifications(nd[:max(NOTIF_FETCH_LIMIT,1)])
        return jsonify({"success":True,"unread_count":unread,"poll_interval_ms":NOTIF_POLL_MS,
                        "notifications":[{k:v for k,v in n.items() if k not in ("_id",)} for n in items]})
    except Exception as e:
        log.error("Live notifications error: %s", e); return jsonify({"success":False,"error":"Failed"}),500


@app.route("/notifications/read_all", methods=["POST"])
@login_required
def mark_notifications_read():
    ensure_db()
    try:
        user_doc = require_user()
        if not user_doc: return redirect(url_for("login"))
        nd = user_doc.get("notifications",[])
        for n in nd: n["is_read"] = True
        users.update_one({"username":session["username"]},
                          {"$set":{"notifications":nd,"updated_at":datetime.now()}})
        flash("All notifications marked as read.","success")
    except Exception as e:
        log.error("Mark read error: %s", e); flash("Unable to mark notifications.","error")
    return redirect(url_for("notifications"))


# ── Settings & account ─────────────────────────────────────────────────────────
@app.route("/settings", methods=["GET","POST"])
@login_required
def settings():
    ensure_db()
    user_doc = require_user()
    if not user_doc: return redirect(url_for("login"))
    if request.method == "POST":
        action = request.form.get("action","profile")
        if action == "profile":
            errs = _apply_profile_updates(user_doc, include_account=True)
            for e in errs: flash(e, "warning" if e=="No changes were submitted." else "error")
            if not errs: flash("Profile settings updated.","success")
        elif action == "password":
            cur, npw, cpw = (request.form.get(k,"") for k in ("current_password","new_password","confirm_password"))
            errs = []
            if not check_password_hash(user_doc.get("password",""), cur): errs.append("Current password is incorrect.")
            ok, msg = User.validate_password(npw)
            if not ok: errs.append(msg)
            if npw != cpw: errs.append("New passwords do not match.")
            if errs:
                for e in errs: flash(e,"error")
            else:
                users.update_one({"username":session["username"]},
                                  {"$set":{"password":generate_password_hash(npw),"updated_at":datetime.now()}})
                flash("Password updated.","success")
        return redirect(url_for("settings"))
    latest = _with_defaults(users.find_one({"username":session["username"]}))
    if not latest: session.clear(); flash("Account not found.","warning"); return redirect(url_for("login"))
    psts = list(posts.find({"username":session["username"]}).sort("created_at",-1))
    return render_template("settings.html", user=latest, insights=_profile_insights(latest, psts))


@app.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    ensure_db()
    uname   = session["username"]
    confirm = request.form.get("confirm_username","").strip()
    if confirm != uname: flash("Type your exact username to confirm.","error"); return redirect(url_for("settings"))
    try:
        user_doc = _with_defaults(users.find_one({"username":uname}))
        if not user_doc: session.clear(); return redirect(url_for("login"))
        for p in posts.find({"username":uname},{"_id":1,"images":1}): _delete_post(p)
        pf = user_doc.get("profile_image","")
        if pf and not _is_default_avatar(pf): _safe_delete(pf)
        if users.delete_one({"username":uname}).deleted_count != 1:
            flash("Unable to delete account.","error"); return redirect(url_for("settings"))
        users.update_many({},{"$pull":{"followers":uname,"following":uname,"notifications":{"actor":uname}}})
        posts.update_many({},{"$pull":{"likes":uname,"comments":{"username":uname}}})
        session.clear()
        flash("Your account was deleted.","success")
        return redirect(url_for("login"))
    except Exception as e:
        log.error("Delete account error: %s", e); flash("Unable to delete account.","error")
        return redirect(url_for("settings"))


# ── Misc routes ────────────────────────────────────────────────────────────────
@app.route("/post/<post_id>")
@login_required
def view_post(post_id):
    return redirect(url_for("home"))

@app.route("/about")
def about():
    return redirect(url_for("login") + "#about-section")

@app.route("/design/ultra")
def design_ultra():
    return render_template("figma_ui/pyinsta_ultra.html")


# ── Error handlers ─────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(_): return render_template("404.html"), 404

@app.errorhandler(503)
def unavailable(_): return "Database unavailable. Check MongoDB connection.", 503

@app.errorhandler(500)
def server_error(e): log.error("500: %s", e); return render_template("500.html"), 500


# ── Utility ────────────────────────────────────────────────────────────────────
def _is_valid_oid(s):
    try: ObjectId(s); return True
    except Exception: return False


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
