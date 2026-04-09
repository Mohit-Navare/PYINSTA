
import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "cloud.env", override=True)

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "pyinsta_social")

client = None
db = None
users_collection = None
posts_collection = None

if not MONGO_URI:
    print("Failed to connect to MongoDB: MONGO_URI is not configured.")
else:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[DB_NAME]
        users_collection = db["users"]
        posts_collection = db["posts"]
        print("MongoDB connected successfully")
    except PyMongoError as e:
        print(f"Failed to connect to MongoDB: {e}")
        client = None
        db = None
        users_collection = None
        posts_collection = None
