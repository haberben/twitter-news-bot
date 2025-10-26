#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitter Haber Botu - Telegram Kontrollü (SORUNSUZ VERSİYON)
Türk haber kaynaklarını takip eder, AI ile analiz eder
"""

import json
import time
import random
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import asyncio

try:
    import requests
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
    import google.generativeai as genai
    from bs4 import BeautifulSoup
    import re
except ImportError as e:
    print(f"Eksik kütüphane: {e}")
    print("Lütfen: pip install --user -r requirements.txt")
    exit(1)

# ========================
# YAPILANDIRMA
# ========================

TELEGRAM_BOT_TOKEN = "8330949618:AAEIq-vkKaTmCJm69rnWrvpf4zeN4ygvcI8"
ADMIN_USER_ID = 7336102260
GEMINI_API_KEY = "AIzaSyDskFEZSVR751FRCmLpVpeoHG1wRJJYIYM"

# Ayarlar
CHECK_INTERVAL_MIN = 15
CHECK_INTERVAL_MAX = 45
SIMILARITY_THRESHOLD = 0.75
MIN_SOURCE_COUNT = 3
DB_PATH = "news_bot.db"

# ========================
# HABER KAYNAKLARI
# ========================

NEWS_SOURCES = {
    "priority_high": [
        {"name": "NTV", "twitter": "@ntvcomtr", "rss": "https://www.ntv.com.tr/gundem.rss", "priority": 1},
        {"name": "Sözcü", "twitter": "@sozcugazetesi", "rss": "https://www.sozcu.com.tr/kategori/gundem/feed/", "priority": 1},
        {"name": "Hürriyet", "twitter": "@Hurriyet", "rss": "https://www.hurriyet.com.tr/rss/gundem", "priority": 1},
        {"name": "CNN Türk", "twitter": "@cnnturk", "rss": "https://www.cnnturk.com/feed/rss/news", "priority": 1},
        {"name": "Habertürk", "twitter": "@haberturk", "rss": "https://www.haberturk.com/rss", "priority": 1},
    ],
    "priority_medium": [
        {"name": "Milliyet", "twitter": "@milliyetcomtr", "rss": "https://www.milliyet.com.tr/rss/rssnew/gundemrss.xml", "priority": 2},
        {"name": "Sabah", "twitter": "@sabah", "rss": "https://www.sabah.com.tr/rss/gundem.xml", "priority": 2},
        {"name": "A Haber", "twitter": "@Ahaber", "rss": "https://www.ahaber.com.tr/rss/gundem.xml", "priority": 2},
    ],
    "priority_low": []
}

# Viral saatler
VIRAL_HOURS = {
    "prime": [(8, 10), (12, 14), (18, 21), (21, 23)],
    "good": [(10, 12), (14, 17)],
    "bad": [(1, 6)]
}

# ========================
# VERİTABANI
# ========================

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                url TEXT,
                source TEXT NOT NULL,
                image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hash TEXT UNIQUE
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shared_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_text TEXT NOT NULL,
                shared_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
    
    def add_news(self, title: str, content: str, url: str, source: str, image_url: str = None):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            import hashlib
            news_hash = hashlib.md5(f"{title}{source}".encode()).hexdigest()
            
            cursor.execute("""
                INSERT OR IGNORE INTO news 
                (title, content, url, source, image_url, hash)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, content, url, source, image_url, news_hash))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"DB hata: {e}")
            return False
    
    def get_recent_news(self, hours: int = 2):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        time_limit = datetime.now() - timedelta(hours=hours)
        
        cursor.execute("""
            SELECT id, title, content, url, source, image_url
            FROM news
            WHERE created_at > ?
            ORDER BY created_at DESC
        """, (time_limit,))
        
        news = []
        for row in cursor.fetchall():
            news.append({
                "id": row[0],
                "title": row[1],
                "content": row[2],
                "url": row[3],
                "source": row[4],
                "image_url": row[5]
            })
        
        conn.close()
        return news

# ========================
# HABER TOPLAMA
# ========================

class NewsCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def collect_from_rss(self, rss_url: str, source_name: str):
        try:
            response = self.session.get(rss_url, timeout=10)
            soup = BeautifulSoup(response.content, 'xml')
            items = soup.find_all('item')
            
            news_list = []
            for item in items[:5]:
                try:
                    title = item.find('title').text.strip()
                    link = item.find('link').text.strip()
                    description = item.find('description').text.strip() if item.find('description') else ""
                    
                    image_url = None
                    if item.find('enclosure'):
                        image_url = item.find('enclosure').get('url')
                    
                    news_list.append({
                        'title': title,
                        'content': description,
                        'url': link,
                        'source': source_name,
                        'image_url': image_url
                    })
                except:
                    continue
            
            return news_list
        except Exception as e:
            print(f"RSS hatası ({source_name}): {e}")
            return []
    
    def collect_all(self):
        all_news = []
        
        for source in NEWS_SOURCES['priority_high']:
            if 'rss' in source:
                news = self.collect_from_rss(source['rss'], source['name'])
                all_news.extend(news)
                time.sleep(random.uniform(1, 3))
        
        for source in NEWS_SOURCES['priority_medium']:
            if 'rss' in source:
                news = self.collect_from_rss(source['rss'], source['name'])
                all_news.extend(news)
                time.sleep(random.uniform(1, 3))
        
        return all_news

