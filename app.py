from __future__ import annotations
"""Main Flask application for PyInsta Social.

This file is organized by sections:
1) application setup
2) infrastructure/helpers
3) route groups (auth, feed, profile, interactions, settings)
4) error handlers
"""

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
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from gridfs import GridFS
from gridfs.errors import NoFile
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from config import db as mongo_db
from config import posts_collection as posts
from config import users_collection as users

# =============================
# Application Setup
# =============================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "cloud.env", override=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "pyinsta_secret_key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB per request

FEED_PAGE_SIZE = 20
FEED_MAINTENANCE_INTERVAL_SECONDS = int(os.getenv("FEED_MAINTENANCE_INTERVAL_SECONDS", "3600"))
MEDIA_CACHE_MAX_AGE_SECONDS = 86400
NOTIFICATION_POLL_INTERVAL_MS = int(os.getenv("NOTIFICATION_POLL_INTERVAL_MS", "15000"))
NOTIFICATION_POPUP_FETCH_LIMIT = int(os.getenv("NOTIFICATION_POPUP_FETCH_LIMIT", "8"))
_last_feed_maintenance_run_at: datetime | None = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
fs = GridFS(mongo_db) if mongo_db is not None else None

DEFAULT_PROFILE_IMAGES = {
    "Female": "uploads/profile/female_default.svg",
    "Male": "uploads/profile/male_default.svg",
    "Other": "uploads/profile/other_default.svg",
}


# =============================
# Domain Models
# =============================
class User:
    """User data model with validation methods."""

    VALID_GENDERS = ["Male", "Female", "Other"]

    @staticmethod
    def validate_username(username: str) -> tuple[bool, str]:
        """Validate username format."""
        if not username or len(username) < 3 or len(username) > 20:
            return False, "Username must be 3-20 characters"

        if not re.match(r"^[a-zA-Z0-9_]+$", username):
            return False, "Username can only contain letters, numbers, and underscore"

        return True, ""

    @staticmethod
    def validate_email(email: str) -> tuple[bool, str]:
        """Validate email format."""
        try:
            validate_email(email, check_deliverability=False)
            return True, ""
        except EmailNotValidError as e:
            return False, f"Invalid email: {str(e)}"

    @staticmethod
    def validate_password(password: str) -> tuple[bool, str]:
        """Validate password strength."""
        if not password or len(password) < 6:
            return False, "Password must be at least 6 characters"

        if not re.search(r"[a-zA-Z]", password):
            return False, "Password must contain at least one letter"

        if not re.search(r"\d", password):
            return False, "Password must contain at least one number"

        return True, ""

    @staticmethod
    def validate_phone(phone: str) -> tuple[bool, str]:
        """Validate phone format."""
        if not phone:
            return True, ""

        clean_phone = re.sub(r"[\s\-\+\(\)]", "", phone)
        if not clean_phone.isdigit() or len(clean_phone) < 10:
            return False, "Phone must be at least 10 digits"

        return True, ""

    @staticmethod
    def validate_gender(gender: str) -> bool:
        """Validate gender value."""
        return gender in User.VALID_GENDERS

    @staticmethod
    def create_user_doc(
        name: str,
        username: str,
        email: str,
        phone: str,
        gender: str,
        password_hash: str,
        profile_image,
    ) -> dict:
        """Create a user document with default values."""
        return {
            "name": name,
            "username": username,
            "email": email,
            "phone": phone,
            "gender": gender,
            "password": password_hash,
            "profile_image": profile_image,
            "bio": "",
            "followers": [],
            "following": [],
            "saved_posts": [],
            "notifications": [],
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        }


class Snap:
    """Post/snap data model with validation methods."""

    ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov", ".mkv", ".avi", ".flv"}
    ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

    @staticmethod
    def validate_caption(caption: str) -> tuple[bool, str]:
        """Validate caption length."""
        if caption is None:
            return True, ""

        caption = str(caption).strip()
        if len(caption) > 2000:
            return False, "Caption must be less than 2000 characters"

        return True, ""

    @staticmethod
    def validate_file_extension(filename: str) -> bool:
        """Check if file has an allowed extension."""
        if not filename:
            return False

        ext = os.path.splitext(filename)[1].lower()
        return ext in Snap.ALLOWED_EXTENSIONS

    @staticmethod
    def validate_file_size(file_obj) -> bool:
        """Check file size before saving."""
        file_obj.seek(0, os.SEEK_END)
        file_size = file_obj.tell()
        file_obj.seek(0)
        return file_size <= Snap.MAX_FILE_SIZE

    @staticmethod
    def is_image(filename: str) -> bool:
        """Check if file is an image."""
        ext = os.path.splitext(filename)[1].lower()
        return ext in Snap.ALLOWED_IMAGE_EXTENSIONS

    @staticmethod
    def is_video(filename: str) -> bool:
        """Check if file is a video."""
        ext = os.path.splitext(filename)[1].lower()
        return ext in Snap.ALLOWED_VIDEO_EXTENSIONS

    @staticmethod
    def create_snap_doc(username: str, images: list, caption: str = "") -> dict:
        """Create a snap/post document."""
        return {
            "username": username,
            "images": images,
            "caption": caption.strip() if caption else "",
            "likes": [],
            "comments": [],
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        }

    @staticmethod
    def create_comment_doc(username: str, comment_text: str) -> dict:
        """Create a comment document."""
        return {
            "username": username,
            "text": comment_text.strip(),
            "created_at": datetime.now(),
        }


# =============================
# Infrastructure Helpers
# =============================
# ---- Request / session guards ----
def ensure_db() -> None:
    """Abort with 503 when DB is unavailable."""
    if users is None or posts is None:
        abort(503)


def login_required(view_func):
    """Route decorator to enforce authenticated access."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_global_template_data():
    """Inject global template data like unread notification count."""
    unread_notifications = 0
    username = session.get("username")
    if username and users is not None:
        user_doc = users.find_one({"username": username}, {"notifications": 1})
        notifications_data = (user_doc or {}).get("notifications", [])
        unread_notifications = sum(1 for item in notifications_data if not item.get("is_read"))

    return {
        "unread_notifications": unread_notifications,
        "notification_poll_interval_ms": NOTIFICATION_POLL_INTERVAL_MS,
    }


def is_ajax_request() -> bool:
    """Check if the current request is an AJAX-style request."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def get_current_session_user_doc() -> dict | None:
    """Return the logged-in user document with defaults applied."""
    username = session.get("username")
    if not username or users is None:
        return None
    return ensure_user_defaults(users.find_one({"username": username}))


def require_current_session_user_doc() -> dict | None:
    """Return the current user doc or clear stale session state."""
    user_doc = get_current_session_user_doc()
    if user_doc:
        return user_doc

    session.clear()
    flash("Your account was not found. Please log in again.", "warning")
    return None


# ---- Media path / upload helpers ----
def normalize_stored_media_path(path: str) -> str:
    """Normalize legacy paths so templates always receive uploads/... style paths."""
    if not path:
        return ""
    if path.startswith("uploads/"):
        return path
    return f"uploads/{path.lstrip('/')}"


def is_gridfs_media(media_value) -> bool:
    """Return True when a media field references a GridFS file."""
    return (
        isinstance(media_value, dict)
        and media_value.get("storage") == "gridfs"
        and media_value.get("file_id") is not None
    )


def is_db_media(media_value) -> bool:
    """Return True when a media field is stored inline in MongoDB."""
    return isinstance(media_value, dict) and media_value.get("storage") == "db" and "data" in media_value


def get_media_filename(media_value) -> str:
    """Extract a stable filename from a stored media value."""
    if isinstance(media_value, dict):
        return (media_value.get("filename") or "").strip()
    if isinstance(media_value, str):
        return os.path.basename(media_value.strip())
    return ""


def get_media_content_type(media_value) -> str:
    """Infer the media content type from stored metadata or filename."""
    if isinstance(media_value, dict):
        content_type = (media_value.get("content_type") or "").strip()
        if content_type:
            return content_type

    filename = get_media_filename(media_value)
    guessed_type, _ = mimetypes.guess_type(filename)
    return guessed_type or "application/octet-stream"


def media_is_video(media_value) -> bool:
    """Check whether the stored media should be rendered as a video."""
    content_type = get_media_content_type(media_value)
    if content_type.startswith("video/"):
        return True
    return Snap.is_video(get_media_filename(media_value))


