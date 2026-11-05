import re
import json
import time
import asyncio
import os
import aiohttp
import csv
import io
import sqlite3
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest
from faker import Faker

# ==================== CONFIG ====================
TOKEN = "8694892966:AAFdzP0IvszunQQAq_qmTSc2PvaO5RtTQ7o"
ADMIN_ID = 8715003522
BOT_USERNAME = 'DazaiappBot'
MESSAGE_PREFIX = '/au'

API_ID = 29612794
API_HASH = '6edc1a58a202c9f6e62dc98466932bad'
TIMEOUT_SECONDS = 40

DATA_DIR = "/tmp" if os.path.exists("/tmp") else "."
DB_PATH = os.path.join(DATA_DIR, "bot_data.db")
SESSION_DIR = os.path.join(DATA_DIR, "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

# ==================== DATABASE ====================
conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
c = conn.cursor()
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA synchronous=NORMAL')

c.execute('''CREATE TABLE IF NOT EXISTS sessions
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_name TEXT UNIQUE,
              phone_number TEXT,
              created_at TIMESTAMP,
              is_active BOOLEAN DEFAULT 1)''')
c.execute('''CREATE TABLE IF NOT EXISTS banned_users
             (user_id INTEGER PRIMARY KEY,
              banned_until TIMESTAMP,
              reason TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS user_stats
             (user_id INTEGER PRIMARY KEY,
              first_name TEXT,
              username TEXT,
              total_checks INTEGER DEFAULT 0,
              last_check TIMESTAMP,
              checks_today INTEGER DEFAULT 0,
              last_reset TIMESTAMP,
              join_date TIMESTAMP)''')
c.execute('''CREATE TABLE IF NOT EXISTS bot_config
             (key TEXT PRIMARY KEY,
              value TEXT)''')
conn.commit()
c.execute("INSERT OR IGNORE INTO bot_config (key, value) VALUES ('check_delay', '2')")
conn.commit()

fake = Faker()

# ==================== HELPERS ====================
def is_admin(uid):
    return uid == ADMIN_ID

def is_banned(uid):
    try:
        c.execute("SELECT banned_until, reason FROM banned_users WHERE user_id = ?", (uid,))
        r = c.fetchone()
        if r:
            until = datetime.fromisoformat(r[0])
            if until > datetime.now():
                return True, until, r[1]
            c.execute("DELETE FROM banned_users WHERE user_id = ?", (uid,))
            conn.commit()
    except: pass
    return False, None, None

def register_user(user):
    try:
        c.execute("SELECT user_id FROM user_stats WHERE user_id = ?", (user.id,))
        if not c.fetchone():
            c.execute("INSERT INTO user_stats (user_id, first_name, username, total_checks, join_date) VALUES (?,?,?,?,?)",
                      (user.id, user.first_name, user.username, 0, datetime.now().isoformat()))
            conn.commit()
    except: pass

def update_stats(user):
    try:
        today = datetime.now().date()
        c.execute("SELECT last_reset, checks_today FROM user_stats WHERE user_id = ?", (user.id,))
        r = c.fetchone()
        if r and r[0]:
            last = datetime.fromisoformat(r[0]).date()
            checks = 1 if last != today else (r[1] or 0) + 1
        else:
            checks = 1
        c.execute("UPDATE user_stats SET total_checks=total_checks+1, last_check=?, checks_today=?, last_reset=?, first_name=?, username=? WHERE user_id=?",
                  (datetime.now().isoformat(), checks, datetime.now().isoformat(), user.first_name, user.username, user.id))
        conn.commit()
        return checks
    except: return 0

def get_delay():
    try:
        c.execute("SELECT value FROM bot_config WHERE key='check_delay'")
        r = c.fetchone()
        return int(r[0]) if r else 2
    except: return 2

def set_delay(sec):
    try:
        c.execute("UPDATE bot_config SET value=? WHERE key='check_delay'", (str(sec),))
        conn.commit()
    except: pass

# Card parsing — accept any format
def validate_and_parse_card(text):
    text = text.strip().replace(' ', '')
    if '|' in text or ':' in text:
        sep = '|' if '|' in text else ':'
        parts = text.split(sep)
        if len(parts) >= 4:
            num, mm, yy, cvv = parts[0].strip(), parts[1].strip().zfill(2), parts[2].strip(), parts[3].strip()
            if len(yy) == 4: yy = yy[-2:]
            yy = yy.zfill(2)
            try:
                if 1 <= int(mm) <= 12 and re.match(r'^\d{15,16}$', num) and re.match(r'^\d{2}$', mm) and re.match(r'^\d{2}$', yy) and re.match(r'^\d{3,4}$', cvv):
                    return True, f"{num}|{mm}|{yy}|{cvv}"
            except: pass
    m = re.search(r'^(\d{15,16})(\d{2})(\d{2,4})(\d{3,4})$', text)
    if m:
        num, mm, yy, cvv = m.group(1), m.group(2).zfill(2), m.group(3), m.group(4)
        if len(yy) == 4: yy = yy[-2:]
        yy = yy.zfill(2)
        try:
            if 1 <= int(mm) <= 12:
                return True, f"{num}|{mm}|{yy}|{cvv}"
        except: pass
    m = re.search(r'(\d{15,16})\D*(\d{2})\D*(\d{2,4})\D*(\d{3,4})', text)
    if m:
        num, mm, yy, cvv = m.group(1), m.group(2).zfill(2), m.group(3), m.group(4)
        if len(yy) == 4: yy = yy[-2:]
        yy = yy.zfill(2)
        try:
            if 1 <= int(mm) <= 12:
                return True, f"{num}|{mm}|{yy}|{cvv}"
        except: pass
    return False, None

# Parser for Dazai format
def _clean(s):
    return re.sub(r'[^\x00-\x7F]+', '', s).strip()

def parse_bot_reply(text):
    def g(field):
        m = re.search(rf'{field}:\s*(.*?)(?:\n|$)', text)
        return m.group(1).strip() if m else ""
    
    bank_raw = g("Bank")
    bank_name, card_type = bank_raw, ""
    if " - " in bank_raw:
        bank_name, card_type = bank_raw.rsplit(" - ", 1)
        bank_name, card_type = bank_name.strip(), card_type.strip()
    
    country_raw = g("Country")
    flag_match = re.search(r'([\U0001F1E6-\U0001F1FF]{2})', country_raw)
    country_clean = _clean(country_raw).strip(" -")
    
    return {
        "cc": g("CC"),
        "status": _clean(g("Status")),
        "response": _clean(g("Message")),
        "gateway": _clean(g("Gateway")),
        "bank": _clean(bank_name),
        "type": _clean(card_type),
        "country": country_clean,
        "flag": flag_match.group(1) if flag_match else "",
        "time_taken": _clean(g("Time Taken")),
        "used_by": _clean(g("Used By")),
    }

def is_final_response(text):
    if 'CC:' not in text and 'Status:' not in text:
        return False
    status_m = re.search(r'Status:\s*(.*?)(?:\n|$)', text)
    msg_m = re.search(r'(?:Message|Response):\s*(.*?)(?:\n|$)', text)
    status = status_m.group(1).strip().lower() if status_m else ""
    msg = msg_m.group(1).strip().lower() if msg_m else ""
    if any(k in status for k in ('process', 'checking', 'pending', 'wait', '⏳')):
        return False
    return bool(status and msg)

# ==================== ASYNC ====================
async def get_bin_info(bin_num):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://lookup.binlist.net/{bin_num}", timeout=10) as r:
                if r.status == 200:
                    d = await r.json()
                    return {
                        "bin": bin_num,
                        "bank": d.get('bank', {}).get('name', 'Unknown'),
                        "brand": d.get('scheme', 'Unknown').upper(),
                        "category": 'Prepaid' if d.get('prepaid') else 'Standard',
                        "type": d.get('type', 'Unknown').capitalize(),
                        "country": d.get('country', {}).get('name', 'Unknown')
                    }
                return {"error": "BIN not found"}
    except Exception as e:
        return {"error": str(e)}

async def send_card_via_session(card_data, session_name):
    path = os.path.join(SESSION_DIR, session_name)
    client = None
    try:
        client = TelegramClient(path, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {"error": "Session not authorized"}
        entity = await client.get_entity(f'@{BOT_USERNAME}')
        await client.send_message(entity, f"{MESSAGE_PREFIX} {card_data}")
        start = time.time()
        last_text = ""
        processing_seen = False
        while time.time() - start < TIMEOUT_SECONDS:
            await asyncio.sleep(2)
            try:
                hist = await client(GetHistoryRequest(peer=entity, limit=1, offset_date=None, offset_id=0, max_id=0, min_id=0, add_offset=0, hash=0))
            except:
                continue
            if hist.messages:
                txt = hist.messages[0].message
                if txt != last_text:
                    last_text = txt
                    if is_final_response(txt):
                        return parse_bot_reply(txt)
                    else:
                        processing_seen = True
        if processing_seen:
            await asyncio.sleep(5)
            try:
                hist = await client(GetHistoryRequest(peer=entity, limit=1, offset_date=None, offset_id=0, max_id=0, min_id=0, add_offset=0, hash=0))
                if hist.messages:
                    txt = hist.messages[0].message
                    if is_final_response(txt):
                        return parse_bot_reply(txt)
            except: pass
        return {"error": "timeout", "status": "Error", "response": "No response from gateway"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if client:
            try: await client.disconnect()
            except: pass

async def verify_session_file(path):
    try:
        client = TelegramClient(path, API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            return True, me.phone or "Unknown"
        await client.disconnect()
        return False, "Not authorized"
    except Exception as e:
        return False, str(e)

# ==================== START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    if is_admin(user.id):
        text = (
            "<b>Hey Admin, welcome</b>\n\n"
            "<b>Gates</b>\n"
            "<code>/au</code> <b>Stripe Auth</b>\n"
            "<b>Adding more soon...!</b>\n\n"
            "<b>Tools</b>\n"
            "<code>/bin</code> <b>Bin Info</b>\n"
            "<code>/fake</code> <b>Fake Address Generator</b>\n\n"
            "<b>Usages</b>\n"
            "<code>/au 4111111111111111|12|2025|123</code>\n"
            "<code>/bin 411111</code>\n"
            "<code>/fake US</code>\n\n"
            "<b>Admin Commands</b>\n"
            "<code>/stats</code> <b>All user stats + hosting</b>\n"
            "<code>/getuserdata</code> <b>Export all users</b>\n"
            "<code>/ban</code> <b>Ban user</b>\n"
            "<code>/unban</code> <b>Unban user</b>\n"
            "<code>/broadcast</code> <b>Broadcast msg</b>\n"
            "<code>/seslog</code> <b>Reply to session file</b>\n"
            "<code>/sescount</code> <b>Active session count</b>"
        )
    else:
        text = (
            "<b>Welcome to @SoenxBot</b>\n\n"
            "<b>Gates</b>\n"
            "<code>/au</code> <b>Stripe Auth</b>\n"
            "<b>Adding more soon...!</b>\n\n"
            "<b>Tools</b>\n"
            "<code>/bin</code> <b>Bin Info</b>\n"
            "<code>/fake</code> <b>Fake Address Generator</b>\n\n"
            "<b>Usages</b>\n"
            "<code>/au 4111111111111111|12|2025|123</code>\n"
            "<code>/bin 411111</code>\n"
            "<code>/fake US</code>"
        )
    await update.message.reply_text(text, parse_mode="HTML")

# ==================== PUBLIC COMMANDS ====================
async def au_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    text = update.message.reply_to_message.text if update.message.reply_to_message else (' '.join(context.args) if context.args else None)
    if not text:
        await update.message.reply_text("<b>Usage:</b> <code>/au cc|mm|yy|cvv</code>", parse_mode="HTML")
        return
    ok, card = validate_and_parse_card(text)
    if not ok:
        await update.message.reply_text("<b>Invalid card format.</b>", parse_mode="HTML")
        return
    banned, until, reason = is_banned(user.id)
    if banned:
        await update.message.reply_text(f"<b>Banned until:</b> {until}", parse_mode="HTML")
        return
    if 'last_check' in context.user_data and time.time() - context.user_data['last_check'] < get_delay():
        await update.message.reply_text("<b>Wait a bit.</b>", parse_mode="HTML")
        return
    context.user_data['last_check'] = time.time()
    msg = await update.message.reply_text("<b>Processing...</b>", parse_mode="HTML")
    c.execute("SELECT session_name FROM sessions WHERE is_active=1 ORDER BY id DESC LIMIT 1")
    r = c.fetchone()
    if not r:
        await msg.edit_text("<b>No active session.</b>", parse_mode="HTML")
        return
    result = await send_card_via_session(card, r[0])
    if "error" in result:
        await msg.edit_text(f"<b>Error:</b> {result.get('error')}", parse_mode="HTML")
        return
    formatted = (
        f"<b>Card:</b> <code>{result.get('cc', card)}</code>\n"
        f"<b>Status:</b> {result.get('status')}\n"
        f"<b>Response:</b> {result.get('response')}\n"
        f"<b>Gateway:</b> {result.get('gateway')}\n"
        f"<b>Bank:</b> {result.get('bank')}\n"
        f"<b>Type:</b> {result.get('type')}\n"
        f"<b>Country:</b> {result.get('country')} {result.get('flag', '')}\n"
        f"<b>Time Taken:</b> {result.get('time_taken', 'N/A')}\n"
        f"<b>Checked by:</b> <a href='tg://user?id={user.id}'>{user.first_name}</a>"
    )
    await msg.edit_text(formatted, parse_mode="HTML")
    update_stats(user)

async def bin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.reply_to_message.text if update.message.reply_to_message else (' '.join(context.args) if context.args else None)
    if not text:
        await update.message.reply_text("<b>Usage:</b> <code>/bin 411111</code>", parse_mode="HTML")
        return
    m = re.search(r'(\d{6})', text)
    if not m:
        await update.message.reply_text("<b>Invalid BIN.</b>", parse_mode="HTML")
        return
    msg = await update.message.reply_text("<b>Fetching BIN info...</b>", parse_mode="HTML")
    info = await get_bin_info(m.group(1))
    if "error" in info:
        await msg.edit_text(f"<b>Error:</b> {info['error']}", parse_mode="HTML")
        return
    await msg.edit_text(
        f"<b>BIN:</b> <code>{info['bin']}</code>\n"
        f"<b>Bank:</b> {info['bank']}\n"
        f"<b>Brand:</b> {info['brand']}\n"
        f"<b>Category:</b> {info['category']}\n"
        f"<b>Type:</b> {info['type']}\n"
        f"<b>Country:</b> {info['country']}",
        parse_mode="HTML"
    )

async def fake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country = context.args[0].upper() if context.args else "US"
    try:
        if country in ('US', 'USA', 'UNITED STATES'):
            addr = {
                "Street 1": fake.street_address(),
                "Street 2": fake.secondary_address(),
                "City": fake.city(),
                "State": fake.state(),
                "ZIP": fake.zipcode(),
                "Country": "United States",
                "Phone": fake.phone_number(),
            }
        elif country in ('UK', 'GB', 'UNITED KINGDOM'):
            addr = {
                "Street 1": fake.street_address(),
                "Street 2": fake.secondary_address(),
                "City": fake.city(),
                "Postcode": fake.postcode(),
                "Country": "United Kingdom",
                "Phone": fake.phone_number(),
            }
        elif country in ('IN', 'INDIA'):
            addr = {
                "Street 1": fake.street_address(),
                "Street 2": fake.secondary_address(),
                "City": fake.city(),
                "State": fake.state(),
                "PIN": fake.postcode(),
                "Country": "India",
                "Phone": fake.phone_number(),
            }
        elif country in ('CA', 'CANADA'):
            addr = {
                "Street 1": fake.street_address(),
                "Street 2": fake.secondary_address(),
                "City": fake.city(),
                "Province": fake.province(),
                "Postal Code": fake.postcode(),
                "Country": "Canada",
                "Phone": fake.phone_number(),
            }
        else:
            addr = {
                "Street 1": fake.street_address(),
                "Street 2": fake.secondary_address(),
                "City": fake.city(),
                "Country": country,
                "Postal Code": fake.postcode(),
                "Phone": fake.phone_number(),
            }
        lines = [f"<b>{k}:</b> <code>{v}</code>" for k, v in addr.items()]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"<b>Error:</b> {e}", parse_mode="HTML")

# ==================== ADMIN COMMANDS ====================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    c.execute("SELECT COUNT(*), SUM(total_checks) FROM user_stats")
    total_users, total_checks = c.fetchone()
    c.execute("SELECT COUNT(*) FROM sessions WHERE is_active=1")
    active_sessions = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM banned_users WHERE banned_until > ?", (datetime.now().isoformat(),))
    banned_count = c.fetchone()[0]
    import psutil
    try:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        cpu = psutil.cpu_percent(interval=0.5)
        host_info = (
            f"<b>CPU:</b> {cpu}%\n"
            f"<b>RAM:</b> {mem.used // (1024**2)} MB / {mem.total // (1024**2)} MB ({mem.percent}%)\n"
            f"<b>Disk:</b> {disk.used // (1024**3)} GB / {disk.total // (1024**3)} GB ({disk.percent}%)"
        )
    except:
        host_info = "<b>Host stats unavailable</b>"
    await update.message.reply_text(
        f"<b>📊 Bot Stats</b>\n\n"
        f"<b>Total Users:</b> {total_users or 0}\n"
        f"<b>Total Checks:</b> {total_checks or 0}\n"
        f"<b>Active Sessions:</b> {active_sessions}\n"
        f"<b>Banned Users:</b> {banned_count}\n"
        f"<b>Delay:</b> {get_delay()}s\n\n"
        f"<b>🖥 Hosting</b>\n{host_info}",
        parse_mode="HTML"
    )

async def getuserdata_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    c.execute("SELECT user_id, first_name, username, total_checks, last_check, checks_today, join_date FROM user_stats")
    rows = c.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['User ID', 'First Name', 'Username', 'Total Checks', 'Last Check', 'Checks Today', 'Join Date'])
    writer.writerows(rows)
    data = output.getvalue().encode()
    await update.message.reply_document(document=io.BytesIO(data), filename=f"users_{datetime.now().strftime('%Y%m%d')}.csv", caption="<b>All Users Data</b>", parse_mode="HTML")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("<b>Usage:</b> <code>/ban user_id</code>", parse_mode="HTML")
        return
    try:
        uid = int(context.args[0])
        until = datetime.now() + timedelta(days=36500)
        c.execute("INSERT OR REPLACE INTO banned_users (user_id, banned_until, reason) VALUES (?,?,?)", (uid, until.isoformat(), "Banned by admin"))
        conn.commit()
        await update.message.reply_text(f"<b>User {uid} permanently banned.</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("<b>Invalid user ID.</b>", parse_mode="HTML")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("<b>Usage:</b> <code>/unban user_id</code>", parse_mode="HTML")
        return
    try:
        uid = int(context.args[0])
        c.execute("DELETE FROM banned_users WHERE user_id = ?", (uid,))
        conn.commit()
        await update.message.reply_text(f"<b>User {uid} unbanned.</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("<b>Invalid user ID.</b>", parse_mode="HTML")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if update.message.reply_to_message:
        msg = update.message.reply_to_message.text
    elif context.args:
        msg = ' '.join(context.args)
    else:
        await update.message.reply_text("<b>Usage:</b> <code>/broadcast msg</code>", parse_mode="HTML")
        return
    c.execute("SELECT DISTINCT user_id FROM user_stats")
    users = c.fetchall()
    ok, fail = 0, 0
    status = await update.message.reply_text(f"<b>Sending to {len(users)} users...</b>", parse_mode="HTML")
    for u in users:
        try:
            await context.bot.send_message(u[0], msg)
            ok += 1
        except:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"<b>Broadcast done!</b>\nSent: {ok}\nFailed: {fail}", parse_mode="HTML")

async def seslog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("<b>Reply to a .session file.</b>", parse_mode="HTML")
        return
    doc = update.message.reply_to_message.document
    if not doc.file_name.endswith('.session'):
        await update.message.reply_text("<b>Not a .session file.</b>", parse_mode="HTML")
        return
    c.execute("SELECT session_name, phone_number FROM sessions WHERE is_active=1")
    existing = c.fetchall()
    file = await context.bot.get_file(doc.file_id)
    new_name = f"session_{int(time.time())}.session"
    path = os.path.join(SESSION_DIR, new_name)
    await file.download_to_drive(path)
    ok, info = await verify_session_file(path)
    if not ok:
        os.remove(path)
        await update.message.reply_text(f"<b>Invalid session:</b> {info}", parse_mode="HTML")
        return
    for sname, phone in existing:
        if phone == info:
            os.remove(path)
            await update.message.reply_text(f"<b>Session already exists</b> ({info}).", parse_mode="HTML")
            return
    c.execute("INSERT INTO sessions (session_name, phone_number, created_at, is_active) VALUES (?,?,?,?)", (new_name, info, datetime.now().isoformat(), 1))
    conn.commit()
    await update.message.reply_text(f"<b>Session added!</b>\nPhone: {info}", parse_mode="HTML")

async def sescount_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    c.execute("SELECT COUNT(*) FROM sessions WHERE is_active=1")
    count = c.fetchone()[0]
    c.execute("SELECT phone_number FROM sessions WHERE is_active=1")
    phones = [r[0] for r in c.fetchall()]
    await update.message.reply_text(f"<b>Active Sessions:</b> {count}\n" + "\n".join([f"• {p}" for p in phones]), parse_mode="HTML")

# ==================== MAIN ====================
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("au", au_command))
    app.add_handler(CommandHandler("bin", bin_command))
    app.add_handler(CommandHandler("fake", fake_command))
    
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("getuserdata", getuserdata_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("seslog", seslog_command))
    app.add_handler(CommandHandler("sescount", sescount_command))
    
    print("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