# ========================
# AI ANALİZ (GEMINI)
# ========================

class NewsAnalyzer:
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
    
    def find_similar_news(self, news_list: List[Dict]):
        if len(news_list) < 2:
            return []
        
        prompt = self._create_prompt(news_list)
        
        try:
            response = self.model.generate_content(prompt)
            groups = self._parse_response(response.text, news_list)
            return groups
        except Exception as e:
            print(f"Gemini hatası: {e}")
            return []
    
    def _create_prompt(self, news_list: List[Dict]):
        news_text = ""
        for i, news in enumerate(news_list, 1):
            news_text += f"{i}. {news['title']} (Kaynak: {news['source']})\n"
        
        prompt = f"""Aşağıdaki Türkçe haber başlıklarını analiz et ve AYNI OLAYI anlatan haberleri grupla.

HABERLER:
{news_text}

GÖREV:
1. Aynı olayı anlatan haberleri bul
2. Her grup için en az 3 farklı kaynak olmalı
3. Özgün bir tweet metni yaz (max 240 karakter, Türkçe, anlaşılır)

ÇIKTI FORMATI (JSON):
{{
  "groups": [
    {{
      "topic": "Olay özeti",
      "news_ids": [1, 4, 7],
      "sources": ["NTV", "Sözcü", "Hürriyet"],
      "tweet": "Özgün tweet metni buraya"
    }}
  ]
}}

Sadece JSON çıktı ver."""
        
        return prompt
    
    def _parse_response(self, response: str, news_list: List[Dict]):
        try:
            json_match = re.search(r'\{[\s\S]*\}', response)
            if not json_match:
                return []
            
            data = json.loads(json_match.group())
            groups = []
            
            for group in data.get('groups', []):
                if len(group.get('sources', [])) >= MIN_SOURCE_COUNT:
                    related_news = []
                    for news_id in group.get('news_ids', []):
                        if 0 < news_id <= len(news_list):
                            related_news.append(news_list[news_id - 1])
                    
                    best_image = self._get_best_image(related_news)
                    
                    groups.append({
                        'topic': group.get('topic', ''),
                        'tweet': group.get('tweet', ''),
                        'sources': group.get('sources', []),
                        'source_count': len(group.get('sources', [])),
                        'related_news': related_news,
                        'best_image': best_image
                    })
            
            return groups
        except Exception as e:
            print(f"Parse hatası: {e}")
            return []
    
    def _get_best_image(self, news_list: List[Dict]):
        for news in news_list:
            if news.get('image_url'):
                return news['image_url']
        return None

# ========================
# VİRAL SAAT
# ========================

def get_viral_info():
    now = datetime.now()
    hour = now.hour
    
    for start, end in VIRAL_HOURS['prime']:
        if start <= hour < end:
            return {
                'status': 'prime',
                'label': '🔥 PRİME TIME',
                'multiplier': 2.0,
                'recommendation': '✅ ŞIMDI PAYLAŞ! Viral olma şansı yüksek!'
            }
    
    for start, end in VIRAL_HOURS['good']:
        if start <= hour < end:
            return {
                'status': 'good',
                'label': '👍 İyi Zaman',
                'multiplier': 1.0,
                'recommendation': 'Paylaşabilirsin'
            }
    
    return {
        'status': 'bad',
        'label': '❌ Kötü Zaman',
        'multiplier': 0.3,
        'recommendation': '⏰ Prime time\'a ertele (08:00-21:00)'
    }

# ========================
# TELEGRAM BOT
# ========================