def build_media_document(file_obj) -> tuple[dict | None, str | None]:
    """Validate an uploaded file and store it in GridFS."""
    filename = (file_obj.filename or "").strip()
    if not filename:
        return None, "No file selected"

    if not Snap.validate_file_extension(filename):
        return None, f"Invalid file type: {filename}"

    if not Snap.validate_file_size(file_obj):
        return None, f"File too large: {filename}"

    safe_name = secure_filename(filename) or f"upload{os.path.splitext(filename)[1].lower()}"
    content_type = (getattr(file_obj, "mimetype", None) or "").strip()
    if not content_type:
        content_type = get_media_content_type(safe_name)

    if fs is None:
        return None, "File storage is unavailable right now"

    file_obj.seek(0)
    file_id = fs.put(
        file_obj.stream,
        filename=safe_name,
        content_type=content_type,
    )
    file_obj.seek(0)

    return {
        "storage": "gridfs",
        "file_id": file_id,
        "filename": safe_name,
        "content_type": content_type,
        "kind": "video" if media_is_video({"filename": safe_name, "content_type": content_type}) else "image",
    }, None


def get_default_profile_image(gender: str) -> str:
    """Return default profile image based on gender."""
    return DEFAULT_PROFILE_IMAGES.get(gender, DEFAULT_PROFILE_IMAGES["Male"])


def is_default_profile_image(path) -> bool:
    """Check whether image path is one of the app default profile images."""
    if isinstance(path, dict):
        return False
    normalized = normalize_stored_media_path(path)
    return normalized in DEFAULT_PROFILE_IMAGES.values()


def save_media_file(file_obj, require_image: bool = False) -> tuple[dict | None, str | None]:
    """Validate and persist an uploaded file into MongoDB-ready structure."""
    filename = (file_obj.filename or "").strip()
    if require_image and filename and not Snap.is_image(filename):
        return None, "Please upload an image file"
    return build_media_document(file_obj)


def get_legacy_static_media_url(path: str, fallback_gender: str = "Male") -> str:
    """Return a static URL for legacy path-based media values."""
    normalized = normalize_stored_media_path(path or get_default_profile_image(fallback_gender))
    return url_for("static", filename=normalized)


def get_user_profile_image(user_doc: dict | None) -> str:
    """Return a display-ready profile image URL for a user doc."""
    if not user_doc:
        return get_legacy_static_media_url("", "Male")

    username = user_doc.get("username", "")
    if username:
        return url_for("serve_profile_media", username=username)

    profile_image = user_doc.get("profile_image", "")
    if is_db_media(profile_image):
        return get_legacy_static_media_url("", user_doc.get("gender", "Male"))

    return get_legacy_static_media_url(profile_image, user_doc.get("gender", "Male"))


def build_post_media_items(post_doc: dict) -> list[dict]:
    """Build display-ready post media metadata with streaming URLs."""
    post_id = str(post_doc.get("_id", ""))
    items: list[dict] = []

    for index, media_value in enumerate(post_doc.get("images", [])):
        if not media_value:
            continue
        items.append(
            {
                "url": url_for("serve_post_media", post_id=post_id, media_index=index),
                "filename": get_media_filename(media_value),
                "content_type": get_media_content_type(media_value),
                "is_video": media_is_video(media_value),
            }
        )

    return items


# ---- Media cleanup helpers ----
def safe_delete_static_file(relative_path: str) -> None:
    """Delete a static file path if it exists inside static/."""
    if is_gridfs_media(relative_path):
        if fs is None:
            return
        try:
            fs.delete(relative_path.get("file_id"))
        except Exception as exc:
            logger.warning("Failed to delete GridFS file %s: %s", relative_path.get("file_id"), exc)
        return
    if is_db_media(relative_path):
        return
    normalized = normalize_stored_media_path(relative_path)
    absolute_path = os.path.normpath(os.path.join("static", normalized))

    if not absolute_path.startswith(os.path.normpath("static")):
        logger.warning("Skipped unsafe delete path: %s", absolute_path)
        return

    if os.path.exists(absolute_path):
        os.remove(absolute_path)
        logger.info("Deleted file: %s", absolute_path)


def delete_post_and_media(post_doc: dict) -> bool:
    """Delete post media files and remove the post document from DB."""
    if not post_doc:
        return False

    post_id = post_doc.get("_id")
    if not post_id:
        return False

    for media_value in post_doc.get("images", []):
        safe_delete_static_file(media_value)

    delete_result = posts.delete_one({"_id": post_id})
    users.update_many({}, {"$pull": {"saved_posts": str(post_id)}})
    users.update_many({}, {"$pull": {"notifications": {"post_id": str(post_id)}}})
    return delete_result.deleted_count > 0


# ---- Data integrity helpers ----
def cleanup_orphan_posts() -> int:
    """Remove all posts whose owner account does not exist."""
    candidate_posts = list(posts.find({}, {"_id": 1, "username": 1, "images": 1}))
    if not candidate_posts:
        return 0

    usernames = {post.get("username", "") for post in candidate_posts if post.get("username")}
    existing_usernames = set()
    if usernames:
        existing_usernames = {
            doc["username"]
            for doc in users.find({"username": {"$in": list(usernames)}}, {"username": 1})
        }

    removed_count = 0

    for post_doc in candidate_posts:
        post_username = post_doc.get("username", "")
        if post_username and post_username in existing_usernames:
            continue

        if delete_post_and_media(post_doc):
            removed_count += 1
            logger.info("Removed orphan post %s from deleted user %s", post_doc.get("_id"), post_username)

    return removed_count


def sanitize_username_list(
    raw_values: list,
    valid_usernames: set[str],
    disallow_username: str | None = None,
) -> list[str]:
    """Return de-duplicated usernames that still exist and are not self-references."""
    cleaned: list[str] = []
    seen: set[str] = set()

    for value in raw_values or []:
        if not isinstance(value, str):
            continue

        candidate = value.strip()
        if not candidate or candidate not in valid_usernames:
            continue
        if disallow_username and candidate == disallow_username:
            continue
        if candidate in seen:
            continue

        seen.add(candidate)
        cleaned.append(candidate)

    return cleaned


def cleanup_social_references() -> tuple[int, int]:
    """Clean stale user references in followers/following/likes/comments/notifications."""
    existing_usernames = {
        doc["username"]
        for doc in users.find({}, {"username": 1})
        if isinstance(doc.get("username"), str) and doc.get("username").strip()
    }
    valid_post_ids = {
        str(doc["_id"])
        for doc in posts.find({}, {"_id": 1})
        if doc.get("_id") is not None
    }
    if not existing_usernames:
        return 0, 0

    user_updates = 0
    post_updates = 0

    user_docs = list(
        users.find({}, {"_id": 1, "username": 1, "followers": 1, "following": 1, "saved_posts": 1, "notifications": 1})
    )
    for user_doc in user_docs:
        username = user_doc.get("username", "")
        followers = sanitize_username_list(user_doc.get("followers", []), existing_usernames, disallow_username=username)
        following = sanitize_username_list(user_doc.get("following", []), existing_usernames, disallow_username=username)

        notifications = []
        for item in user_doc.get("notifications", []):
            if not isinstance(item, dict):
                continue
            actor = item.get("actor")
            if actor and actor not in existing_usernames:
                continue
            notifications.append(item)

        saved_posts = []
        seen_saved_posts: set[str] = set()
        for raw_post_id in user_doc.get("saved_posts", []) or []:
            post_id = str(raw_post_id).strip()
            if not post_id or post_id in seen_saved_posts or post_id not in valid_post_ids:
                continue
            seen_saved_posts.add(post_id)
            saved_posts.append(post_id)

        updates = {}
        if followers != user_doc.get("followers", []):
            updates["followers"] = followers
        if following != user_doc.get("following", []):
            updates["following"] = following
        if saved_posts != user_doc.get("saved_posts", []):
            updates["saved_posts"] = saved_posts
        if notifications != user_doc.get("notifications", []):
            updates["notifications"] = notifications

        if updates:
            updates["updated_at"] = datetime.now()
            users.update_one({"_id": user_doc["_id"]}, {"$set": updates})
            user_updates += 1

    post_docs = list(posts.find({}, {"_id": 1, "likes": 1, "comments": 1}))
    for post_doc in post_docs:
        likes = sanitize_username_list(post_doc.get("likes", []), existing_usernames)

        comments = []
        for comment in post_doc.get("comments", []):
            if not isinstance(comment, dict):
                continue
            comment_username = comment.get("username")
            if not comment_username or comment_username not in existing_usernames:
                continue
            comments.append(comment)

        updates = {}
        if likes != post_doc.get("likes", []):
            updates["likes"] = likes
        if comments != post_doc.get("comments", []):
            updates["comments"] = comments

        if updates:
            updates["updated_at"] = datetime.now()
            posts.update_one({"_id": post_doc["_id"]}, {"$set": updates})
            post_updates += 1

    return user_updates, post_updates


