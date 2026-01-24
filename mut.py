#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import re
from datetime import datetime, date, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler
from telegram.ext.callbackcontext import CallbackContext

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, func
)
from sqlalchemy.orm import sessionmaker, declarative_base

# ================= CONFIG =================
TELEGRAM_TOKEN = "8559555118:AAEOSO28H1yPajKXNPjbViWz4NVB5FVG3Ms"
DB_URL = "sqlite:///mutabakat.db"
CONFIRM_LIMIT = 100_000

logging.basicConfig(level=logging.INFO)

# ================= DATABASE =================
Base = declarative_base()
engine = create_engine(DB_URL, echo=False)
Session = sessionmaker(bind=engine)

class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer)
    type = Column(String)  # deposit, withdraw, delivery, carryover
    amount = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

class DailyCommission(Base):
    __tablename__ = "daily_commissions"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer)
    date = Column(String)  # YYYY-MM-DD
    percent = Column(Integer)

Base.metadata.create_all(engine)

# ================= MEMORY =================
pending_actions = {}  # (chat_id, user_id) -> {type, amount}

# ================= HELPERS =================
def parse_amount(text):
    if not text:
        return None
    s = re.sub(r"[^\d]", "", text)
    return int(s) if s else None

def parse_percentage(text):
    if not text or "%" not in text:
        return None
    try:
        return float(text.replace("%", "").replace(",", "."))
    except ValueError:
        return None

def get_group(db, chat_id):
    g = db.query(Group).filter_by(chat_id=str(chat_id)).first()
    if not g:
        g = Group(chat_id=str(chat_id))
        db.add(g)
        db.commit()
    return g

def add_tx(db, group_id, ttype, amount):
    db.add(Transaction(group_id=group_id, type=ttype, amount=amount))
    db.commit()

def last_carry(db, group_id):
    tx = db.query(Transaction).filter(
        Transaction.group_id == group_id,
        Transaction.type == "carryover"
    ).order_by(Transaction.id.desc()).first()
    return tx.amount if tx else 0

# ================= CORE =================
def start(update: Update, ctx: CallbackContext):
    db = Session()
    get_group(db, update.effective_chat.id)
    db.close()
    update.message.reply_text("✅ Grup kayıt edildi.")

def process_action(update, action_type, amount):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if amount >= CONFIRM_LIMIT:
        pending_actions[(chat_id, user_id)] = {
            "type": action_type,
            "amount": amount
        }
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Onayla", callback_data="confirm"),
            InlineKeyboardButton("❌ İptal", callback_data="cancel")
        ]])
        return update.message.reply_text(
            f"⚠️ {amount:,} TL işlem onayı\nEmin misin?",
            reply_markup=kb
        )

    db = Session()
    g = get_group(db, chat_id)
    add_tx(db, g.id, action_type, amount)
    db.close()
    update.message.reply_text(f"✅ Kaydedildi: {amount:,}")

def handle_callback(update: Update, ctx: CallbackContext):
    q = update.callback_query
    q.answer()

    key = (q.message.chat_id, q.from_user.id)
    action = pending_actions.get(key)

    if not action:
        return q.edit_message_text("❌ İşlem bulunamadı.")

    if q.data == "confirm":
        db = Session()
        g = get_group(db, q.message.chat_id)
        add_tx(db, g.id, action["type"], action["amount"])
        db.close()
        q.edit_message_text(f"✅ Onaylandı: {action['amount']:,}")
    else:
        q.edit_message_text("❌ İptal edildi.")

    pending_actions.pop(key, None)

# ================= COMMANDS =================
def yatirim(u, c):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt:
        return u.message.reply_text("Kullanım: /yatirim 100000")
    process_action(u, "deposit", amt)

def cekim(u, c):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt:
        return u.message.reply_text("Kullanım: /cekim 50000")
    process_action(u, "withdraw", amt)

def teslimat(u, c):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt:
        return u.message.reply_text("Kullanım: /teslimat 30000")
    process_action(u, "delivery", amt)

def komisyon(update: Update, ctx: CallbackContext):
    percent = parse_percentage(ctx.args[0]) if ctx.args else None
    if percent is None:
        return update.message.reply_text("Kullanım: /komisyon %14")

    db = Session()
    g = get_group(db, update.effective_chat.id)
    today = date.today().isoformat()

    db.query(DailyCommission).filter_by(
        group_id=g.id,
        date=today
    ).delete()

    db.add(DailyCommission(
        group_id=g.id,
        date=today,
        percent=int(percent)
    ))
    db.commit()
    db.close()

    update.message.reply_text(f"💸 Bugünkü komisyon %{int(percent)} olarak ayarlandı.")

def devir(update: Update, ctx: CallbackContext):
    amt = parse_amount(ctx.args[0]) if ctx.args else None
    if amt is None:
        return update.message.reply_text("Kullanım: /devir 250000")

    db = Session()
    g = get_group(db, update.effective_chat.id)
    add_tx(db, g.id, "carryover", amt)
    db.close()

    update.message.reply_text(f"♻️ Devir güncellendi: {amt:,}")

def geri(update: Update, ctx: CallbackContext):
    db = Session()
    g = get_group(db, update.effective_chat.id)

    tx = db.query(Transaction).filter(
        Transaction.group_id == g.id,
        Transaction.type != "carryover"
    ).order_by(Transaction.id.desc()).first()

    if not tx:
        db.close()
        return update.message.reply_text("❌ Geri alınacak işlem yok.")

    db.delete(tx)
    db.commit()
    db.close()
    update.message.reply_text("♻️ Son işlem geri alındı.")

def durum(update: Update, ctx: CallbackContext):
    db = Session()
    g = get_group(db, update.effective_chat.id)

    yesterday = date.today() - timedelta(days=1)
    carry = last_carry(db, g.id)

    txs = db.query(Transaction).filter(
        Transaction.group_id == g.id,
        func.date(Transaction.created_at) == str(date.today())
    ).all()

    d = sum(t.amount for t in txs if t.type == "deposit")
    w = sum(t.amount for t in txs if t.type == "withdraw")
    t = sum(t.amount for t in txs if t.type == "delivery")

    dc = db.query(DailyCommission).filter_by(
        group_id=g.id,
        date=date.today().isoformat()
    ).first()

    commission = int(d * (dc.percent / 100)) if dc else 0
    net = carry + d - w - t - commission

    db.close()

    update.message.reply_text(
        f"📊 Kasa Durumu\n\n"
        f"Devir: {carry:,}\n"
        f"Yatırım: {d:,}\n"
        f"Çekim: {w:,}\n"
        f"Teslimat: {t:,}\n"
        f"Komisyon: {commission:,}\n\n"
        f"💰 Net: {net:,}"
    )

def mutabakat(u, c):
    durum(u, c)

# ================= START =================
def main():
    up = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = up.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("yatirim", yatirim))
    dp.add_handler(CommandHandler("cekim", cekim))
    dp.add_handler(CommandHandler("teslimat", teslimat))
    dp.add_handler(CommandHandler("komisyon", komisyon))
    dp.add_handler(CommandHandler("devir", devir))
    dp.add_handler(CommandHandler("geri", geri))
    dp.add_handler(CommandHandler("durum", durum))
    dp.add_handler(CommandHandler("mutabakat", mutabakat))
    dp.add_handler(CallbackQueryHandler(handle_callback))

    up.start_polling()
    up.idle()

if __name__ == "__main__":
    main()