class TelegramBot:
    def __init__(self):
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self.db = Database(DB_PATH)
        self.collector = NewsCollector()
        self.analyzer = NewsAnalyzer(GEMINI_API_KEY)
        self.setup_handlers()
    
    def setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("test", self.test_command))
        self.app.add_handler(CommandHandler("sources", self.sources_command))
        self.app.add_handler(CommandHandler("viral", self.viral_command))
        self.app.add_handler(CommandHandler("addsource", self.addsource_command))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = f"""
🤖 **Twitter Haber Botu Aktif!**

✅ Sistem hazır ve çalışıyor!
📱 User ID: `{update.effective_user.id}`

**📋 KOMUTLAR:**

🔍 /test - Hemen test et!
📰 /sources - Kaynakları listele
⚡ /addsource - Yeni kaynak ekle
🕐 /viral - Viral saat bilgisi

**🚀 Başlamak için /test yaz!**
        """
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔥 **TEST BAŞLATILDI!**\n\n⏳ Haberler toplanıyor...")
        
        news_list = self.collector.collect_all()
        
        if not news_list:
            await update.message.reply_text("❌ Haber bulunamadı")
            return
        
        for news in news_list:
            self.db.add_news(
                title=news['title'],
                content=news['content'],
                url=news['url'],
                source=news['source'],
                image_url=news['image_url']
            )
        
        await update.message.reply_text(f"✅ {len(news_list)} haber toplandı!\n\n🤖 AI analizi yapılıyor...")
        
        groups = self.analyzer.find_similar_news(news_list)
        
        if not groups:
            await update.message.reply_text("📊 Benzer haber bulunamadı\n(Min 3 kaynak gerekli)")
            return
        
        await update.message.reply_text(f"🎉 {len(groups)} haber grubu hazır!")
        
        for i, group in enumerate(groups, 1):
            await self.send_news_group(update, group, i)
            await asyncio.sleep(1)
    
    async def send_news_group(self, update: Update, group: Dict, index: int):
        viral_info = get_viral_info()
        
        tweet_text = group['tweet']
        hashtags = "#SonDakika #Türkiye"
        
        message = f"""
📰 **Haber #{index}**

**Konu:** {group['topic']}
**Kaynak:** {group['source_count']} ({', '.join(group['sources'][:3])})

📝 **HAZIR TWEET:**
```
{tweet_text}

{hashtags}
```

🕐 **VİRAL SAAT:**
{viral_info['label']} (x{viral_info['multiplier']})
{viral_info['recommendation']}

🖼️ **Görsel:** {"✅ Var" if group['best_image'] else "❌ Yok"}
        """
        
        if group['best_image']:
            try:
                await update.effective_chat.send_photo(
                    photo=group['best_image'],
                    caption=message,
                    parse_mode='Markdown'
                )
            except:
                await update.message.reply_text(message, parse_mode='Markdown')
        else:
            await update.message.reply_text(message, parse_mode='Markdown')
    
    async def sources_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = "📰 **HABER KAYNAKLARI**\n\n"
        
        message += "🔴 **Yüksek Öncelikli:**\n"
        for source in NEWS_SOURCES['priority_high']:
            message += f"• {source['name']} ({source['twitter']})\n"
        
        message += "\n🟡 **Orta Öncelikli:**\n"
        for source in NEWS_SOURCES['priority_medium']:
            message += f"• {source['name']} ({source['twitter']})\n"
        
        total = len(NEWS_SOURCES['priority_high']) + len(NEWS_SOURCES['priority_medium'])
        message += f"\n**Toplam: {total} kaynak**"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def viral_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        viral_info = get_viral_info()
        now = datetime.now().strftime('%H:%M')
        
        message = f"""
🕐 **VİRAL SAAT ANALİZİ**

**Şu An:** {now}
{viral_info['label']}

**Durum:** {viral_info['recommendation']}
**Viral Çarpan:** x{viral_info['multiplier']}

**Prime Time Saatleri:**
🌅 08:00-10:00 (Sabah)
☀️ 12:00-14:00 (Öğle)
🔥 18:00-21:00 (Akşam - EN İYİ)
🌙 21:00-23:00 (Gece)
        """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def addsource_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = """
📰 **YENİ KAYNAK EKLEME**

**Format:**
`/addsource İsim @twitter https://rss 1`

**Örnek:**
`/addsource Örnek @ornektr https://ornek.com/rss 1`

**Öncelik:** 1=Yüksek, 2=Orta
        """
        
        if len(context.args) < 4:
            await update.message.reply_text(message, parse_mode='Markdown')
            return
        
        name = context.args[0]
        twitter = context.args[1]
        rss = context.args[2]
        priority = int(context.args[3])
        
        new_source = {
            "name": name,
            "twitter": twitter,
            "rss": rss,
            "priority": priority
        }
        
        if priority == 1:
            NEWS_SOURCES['priority_high'].append(new_source)
        else:
            NEWS_SOURCES['priority_medium'].append(new_source)
        
        await update.message.reply_text(f"✅ Kaynak eklendi: {name}")
    
    def run(self):
        print("""
╔══════════════════════════════════════╗
║  Twitter Haber Botu - Telegram v1.0  ║
║  Türk Haber Kaynakları Analizi       ║
╚══════════════════════════════════════╝
        """)
        print("🤖 Telegram bot başlatılıyor...")
        print("✅ Bot aktif!")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

# ========================
# ANA PROGRAM
# ========================

if __name__ == "__main__":
    try:
        bot = TelegramBot()
        bot.run()
    except KeyboardInterrupt:
        print("\n👋 Bot kapatılıyor...")
    except Exception as e:
        print(f"❌ Hata: {e}")