def remove_user_references(deleted_username: str) -> None:
    """Remove one username from all relational arrays across collections."""
    if not deleted_username:
        return

    users.update_many({}, {"$pull": {"followers": deleted_username}})
    users.update_many({}, {"$pull": {"following": deleted_username}})
    users.update_many({}, {"$pull": {"notifications": {"actor": deleted_username}}})
    posts.update_many({}, {"$pull": {"likes": deleted_username}})
    posts.update_many({}, {"$pull": {"comments": {"username": deleted_username}}})


def ensure_user_defaults(user_doc: dict | None) -> dict | None:
    """Backfill missing user fields for older accounts."""
    if not user_doc:
        return None

    gender = user_doc.get("gender", "Male")
    profile_image = user_doc.get("profile_image", "")
    updates = {}

    if "bio" not in user_doc:
        updates["bio"] = ""
    if "followers" not in user_doc:
        updates["followers"] = []
    if "following" not in user_doc:
        updates["following"] = []
    if "saved_posts" not in user_doc:
        updates["saved_posts"] = []
    if "notifications" not in user_doc:
        updates["notifications"] = []
    if not profile_image:
        updates["profile_image"] = get_default_profile_image(gender)

    if updates:
        updates["updated_at"] = datetime.now()
        users.update_one({"_id": user_doc["_id"]}, {"$set": updates})
        user_doc.update(updates)

    username = user_doc.get("username", "")
    related_usernames = set()
    for candidate in (user_doc.get("followers", []) or []) + (user_doc.get("following", []) or []):
        if isinstance(candidate, str) and candidate.strip():
            related_usernames.add(candidate.strip())

    if related_usernames:
        valid_related_usernames = {
            doc["username"]
            for doc in users.find({"username": {"$in": list(related_usernames)}}, {"username": 1})
        }
        cleaned_followers = sanitize_username_list(
            user_doc.get("followers", []),
            valid_related_usernames,
            disallow_username=username,
        )
        cleaned_following = sanitize_username_list(
            user_doc.get("following", []),
            valid_related_usernames,
            disallow_username=username,
        )

        relation_updates = {}
        if cleaned_followers != user_doc.get("followers", []):
            relation_updates["followers"] = cleaned_followers
        if cleaned_following != user_doc.get("following", []):
            relation_updates["following"] = cleaned_following

        if relation_updates:
            relation_updates["updated_at"] = datetime.now()
            users.update_one({"_id": user_doc["_id"]}, {"$set": relation_updates})
            user_doc.update(relation_updates)

    if not user_doc.get("profile_image"):
        user_doc["profile_image"] = get_default_profile_image(gender)
    user_doc["profile_image_url"] = get_user_profile_image(user_doc)
    return user_doc


def build_profile_insights(user_doc: dict | None, user_posts: list[dict] | None) -> dict:
    """Build profile analytics and completion metadata for templates."""
    user_doc = user_doc or {}
    user_posts = user_posts or []

    total_likes = sum(len(post.get("likes", []) or []) for post in user_posts)
    total_comments = sum(len(post.get("comments", []) or []) for post in user_posts)
    media_items = sum(len(post.get("images", []) or []) for post in user_posts)
    average_engagement = round((total_likes + total_comments) / len(user_posts), 1) if user_posts else 0

    completion_checks = [
        ("Display name", bool((user_doc.get("name") or "").strip())),
        ("Email", bool((user_doc.get("email") or "").strip())),
        ("Phone", bool((user_doc.get("phone") or "").strip())),
        ("Bio", bool((user_doc.get("bio") or "").strip())),
        (
            "Custom profile photo",
            bool(user_doc.get("profile_image")) and not is_default_profile_image(user_doc.get("profile_image")),
        ),
    ]
    completed_items = sum(1 for _, is_done in completion_checks if is_done)
    completion_percent = int((completed_items / len(completion_checks)) * 100) if completion_checks else 0

    return {
        "total_posts": len(user_posts),
        "total_likes": total_likes,
        "total_comments": total_comments,
        "media_items": media_items,
        "saved_posts": len(user_doc.get("saved_posts", []) or []),
        "average_engagement": average_engagement,
        "completion_percent": completion_percent,
        "completion_items": completion_checks,
        "missing_items": [label for label, is_done in completion_checks if not is_done],
        "joined_at": user_doc.get("created_at"),
        "last_posted_at": user_posts[0].get("created_at") if user_posts else None,
    }


def apply_profile_updates(user_doc: dict, *, include_account_fields: bool) -> list[str]:
    """Validate and persist profile/account updates from forms."""
    update_data = {"updated_at": datetime.now()}
    errors: list[str] = []

    bio = request.form.get("bio", "").strip()
    if len(bio) > 500:
        errors.append("Bio must be 500 characters or less.")
    else:
        update_data["bio"] = bio

    requested_gender = request.form.get("gender", "").strip()
    current_gender = user_doc.get("gender", "Male")
    new_gender = current_gender
    if requested_gender:
        if User.validate_gender(requested_gender):
            new_gender = requested_gender
        else:
            errors.append("Invalid gender selection.")
    if new_gender != current_gender:
        update_data["gender"] = new_gender

    if include_account_fields:
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name:
            errors.append("Name is required.")

        valid, message = User.validate_email(email)
        if not valid:
            errors.append(message)

        valid, message = User.validate_phone(phone)
        if not valid:
            errors.append(message)

        existing_email_user = users.find_one({"email": email, "username": {"$ne": user_doc["username"]}})
        if existing_email_user:
            errors.append("Email is already in use.")

        if not errors:
            update_data["name"] = name
            update_data["email"] = email
            update_data["phone"] = phone

    if errors:
        return errors

    image = request.files.get("profile_image")
    reset_profile_image = request.form.get("reset_profile_image") == "1"
    current_profile_image = user_doc.get("profile_image", "")

    if reset_profile_image:
        update_data["profile_image"] = get_default_profile_image(new_gender)
        if current_profile_image and not is_default_profile_image(current_profile_image):
            safe_delete_static_file(current_profile_image)

    if image and image.filename:
        saved_media, save_error = save_media_file(image, require_image=True)
        if save_error:
            errors.append(save_error)
        elif saved_media:
            update_data["profile_image"] = saved_media
            if current_profile_image and not is_default_profile_image(current_profile_image):
                safe_delete_static_file(current_profile_image)
            logger.info("Profile image updated for %s", user_doc["username"])
    elif "profile_image" not in update_data and (
        not current_profile_image or is_default_profile_image(current_profile_image)
    ) and new_gender != current_gender:
        update_data["profile_image"] = get_default_profile_image(new_gender)

    if errors:
        return errors

    has_changes = False
    for field, value in update_data.items():
        if field == "updated_at":
            continue
        if user_doc.get(field) != value:
            has_changes = True
            break

    if not has_changes:
        return ["No changes were submitted."]

    users.update_one({"_id": user_doc["_id"]}, {"$set": update_data})
    logger.info("Profile settings saved for %s", user_doc["username"])
    return []


# ---- View-model enrichment helpers ----
def build_user_card_list(usernames: list) -> list[dict]:
    """Build display-ready user cards in the same order as username list."""
    ordered_usernames: list[str] = []
    seen: set[str] = set()

    for item in usernames or []:
        if not isinstance(item, str):
            continue
        username = item.strip()
        if not username or username in seen:
            continue
        seen.add(username)
        ordered_usernames.append(username)

    if not ordered_usernames:
        return []

    user_docs = list(
        users.find(
            {"username": {"$in": ordered_usernames}},
            {"username": 1, "name": 1, "profile_image": 1, "gender": 1},
        )
    )
    user_map = {doc["username"]: ensure_user_defaults(doc) for doc in user_docs if doc.get("username")}

    cards: list[dict] = []
    for username in ordered_usernames:
        user_doc = user_map.get(username)
        if not user_doc:
            continue
        cards.append(
            {
                "username": user_doc.get("username", ""),
                "name": user_doc.get("name", ""),
                "profile_image_url": get_user_profile_image(user_doc),
            }
        )

    return cards


