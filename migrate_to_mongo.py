import os
import json
import asyncio
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Loyiha ota-papkasida yoki shu joydagi .env dan o'qish
if os.path.exists(".env"):
    load_dotenv(".env")
elif os.path.exists("../.env"):
    load_dotenv("../.env")

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = "light_bot_db"

async def migrate_data():
    if not MONGO_URI:
        print("❌ MONGO_URI topilmadi! Iltimos, .env faylida MONGO_URI ni kiriting.")
        return

    # Mahalliy data.json yo'q bo'lsa, ota-papkadagini olish
    data_file = "data.json"
    if not os.path.exists(data_file):
        data_file = "../data.json"
        
    if not os.path.exists(data_file):
        print("❌ data.json topilmadi. Migratsiya qilishga hech narsa yo'q.")
        return

    print(f"📦 Ma'lumotlar '{data_file}' faylidan o'qilmoqda...")
    try:
        with open(data_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ data.json ni o'qishda xatolik: {e}")
        return

    if not data:
        print("ℹ️ Ma'lumotlar bazasi bo'sh. Migratsiya yakunlandi.")
        return

    print(f"🔄 MongoDB ga ulanilmoqda...")
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client[DB_NAME]
        users_collection = db["users"]

        # Hujjatlarni yig'ish (user_id ni _id sifatida yozamiz)
        documents = []
        for user_id_str, state in data.items():
            user_doc = {"_id": int(user_id_str)}
            user_doc.update(state)
            documents.append(user_doc)

        print(f"🚀 {len(documents)} ta foydalanuvchi ma'lumoti MongoDB ga ko'chirilmoqda...")
        
        # MongoDB ga joylash (agar allaqachon mavjud bo'lsa, yangilash)
        inserted_count = 0
        updated_count = 0
        for doc in documents:
            result = await users_collection.replace_one(
                {"_id": doc["_id"]}, 
                doc, 
                upsert=True
            )
            if result.upserted_id:
                inserted_count += 1
            else:
                updated_count += 1
                
        print("✅ Migratsiya muvaffaqiyatli yakunlandi!")
        print(f"Yangi qo'shildi: {inserted_count} ta")
        print(f"Yangilandi: {updated_count} ta")
        
    except Exception as e:
        print(f"❌ MongoDB ga yozishda xatolik: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_data())