def enrich_posts_for_view(feed_items: list[dict], current_user_doc: dict | None) -> list[dict]:
    """Attach normalized media, owner profile, and saved-state for post rendering."""
    if not feed_items:
        return []

    usernames = {post.get("username") for post in feed_items if post.get("username")}
    owner_docs = list(
        users.find(
            {"username": {"$in": list(usernames)}},
            {"username": 1, "profile_image": 1, "gender": 1, "name": 1},
        )
    )
    owner_map = {doc["username"]: ensure_user_defaults(doc) for doc in owner_docs}
    saved_post_ids = set((current_user_doc or {}).get("saved_posts", []))

    for post_doc in feed_items:
        owner_doc = owner_map.get(post_doc.get("username"))
        post_doc["owner_profile_image_url"] = get_user_profile_image(owner_doc)
        post_doc["media_items"] = build_post_media_items(post_doc)
        post_doc["primary_media"] = post_doc["media_items"][0] if post_doc["media_items"] else None
        post_doc["image_urls"] = [item["url"] for item in post_doc["media_items"] if not item["is_video"]]
        post_doc["is_saved"] = str(post_doc.get("_id")) in saved_post_ids

    return feed_items


def maybe_run_feed_maintenance() -> None:
    """Throttle expensive feed cleanup work so it does not run on every page load."""
    global _last_feed_maintenance_run_at

    now = datetime.now()
    if _last_feed_maintenance_run_at and (
        now - _last_feed_maintenance_run_at
    ).total_seconds() < FEED_MAINTENANCE_INTERVAL_SECONDS:
        return

    orphan_removed = cleanup_orphan_posts()
    if orphan_removed:
        logger.info("Removed %s orphan posts during feed maintenance", orphan_removed)

    repaired_users, repaired_posts = cleanup_social_references()
    if repaired_users or repaired_posts:
        logger.info(
            "Repaired social references during feed maintenance (users=%s, posts=%s)",
            repaired_users,
            repaired_posts,
        )

    _last_feed_maintenance_run_at = now


def build_download_filename(base_name: str, fallback_prefix: str, index: int, media_value) -> str:
    """Create a safe filename for download responses."""
    filename = secure_filename(get_media_filename(media_value))
    if filename:
        return filename

    ext = os.path.splitext(get_media_filename(media_value))[1].lower() or ".bin"
    return f"{fallback_prefix}_{index:02d}{ext}"


def apply_media_cache_headers(response, max_age: int = MEDIA_CACHE_MAX_AGE_SECONDS):
    """Mark streamed media as browser-cacheable for faster repeat loads."""
    response.cache_control.private = True
    response.cache_control.max_age = max_age
    response.cache_control.no_store = False
    response.expires = datetime.utcnow() + timedelta(seconds=max_age)
    return response


def send_db_media(media_value, download_name: str | None = None, as_attachment: bool = False):
    """Stream DB-stored media bytes to the client."""
    media_bytes = bytes(media_value.get("data", b""))
    response = send_file(
        io.BytesIO(media_bytes),
        mimetype=get_media_content_type(media_value),
        as_attachment=as_attachment,
        download_name=download_name or get_media_filename(media_value) or "media",
    )
    return apply_media_cache_headers(response)


def send_gridfs_media(media_value, download_name: str | None = None, as_attachment: bool = False):
    """Stream GridFS-backed media bytes to the client."""
    if fs is None:
        abort(503)

    try:
        grid_out = fs.get(media_value.get("file_id"))
    except NoFile:
        abort(404)

    response = send_file(
        io.BytesIO(grid_out.read()),
        mimetype=media_value.get("content_type") or getattr(grid_out, "content_type", None) or get_media_content_type(media_value),
        as_attachment=as_attachment,
        download_name=download_name or media_value.get("filename") or getattr(grid_out, "filename", None) or "media",
    )
    return apply_media_cache_headers(response)


def send_gridfs_file_by_id(file_id: ObjectId, as_attachment: bool = False):
    """Stream one GridFS file by fs.files._id."""
    if fs is None:
        abort(503)

    try:
        grid_out = fs.get(file_id)
    except NoFile:
        abort(404)

    filename = secure_filename((getattr(grid_out, "filename", None) or "").strip()) or f"{file_id}.bin"
    mimetype = (
        getattr(grid_out, "content_type", None)
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    response = send_file(
        io.BytesIO(grid_out.read()),
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=filename,
    )
    return apply_media_cache_headers(response)


def format_file_size(num_bytes: int | float | None) -> str:
    """Convert byte count into a short human-readable label."""
    size = float(num_bytes or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


# ---- Notification helpers ----
def add_notification(
    target_username: str,
    actor_username: str,
    notification_type: str,
    post_id: str | None = None,
) -> None:
    """Push a notification to target user."""
    if not target_username or target_username == actor_username:
        return

    text_by_type = {
        "follow": f"{actor_username} started following you.",
        "like": f"{actor_username} liked your post.",
        "comment": f"{actor_username} commented on your post.",
    }
    notification = {
        "id": uuid.uuid4().hex,
        "type": notification_type,
        "actor": actor_username,
        "text": text_by_type.get(notification_type, "You have a new notification."),
        "post_id": post_id,
        "is_read": False,
        "created_at": datetime.now(),
    }

    users.update_one(
        {"username": target_username},
        {"$push": {"notifications": {"$each": [notification], "$position": 0, "$slice": 100}}},
    )


def get_notification_public_id(notification_item: dict) -> str:
    """Return a stable public id for notification payloads."""
    raw_id = str(notification_item.get("id") or "").strip()
    if raw_id:
        return raw_id

    created_at = notification_item.get("created_at")
    created_at_iso = created_at.isoformat() if isinstance(created_at, datetime) else ""
    actor = str(notification_item.get("actor") or "").strip()
    notification_type = str(notification_item.get("type") or "").strip()
    post_id = str(notification_item.get("post_id") or "").strip()
    return f"{actor}:{notification_type}:{post_id}:{created_at_iso}"


def build_notification_target_url(notification_item: dict) -> str:
    """Choose the most useful destination for a notification click."""
    actor_username = str(notification_item.get("actor") or "").strip()
    if notification_item.get("type") == "follow" and actor_username:
        return url_for("view_profile", username=actor_username)
    return url_for("notifications")


def enrich_notifications_for_view(notifications_data: list[dict] | None) -> list[dict]:
    """Attach actor images, labels, and links for notification views and APIs."""
    normalized_items = [item for item in (notifications_data or []) if isinstance(item, dict)]
    if not normalized_items:
        return []

    actor_usernames = {
        item.get("actor")
        for item in normalized_items
        if isinstance(item.get("actor"), str) and item.get("actor").strip()
    }
    actor_map = {}
    if actor_usernames:
        actor_docs = list(
            users.find(
                {"username": {"$in": list(actor_usernames)}},
                {"username": 1, "profile_image": 1, "gender": 1},
            )
        )
        actor_map = {doc["username"]: doc for doc in actor_docs if doc.get("username")}

    enriched_items: list[dict] = []
    for item in normalized_items:
        created_at = item.get("created_at")
        actor_doc = actor_map.get(item.get("actor"))
        enriched_item = dict(item)
        enriched_item["id"] = get_notification_public_id(item)
        enriched_item["actor_profile_image_url"] = get_user_profile_image(actor_doc)
        enriched_item["created_at_label"] = (
            created_at.strftime("%d %b %Y, %H:%M") if isinstance(created_at, datetime) else ""
        )
        enriched_item["created_at_iso"] = created_at.isoformat() if isinstance(created_at, datetime) else ""
        enriched_item["link_url"] = build_notification_target_url(enriched_item)
        enriched_items.append(enriched_item)

    return enriched_items


def serialize_notification_item(notification_item: dict) -> dict:
    """Return a JSON-safe notification payload for live popups."""
    return {
        "id": notification_item.get("id", ""),
        "type": notification_item.get("type", ""),
        "actor": notification_item.get("actor", ""),
        "text": notification_item.get("text", ""),
        "post_id": notification_item.get("post_id"),
        "is_read": bool(notification_item.get("is_read")),
        "created_at_label": notification_item.get("created_at_label", ""),
        "created_at_iso": notification_item.get("created_at_iso", ""),
        "actor_profile_image_url": notification_item.get("actor_profile_image_url", ""),
        "link_url": notification_item.get("link_url", url_for("notifications")),
    }


@app.route("/media/profile/<username>")
@login_required
def serve_profile_media(username):
    """Serve a profile image from MongoDB or redirect to a legacy static asset."""
    ensure_db()
    user_doc = users.find_one({"username": username}, {"profile_image": 1, "gender": 1})
    if not user_doc:
        abort(404)

    profile_media = user_doc.get("profile_image")
    if is_gridfs_media(profile_media):
        return send_gridfs_media(
            profile_media,
            download_name=build_download_filename(username, f"{username}_profile", 1, profile_media),
        )
    if is_db_media(profile_media):
        return send_db_media(
            profile_media,
            download_name=build_download_filename(username, f"{username}_profile", 1, profile_media),
        )

    return redirect(get_legacy_static_media_url(profile_media, user_doc.get("gender", "Male")))


@app.route("/media/post/<post_id>/<int:media_index>")
@login_required
def serve_post_media(post_id, media_index):
    """Serve post media from MongoDB or redirect to a legacy static asset."""
    ensure_db()

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)}, {"images": 1})
    except Exception:
        post_doc = None

    if not post_doc:
        abort(404)

    media_entries = post_doc.get("images", [])
    if media_index < 0 or media_index >= len(media_entries):
        abort(404)

    media_value = media_entries[media_index]
    if is_gridfs_media(media_value):
        return send_gridfs_media(
            media_value,
            download_name=build_download_filename(post_id, f"post_{post_id}", media_index + 1, media_value),
        )
    if is_db_media(media_value):
        return send_db_media(
            media_value,
            download_name=build_download_filename(post_id, f"post_{post_id}", media_index + 1, media_value),
        )

    legacy_path = normalize_stored_media_path(media_value)
    if not legacy_path:
        abort(404)
    return redirect(url_for("static", filename=legacy_path))


@app.route("/media/gridfs/<file_id>")
@login_required
def serve_gridfs_file(file_id):
    """Serve any GridFS file directly by fs.files _id."""
    ensure_db()

    try:
        object_id = ObjectId(file_id)
    except Exception:
        abort(404)

    download_flag = request.args.get("download", "").strip().lower()
    as_attachment = download_flag in {"1", "true", "yes"}
    return send_gridfs_file_by_id(object_id, as_attachment=as_attachment)


@app.route("/media/<file_id>")
@login_required
def get_media(file_id):
    """Compatibility route for direct GridFS access by file id."""
    ensure_db()

    try:
        object_id = ObjectId(file_id)
    except Exception:
        abort(404)

    download_flag = request.args.get("download", "").strip().lower()
    as_attachment = download_flag in {"1", "true", "yes"}
    return send_gridfs_file_by_id(object_id, as_attachment=as_attachment)


@app.route("/saved")
@login_required
def saved_posts():
    """Show posts saved by the current user."""
    ensure_db()

    current_user_doc = require_current_session_user_doc()
    if not current_user_doc:
        return redirect(url_for("login"))

    saved_ids = current_user_doc.get("saved_posts", [])
    object_ids = []
    for raw_post_id in saved_ids:
        try:
            object_ids.append(ObjectId(str(raw_post_id)))
        except Exception:
            continue

    saved_feed = []
    if object_ids:
        saved_feed = list(posts.find({"_id": {"$in": object_ids}}))
        order_map = {post_id: index for index, post_id in enumerate(saved_ids)}
        saved_feed.sort(key=lambda item: order_map.get(str(item.get("_id")), len(saved_ids)))
        saved_feed = enrich_posts_for_view(saved_feed, current_user_doc)

    return render_template("feed/saved.html", saved_posts=saved_feed, user=current_user_doc)


# =============================
# Index Initialization
# =============================
try:
    if users is not None:
        users.create_index("username", unique=True)
        users.create_index("email", unique=True)
    if posts is not None:
        posts.create_index("username")
        posts.create_index("created_at")
        posts.create_index([("username", 1), ("created_at", -1)])
except Exception as exc:
    logger.warning("Could not create indexes: %s", exc)


# =============================
# Authentication Routes
# =============================
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    """Render and handle login."""
    ensure_db()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("auth/login.html")

        try:
            user_doc = users.find_one({"username": username})
            if user_doc and check_password_hash(user_doc["password"], password):
                ensure_user_defaults(user_doc)
                session["username"] = username
                logger.info("User %s logged in", username)
                return redirect(url_for("home"))

            flash("Invalid username or password.", "error")
            logger.warning("Failed login for username: %s", username)
        except Exception as exc:
            logger.error("Login error: %s", exc)
            flash("An error occurred while logging in.", "error")

    return render_template("auth/login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Reset a password by verifying username and email."""
    ensure_db()

    if "username" in session:
        return redirect(url_for("settings"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        errors: list[str] = []

        if not username:
            errors.append("Username is required.")

        valid, message = User.validate_email(email)
        if not valid:
            errors.append(message)

        valid, message = User.validate_password(new_password)
        if not valid:
            errors.append(message)

        if new_password != confirm_password:
            errors.append("Passwords do not match.")

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("auth/forgot_password.html")

        try:
            user_doc = users.find_one({"username": username})
            if not user_doc or (user_doc.get("email", "").strip().lower() != email.lower()):
                flash("Username and email do not match any account.", "error")
                return render_template("auth/forgot_password.html")

            users.update_one(
                {"_id": user_doc["_id"]},
                {"$set": {"password": generate_password_hash(new_password), "updated_at": datetime.now()}},
            )
            logger.info("Password reset completed for %s", username)
            flash("Password reset successful. Please log in with your new password.", "success")
            return redirect(url_for("login"))
        except Exception as exc:
            logger.error("Forgot password error: %s", exc)
            flash("Unable to reset password right now.", "error")

    return render_template("auth/forgot_password.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    """Render and handle registration."""
    ensure_db()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        gender = request.form.get("gender", "Male").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        errors: list[str] = []

        if not name:
            errors.append("Name is required")

        valid, message = User.validate_username(username)
        if not valid:
            errors.append(message)

        valid, message = User.validate_email(email)
        if not valid:
            errors.append(message)

        valid, message = User.validate_phone(phone)
        if not valid:
            errors.append(message)

        valid, message = User.validate_password(password)
        if not valid:
            errors.append(message)

        if password != confirm_password:
            errors.append("Passwords do not match")

        if not User.validate_gender(gender):
            errors.append("Invalid gender selection")

        if not errors and users.find_one({"username": username}):
            errors.append("Username already exists")

        if not errors and users.find_one({"email": email}):
            errors.append("Email already registered")

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("auth/register.html")

        profile_image = get_default_profile_image(gender)

        image = request.files.get("profile_image")
        if image and image.filename:
            saved_media, save_error = save_media_file(image, require_image=True)
            if save_error:
                flash(save_error, "warning")
            elif saved_media:
                profile_image = saved_media

        try:
            user_doc = User.create_user_doc(
                name,
                username,
                email,
                phone,
                gender,
                generate_password_hash(password),
                profile_image,
            )
            users.insert_one(user_doc)
            logger.info("Registered user: %s", username)
            flash("Account created successfully. Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as exc:
            logger.error("Registration error: %s", exc)
            flash("An error occurred during registration.", "error")

    return render_template("auth/register.html")


@app.route("/logout")
def logout():
    """Clear session and return to login."""
    username = session.get("username")
    session.clear()
    if username:
        logger.info("User %s logged out", username)
    return redirect(url_for("login"))


# =============================
# Feed and Post Routes
# =============================
@app.route("/home")
@login_required
def home():
    """Render latest feed items."""
    ensure_db()

    try:
        user_doc = require_current_session_user_doc()
        if not user_doc:
            return redirect(url_for("login"))

        maybe_run_feed_maintenance()

        feed_items = list(
            posts.find(
                {},
                {
                    "username": 1,
                    "images": 1,
                    "likes": 1,
                    "comments": 1,
                    "caption": 1,
                    "created_at": 1,
                },
            )
            .sort("created_at", -1)
            .limit(FEED_PAGE_SIZE)
        )
        feed_items = enrich_posts_for_view(feed_items, user_doc)

        following_usernames = set(user_doc.get("following", []))
        suggestions = list(
            users.find(
                {"username": {"$nin": list(following_usernames | {session["username"]})}},
                {"username": 1, "name": 1, "profile_image": 1, "gender": 1},
            ).limit(5)
        )
        for suggestion in suggestions:
            suggestion["profile_image_url"] = get_user_profile_image(suggestion)

        dashboard_stats = [
            {"label": "Posts", "value": posts.count_documents({"username": session["username"]})},
            {"label": "Following", "value": len(user_doc.get("following", []))},
            {"label": "Saved", "value": len(user_doc.get("saved_posts", []))},
            {
                "label": "Unread alerts",
                "value": sum(1 for item in user_doc.get("notifications", []) if not item.get("is_read")),
            },
        ]

        return render_template(
            "feed/home.html",
            user=user_doc,
            feed=feed_items,
            suggestions=suggestions,
            dashboard_stats=dashboard_stats,
        )
    except Exception as exc:
        logger.error("Home page error: %s", exc)
        flash("Unable to load feed right now.", "error")
        return render_template("feed/home.html", user=None, feed=[], suggestions=[], dashboard_stats=[])


@app.route("/create-snap", methods=["GET", "POST"])
@login_required
def create_snap():
    """Render the create form and handle submit via existing upload route."""
    ensure_db()

    if request.method == "POST":
        return upload()

    return render_template("create_snap.html")


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    """Handle image/video uploads for a new post."""
    ensure_db()

    if "images" not in request.files:
        flash("Select at least one image or video.", "error")
        return redirect(url_for("create_snap"))

    files = request.files.getlist("images")
    caption = request.form.get("caption", "").strip()

    valid, message = Snap.validate_caption(caption)
    if not valid:
        flash(message, "error")
        return redirect(url_for("create_snap"))

    saved_media_items: list[dict] = []
    for file_obj in files:
        if not file_obj or not file_obj.filename:
            continue

        try:
            saved_media, save_error = save_media_file(file_obj)
            if save_error:
                flash(save_error, "warning")
                continue
            if saved_media:
                saved_media_items.append(saved_media)
        except Exception as exc:
            logger.error("Upload error for %s: %s", file_obj.filename, exc)
            flash(f"Error uploading {file_obj.filename}", "error")

    if not saved_media_items:
        flash("No files were uploaded successfully.", "error")
        return redirect(url_for("create_snap"))

    try:
        snap_doc = Snap.create_snap_doc(session["username"], saved_media_items, caption)
        posts.insert_one(snap_doc)
        logger.info("Post created by %s", session["username"])
        flash("Post published successfully.", "success")
        return redirect(url_for("home"))
    except Exception as exc:
        logger.error("Post creation error: %s", exc)
        flash("Error creating post.", "error")
        return redirect(url_for("create_snap"))


@app.route("/delete_post/<post_id>", methods=["POST"])
@login_required
def delete_post(post_id):
    """Delete a user-owned post and its media files."""
    ensure_db()

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)})
    except Exception:
        post_doc = None

    if not post_doc:
        flash("Post not found.", "error")
        return redirect(url_for("home"))

    if post_doc.get("username") != session.get("username"):
        flash("You can only delete your own posts.", "error")
        return redirect(url_for("view_profile", username=session["username"]))

    try:
        was_deleted = delete_post_and_media(post_doc)
        if was_deleted:
            logger.info("Post %s deleted by %s", post_id, session["username"])
            flash("Post deleted successfully.", "success")
        else:
            flash("Post already deleted.", "warning")
    except Exception as exc:
        logger.error("Delete post error: %s", exc)
        flash("Error deleting post.", "error")

    return redirect(url_for("view_profile", username=session["username"]))


# =============================
# Profile and Social Routes
# =============================
@app.route("/profile")
@login_required
def profile():
    """Shortcut to the current user's profile."""
    ensure_db()
    return redirect(url_for("view_profile", username=session["username"]))


@app.route("/profile/<username>")
@login_required
def view_profile(username):
    """Render profile page for a user."""
    ensure_db()

    try:
        user_doc = ensure_user_defaults(users.find_one({"username": username}))
        if not user_doc:
            abort(404)

        user_posts = list(posts.find({"username": username}).sort("created_at", -1))
        current_user_doc = ensure_user_defaults(users.find_one({"username": session["username"]}))
        user_posts = enrich_posts_for_view(user_posts, current_user_doc)

        followers = user_doc.get("followers", [])
        following = user_doc.get("following", [])
        insights = build_profile_insights(user_doc, user_posts)

        is_owner = session["username"] == username
        is_following = (session["username"] in followers) if not is_owner else False

        return render_template(
            "profile.html",
            user=user_doc,
            posts=user_posts,
            followers_count=len(followers),
            following_count=len(following),
            is_owner=is_owner,
            is_following=is_following,
            insights=insights,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Profile view error for %s: %s", username, exc)
        flash("Error loading profile.", "error")
        return redirect(url_for("home"))


@app.route("/profile/<username>/connections")
@login_required
def profile_connections(username):
    """Show followers/following lists for a profile."""
    ensure_db()

    try:
        user_doc = ensure_user_defaults(users.find_one({"username": username}))
        if not user_doc:
            abort(404)

        active_tab = request.args.get("tab", "followers").strip().lower()
        if active_tab not in {"followers", "following"}:
            active_tab = "followers"

        follower_cards = build_user_card_list(user_doc.get("followers", []))
        following_cards = build_user_card_list(user_doc.get("following", []))
        active_list = follower_cards if active_tab == "followers" else following_cards

        return render_template(
            "profile_connections.html",
            user=user_doc,
            active_tab=active_tab,
            followers=follower_cards,
            following=following_cards,
            active_list=active_list,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Profile connections error for %s: %s", username, exc)
        flash("Unable to load followers/following right now.", "error")
        return redirect(url_for("view_profile", username=username))


@app.route("/follow/<username>", methods=["POST"])
@login_required
def follow(username):
    """Follow or unfollow another user."""
    ensure_db()

    current_user_doc = require_current_session_user_doc()
    if not current_user_doc:
        return redirect(url_for("login"))

    current_username = current_user_doc["username"]
    if current_username == username:
        flash("You cannot follow yourself.", "warning")
        return redirect(url_for("view_profile", username=username))

    try:
        current_user = current_user_doc
        target_user = users.find_one({"username": username})

        if not current_user or not target_user:
            flash("User not found.", "error")
            return redirect(url_for("home"))

        currently_following = username in current_user.get("following", [])

        if currently_following:
            users.update_one({"username": current_username}, {"$pull": {"following": username}})
            users.update_one({"username": username}, {"$pull": {"followers": current_username}})
            logger.info("%s unfollowed %s", current_username, username)
        else:
            users.update_one({"username": current_username}, {"$addToSet": {"following": username}})
            users.update_one({"username": username}, {"$addToSet": {"followers": current_username}})
            add_notification(username, current_username, "follow")
            logger.info("%s followed %s", current_username, username)

        return redirect(url_for("view_profile", username=username))
    except Exception as exc:
        logger.error("Follow error: %s", exc)
        flash("Error updating follow status.", "error")
        return redirect(url_for("view_profile", username=username))


@app.route("/change_profile_pic", methods=["POST"])
@login_required
def change_profile_pic():
    """Update profile picture and bio."""
    ensure_db()

    current_user_doc = require_current_session_user_doc()
    if not current_user_doc:
        return redirect(url_for("login"))

    username = current_user_doc["username"]

    try:
        errors = apply_profile_updates(current_user_doc, include_account_fields=False)
        if errors:
            for err in errors:
                flash(err, "error" if err != "No changes were submitted." else "warning")
            return redirect(url_for("view_profile", username=username))

        flash("Profile updated successfully.", "success")
    except Exception as exc:
        logger.error("Profile update error: %s", exc)
        flash("Error updating profile.", "error")

    return redirect(url_for("view_profile", username=username))


# =============================
# Interaction Routes
# =============================
@app.route("/like_post/<post_id>", methods=["POST"])
@login_required
def like_post(post_id):
    """Like or unlike a post."""
    ensure_db()

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)})
        if not post_doc:
            if is_ajax_request():
                return jsonify({"success": False, "error": "Post not found"}), 404
            flash("Post not found.", "error")
            return redirect(url_for("home"))

        username = session["username"]
        likes = post_doc.get("likes", [])

        if username in likes:
            posts.update_one({"_id": ObjectId(post_id)}, {"$pull": {"likes": username}})
            liked = False
            likes_count = max(len(likes) - 1, 0)
            logger.info("%s unliked post %s", username, post_id)
        else:
            posts.update_one({"_id": ObjectId(post_id)}, {"$addToSet": {"likes": username}})
            add_notification(post_doc.get("username", ""), username, "like", post_id)
            liked = True
            likes_count = len(likes) + 1
            logger.info("%s liked post %s", username, post_id)

        if is_ajax_request():
            return jsonify({"success": True, "liked": liked, "likes_count": likes_count})

        return redirect(request.referrer or url_for("home"))
    except Exception as exc:
        logger.error("Like error: %s", exc)
        if is_ajax_request():
            return jsonify({"success": False, "error": "Like failed"}), 500
        flash("Error liking post.", "error")
        return redirect(request.referrer or url_for("home"))


@app.route("/save_post/<post_id>", methods=["POST"])
@login_required
def save_post(post_id):
    """Save or unsave a post for the current user."""
    ensure_db()

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)}, {"_id": 1})
        if not post_doc:
            if is_ajax_request():
                return jsonify({"success": False, "error": "Post not found"}), 404
            flash("Post not found.", "error")
            return redirect(request.referrer or url_for("home"))

        current_user_doc = ensure_user_defaults(users.find_one({"username": session["username"]}))
        if not current_user_doc:
            if is_ajax_request():
                return jsonify({"success": False, "error": "User not found"}), 404
            flash("User not found.", "error")
            return redirect(url_for("home"))

        saved_post_ids = current_user_doc.get("saved_posts", [])
        if post_id in saved_post_ids:
            users.update_one(
                {"username": session["username"]},
                {"$pull": {"saved_posts": post_id}, "$set": {"updated_at": datetime.now()}},
            )
            is_saved = False
            logger.info("%s removed post %s from saved", session["username"], post_id)
        else:
            users.update_one(
                {"username": session["username"]},
                {"$addToSet": {"saved_posts": post_id}, "$set": {"updated_at": datetime.now()}},
            )
            is_saved = True
            logger.info("%s saved post %s", session["username"], post_id)

        if is_ajax_request():
            return jsonify({"success": True, "saved": is_saved})

        flash("Post saved." if is_saved else "Post removed from saved.", "success")
        return redirect(request.referrer or url_for("home"))
    except Exception as exc:
        logger.error("Save post error: %s", exc)
        if is_ajax_request():
            return jsonify({"success": False, "error": "Save failed"}), 500
        flash("Unable to update saved posts right now.", "error")
        return redirect(request.referrer or url_for("home"))


@app.route("/comment_post/<post_id>", methods=["POST"])
@login_required
def comment_post(post_id):
    """Add a comment to a post."""
    ensure_db()

    comment_text = request.form.get("comment", "").strip()
    if not comment_text:
        if is_ajax_request():
            return jsonify({"success": False, "error": "Comment cannot be empty."}), 400
        flash("Comment cannot be empty.", "error")
        return redirect(request.referrer or url_for("home"))
    if len(comment_text) > 500:
        if is_ajax_request():
            return jsonify({"success": False, "error": "Comment must be 500 characters or less."}), 400
        flash("Comment must be 500 characters or less.", "error")
        return redirect(request.referrer or url_for("home"))

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)})
        if not post_doc:
            if is_ajax_request():
                return jsonify({"success": False, "error": "Post not found."}), 404
            flash("Post not found.", "error")
            return redirect(url_for("home"))

        comment_doc = Snap.create_comment_doc(session["username"], comment_text)
        posts.update_one({"_id": ObjectId(post_id)}, {"$push": {"comments": comment_doc}})
        add_notification(post_doc.get("username", ""), session["username"], "comment", post_id)

        logger.info("%s commented on post %s", session["username"], post_id)
        if is_ajax_request():
            return jsonify(
                {
                    "success": True,
                    "comment": {
                        "username": comment_doc.get("username", session["username"]),
                        "text": comment_doc.get("text", comment_text),
                        "created_at_label": (comment_doc.get("created_at") or datetime.now()).strftime("%d %b %Y"),
                    },
                    "comments_count": len(post_doc.get("comments", [])) + 1,
                }
            )
        flash("Comment added.", "success")
    except Exception as exc:
        logger.error("Comment error: %s", exc)
        if is_ajax_request():
            return jsonify({"success": False, "error": "Error adding comment."}), 500
        flash("Error adding comment.", "error")

    return redirect(request.referrer or url_for("home"))


@app.route("/delete_comment/<post_id>/<int:comment_index>", methods=["POST"])
@login_required
def delete_comment(post_id, comment_index):
    """Delete the current user's comment by index."""
    ensure_db()

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)})
        if not post_doc:
            flash("Post not found.", "error")
            return redirect(request.referrer or url_for("home"))

        comments = post_doc.get("comments", [])
        if comment_index < 0 or comment_index >= len(comments):
            flash("Comment not found.", "error")
            return redirect(request.referrer or url_for("home"))

        comment = comments[comment_index]
        if comment.get("username") != session["username"]:
            flash("You can only delete your own comments.", "error")
            return redirect(request.referrer or url_for("home"))

        posts.update_one({"_id": ObjectId(post_id)}, {"$unset": {f"comments.{comment_index}": 1}})
        posts.update_one({"_id": ObjectId(post_id)}, {"$pull": {"comments": None}})

        logger.info("%s deleted a comment on post %s", session["username"], post_id)
        flash("Comment deleted.", "success")
    except Exception as exc:
        logger.error("Delete comment error: %s", exc)
        flash("Error deleting comment.", "error")

    return redirect(request.referrer or url_for("home"))


# =============================
# Utility Routes
# =============================
@app.route("/post/<post_id>")
@login_required
def view_post(post_id):
    """Post detail pages are removed; send users back to the feed."""
    return redirect(url_for("home"))

@app.route("/search")
@login_required
def search():
    """Search people by username or display name."""
    ensure_db()

    query = request.args.get("q", "").strip()
    current_username = session["username"]
    results = []

    try:
        if query:
            escaped = re.escape(query)
            results = list(
                users.find(
                    {
                        "username": {"$ne": current_username},
                        "$or": [
                            {"username": {"$regex": escaped, "$options": "i"}},
                            {"name": {"$regex": escaped, "$options": "i"}},
                        ],
                    },
                    {"username": 1, "name": 1, "profile_image": 1, "gender": 1, "followers": 1},
                ).limit(30)
            )
            for user_doc in results:
                user_doc["profile_image_url"] = get_user_profile_image(user_doc)
                user_doc["followers_count"] = len(user_doc.get("followers", []))

        return render_template("feed/search.html", query=query, results=results)
    except Exception as exc:
        logger.error("Search error: %s", exc)
        flash("Search is unavailable right now.", "error")
        return render_template("feed/search.html", query=query, results=[])


# ---- Post media download ----
@app.route("/download_post/<post_id>")
@login_required
def download_post(post_id):
    """Download post media (single file or zip for multiple files)."""
    ensure_db()

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)})
    except Exception:
        post_doc = None

    if not post_doc:
        flash("Post not found.", "error")
        return redirect(request.referrer or url_for("home"))

    try:
        media_entries = [item for item in post_doc.get("images", []) if item]
        if not media_entries:
            flash("No downloadable media found for this post.", "error")
            return redirect(request.referrer or url_for("home"))

        if len(media_entries) == 1:
            media_value = media_entries[0]
            if is_gridfs_media(media_value):
                return send_gridfs_media(
                    media_value,
                    download_name=build_download_filename(post_id, f"post_{post_id}", 1, media_value),
                    as_attachment=True,
                )
            if is_db_media(media_value):
                return send_db_media(
                    media_value,
                    download_name=build_download_filename(post_id, f"post_{post_id}", 1, media_value),
                    as_attachment=True,
                )

            normalized = normalize_stored_media_path(media_value)
            source_path = os.path.normpath(os.path.join("static", normalized))
            if not source_path.startswith(os.path.normpath("static")) or not os.path.exists(source_path):
                flash("No downloadable media found for this post.", "error")
                return redirect(request.referrer or url_for("home"))
            return send_file(
                source_path,
                as_attachment=True,
                download_name=os.path.basename(source_path),
            )

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for idx, media_value in enumerate(media_entries, start=1):
                filename = build_download_filename(post_id, f"post_{post_id}", idx, media_value)
                if is_gridfs_media(media_value):
                    try:
                        grid_out = fs.get(media_value.get("file_id")) if fs is not None else None
                    except NoFile:
                        grid_out = None
                    if grid_out is not None:
                        zip_file.writestr(filename, grid_out.read())
                    continue
                if is_db_media(media_value):
                    zip_file.writestr(filename, bytes(media_value.get("data", b"")))
                    continue

                normalized = normalize_stored_media_path(media_value)
                source_path = os.path.normpath(os.path.join("static", normalized))
                if not source_path.startswith(os.path.normpath("static")) or not os.path.exists(source_path):
                    continue
                with open(source_path, "rb") as source_file:
                    zip_file.writestr(filename, source_file.read())

        zip_buffer.seek(0)
        archive_name = f"{post_doc.get('username', 'post')}_{post_id}.zip"
        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=archive_name,
        )
    except Exception as exc:
        logger.error("Download post error: %s", exc)
        flash("Unable to download this post right now.", "error")
        return redirect(url_for("home"))


# ---- Post editing ----
@app.route("/edit_post/<post_id>", methods=["POST"])
@login_required
def edit_post(post_id):
    """Edit caption for a user's own post."""
    ensure_db()

    caption = request.form.get("caption", "").strip()
    valid, message = Snap.validate_caption(caption)
    if not valid:
        flash(message, "error")
        return redirect(request.referrer or url_for("home"))

    try:
        post_doc = posts.find_one({"_id": ObjectId(post_id)})
        if not post_doc:
            flash("Post not found.", "error")
            return redirect(request.referrer or url_for("home"))

        if post_doc.get("username") != session["username"]:
            flash("You can only edit your own posts.", "error")
            return redirect(request.referrer or url_for("home"))

        posts.update_one(
            {"_id": ObjectId(post_id)},
            {"$set": {"caption": caption, "updated_at": datetime.now()}},
        )
        flash("Post caption updated.", "success")
    except Exception as exc:
        logger.error("Edit post error: %s", exc)
        flash("Unable to update post.", "error")

    return redirect(request.referrer or url_for("home"))


# ---- Notifications ----
@app.route("/notifications")
@login_required
def notifications():
    """Show notification feed."""
    ensure_db()

    try:
        current_user_doc = require_current_session_user_doc()
        if not current_user_doc:
            return redirect(url_for("login"))
        notifications_data = enrich_notifications_for_view(current_user_doc.get("notifications", []))

        return render_template("feed/notifications.html", notifications=notifications_data)
    except Exception as exc:
        logger.error("Notifications error: %s", exc)
        flash("Unable to load notifications.", "error")
        return redirect(url_for("home"))


@app.route("/notifications/live")
def notifications_live():
    """Serve recent notifications for client-side live popup polling."""
    ensure_db()

    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Authentication required."}), 401

    current_user_doc = get_current_session_user_doc()
    if not current_user_doc:
        session.clear()
        return jsonify({"success": False, "error": "Session expired."}), 401

    try:
        notifications_data = current_user_doc.get("notifications", [])
        unread_count = sum(1 for item in notifications_data if not item.get("is_read"))
        recent_notifications = enrich_notifications_for_view(
            notifications_data[: max(NOTIFICATION_POPUP_FETCH_LIMIT, 1)]
        )
        return jsonify(
            {
                "success": True,
                "unread_count": unread_count,
                "poll_interval_ms": NOTIFICATION_POLL_INTERVAL_MS,
                "notifications": [serialize_notification_item(item) for item in recent_notifications],
            }
        )
    except Exception as exc:
        logger.error("Live notifications error: %s", exc)
        return jsonify({"success": False, "error": "Unable to load notifications."}), 500


@app.route("/notifications/read_all", methods=["POST"])
@login_required
def mark_notifications_read():
    """Mark all notifications as read."""
    ensure_db()

    try:
        user_doc = require_current_session_user_doc()
        if not user_doc:
            return redirect(url_for("login"))
        notifications_data = user_doc.get("notifications", [])
        for item in notifications_data:
            item["is_read"] = True
        users.update_one(
            {"username": session["username"]},
            {"$set": {"notifications": notifications_data, "updated_at": datetime.now()}},
        )
        flash("All notifications marked as read.", "success")
    except Exception as exc:
        logger.error("Mark notifications read error: %s", exc)
        flash("Unable to mark notifications as read.", "error")
    return redirect(url_for("notifications"))


# ---- Account settings ----
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """Manage account details and password."""
    ensure_db()

    current_username = session["username"]
    user_doc = require_current_session_user_doc()
    if not user_doc:
        return redirect(url_for("login"))

    if request.method == "POST":
        action = request.form.get("action", "profile")

        if action == "profile":
            errors = apply_profile_updates(user_doc, include_account_fields=True)
            if errors:
                for err in errors:
                    flash(err, "error" if err != "No changes were submitted." else "warning")
            else:
                flash("Profile settings updated.", "success")

        if action == "password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            errors = []

            if not check_password_hash(user_doc.get("password", ""), current_password):
                errors.append("Current password is incorrect.")

            valid, message = User.validate_password(new_password)
            if not valid:
                errors.append(message)

            if new_password != confirm_password:
                errors.append("New passwords do not match.")

            if errors:
                for err in errors:
                    flash(err, "error")
            else:
                users.update_one(
                    {"username": current_username},
                    {
                        "$set": {
                            "password": generate_password_hash(new_password),
                            "updated_at": datetime.now(),
                        }
                    },
                )
                flash("Password updated successfully.", "success")

        return redirect(url_for("settings"))

    latest_user_doc = ensure_user_defaults(users.find_one({"username": current_username}))
    if not latest_user_doc:
        session.clear()
        flash("Your account was not found. Please log in again.", "warning")
        return redirect(url_for("login"))
    latest_posts = list(posts.find({"username": current_username}).sort("created_at", -1))
    insights = build_profile_insights(latest_user_doc, latest_posts)
    return render_template("settings.html", user=latest_user_doc, insights=insights)


# ---- Account deletion ----
@app.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    """Delete current account with media cleanup."""
    ensure_db()
    current_username = session["username"]
    confirm_username = request.form.get("confirm_username", "").strip()

    if confirm_username != current_username:
        flash("Type your exact username to confirm account deletion.", "error")
        return redirect(url_for("settings"))

    try:
        user_doc = ensure_user_defaults(users.find_one({"username": current_username}))
        if not user_doc:
            session.clear()
            return redirect(url_for("login"))

        user_posts = list(posts.find({"username": current_username}, {"_id": 1, "images": 1}))
        for post_doc in user_posts:
            delete_post_and_media(post_doc)

        profile_image = user_doc.get("profile_image", "")
        if profile_image and not is_default_profile_image(profile_image):
            safe_delete_static_file(profile_image)

        deleted_user_result = users.delete_one({"username": current_username})
        if deleted_user_result.deleted_count != 1:
            flash("Unable to delete account right now.", "error")
            return redirect(url_for("settings"))

        remove_user_references(current_username)
        repaired_users, repaired_posts = cleanup_social_references()
        if repaired_users or repaired_posts:
            logger.info(
                "Repaired residual references after deleting %s (users=%s, posts=%s)",
                current_username,
                repaired_users,
                repaired_posts,
            )

        session.clear()
        flash("Your account was deleted.", "success")
        return redirect(url_for("login"))
    except Exception as exc:
        logger.error("Delete account error: %s", exc)
        flash("Unable to delete account right now.", "error")
        return redirect(url_for("settings"))


# ---- Legacy compatibility ----
@app.route("/about")
def about():
    """Keep compatibility with old about URL by jumping to login section."""
    return redirect(url_for("login") + "#about-section")


@app.route("/design/ultra")
def design_ultra():
    """Show the standalone premium UI concept page."""
    return render_template("figma_ui/pyinsta_ultra.html")


# =============================
# Error Handlers
# =============================
@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(503)
def service_unavailable(_error):
    return "Database unavailable. Please check MongoDB connection.", 503


@app.errorhandler(500)
def server_error(error):
    logger.error("Server error: %s", error)
    return render_template("500.html"), 500


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
