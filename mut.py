#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MUTABAKAT BOT — Railway Edition
Profesyonel kasa takip ve mutabakat botu
"""

import logging
import os
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, CallbackContext
)

from sqlalchemy import (
    create_engine, Column, Integer, String,
    DateTime, Numeric, Text, Float, func
)
from sqlalchemy.orm import sessionmaker, declarative_base

# ============================================================
# CONFIG — Railway'de Environment Variable olarak tanımla
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8301516931:AAHLSjEV_E1ON7kLgjKYZybGtrD75e8yHp0")
DATABASE_URL   = os.environ.get("DATABASE_URL", "sqlite:///mutabakat.db")
PORT           = int(os.environ.get("PORT", 8443))
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "")
CONFIRM_LIMIT  = Decimal("50000")

# Railway PostgreSQL URL düzeltmesi
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ============================================================
# DATABASE
# ============================================================
Base = declarative_base()

db_kwargs = {}
if "postgresql" in DATABASE_URL:
    db_kwargs = {"pool_pre_ping": True, "pool_recycle": 300}

engine = create_engine(DATABASE_URL, echo=False, **db_kwargs)
Session = sessionmaker(bind=engine)


class Group(Base):
    __tablename__ = "groups"
    id      = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True, nullable=False)
    yat_kom = Column(Float, default=3.25)
    tes_kom = Column(Float, default=3.00)


class Transaction(Base):
    __tablename__ = "transactions"
    id         = Column(Integer, primary_key=True)
    group_id   = Column(Integer, nullable=False)
    ttype      = Column(String, nullable=False)
    amount     = Column(Numeric(20, 2), nullable=False)
    note       = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

# ============================================================
# BEKLEYEN ONAYLAR
# ============================================================
pending = {}


# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================
def fmt(val) -> str:
    """Türk para formatı: -1.234.567,89 ₺"""
    try:
        v = Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        sign = "-" if v < 0 else ""
        abs_v = abs(v)
        int_part = int(abs_v)
        dec_part = int(round((abs_v - int_part) * 100))
        int_str = f"{int_part:,}".replace(",", ".")
        return f"{sign}{int_str},{dec_part:02d} ₺"
    except Exception:
        return f"{val} ₺"


def parse_amount(text: str):
    """1.234.567,89 veya 1234567 veya -150000 → Decimal"""
    if not text:
        return None
    s = text.strip().replace(" ", "").replace("₺", "").replace("−", "-")
    negative = s.startswith("-")
    s = s.lstrip("-")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    try:
        val = Decimal(s)
        return -val if negative else val
    except Exception:
        return None


def parse_float(text: str):
    if not text:
        return None
    try:
        return float(text.strip().replace("%", "").replace(",", "."))
    except Exception:
        return None


def get_group(db, chat_id) -> Group:
    g = db.query(Group).filter_by(chat_id=str(chat_id)).first()
    if not g:
        g = Group(chat_id=str(chat_id))
        db.add(g)
        db.commit()
    return g


def add_tx(db, group_id: int, ttype: str, amount: Decimal, note: str = ""):
    tx = Transaction(group_id=group_id, ttype=ttype, amount=amount, note=note)
    db.add(tx)
    db.commit()
    return tx


def today_str() -> str:
    return date.today().isoformat()


def get_date_txs(db, group_id: int, d: str):
    return db.query(Transaction).filter(
        Transaction.group_id == group_id,
        func.date(Transaction.created_at) == d
    ).all()


def compute_kasa(carry, dep, yat_pct, wdr, man_wdr, dlv, tes_pct):
    """
    Net = Devir + Yatırım − YatırımKom − Çekim − ManuelÇekim − Teslimat − TeslimatKom
    """
    yat_kom = (dep * Decimal(str(yat_pct)) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    tes_kom = (dlv * Decimal(str(tes_pct)) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    net = carry + dep - yat_kom - wdr - man_wdr - dlv - tes_kom
    return net, yat_kom, tes_kom


def build_mut_text(carry, dep, yat_pct, yat_kom,
                   wdr, man_wdr, dlv, tes_pct, tes_kom,
                   net, tarih_fmt: str) -> str:
    emoji = "🟢" if net >= 0 else "🔴"
    return (
        f"🏴 MUTABAKAT — {tarih_fmt}\n"
        f"{'─' * 34}\n"
        f"📥 Devir               : {fmt(carry)}\n"
        f"💰 Yatırım             : {fmt(dep)}\n"
        f"📊 Yatırım Kom (%{yat_pct}): {fmt(yat_kom)}\n"
        f"💸 Çekim               : {fmt(wdr)}\n"
        f"✋ Manuel Çekim        : {fmt(man_wdr)}\n"
        f"🚚 Teslimat            : {fmt(dlv)}\n"
        f"📊 Teslimat Kom (%{tes_pct}): {fmt(tes_kom)}\n"
        f"{'─' * 34}\n"
        f"{emoji} NET KASA            : {fmt(net)}\n"
    )


def get_durum(db, g: Group, d: str = None):
    tarih = d or today_str()

    carry_tx = db.query(Transaction).filter(
        Transaction.group_id == g.id,
        Transaction.ttype == "carryover",
        func.date(Transaction.created_at) <= tarih
    ).order_by(Transaction.id.desc()).first()
    carry = Decimal(str(carry_tx.amount)) if carry_tx else Decimal("0")

    txs = get_date_txs(db, g.id, tarih)
    dep     = sum((Decimal(str(t.amount)) for t in txs if t.ttype == "deposit"),          Decimal("0"))
    wdr     = sum((Decimal(str(t.amount)) for t in txs if t.ttype == "withdraw"),         Decimal("0"))
    man_wdr = sum((Decimal(str(t.amount)) for t in txs if t.ttype == "manual_withdraw"),  Decimal("0"))
    dlv     = sum((Decimal(str(t.amount)) for t in txs if t.ttype == "delivery"),         Decimal("0"))

    net, yat_kom, tes_kom = compute_kasa(carry, dep, g.yat_kom, wdr, man_wdr, dlv, g.tes_kom)
    tarih_fmt = datetime.strptime(tarih, "%Y-%m-%d").strftime("%d.%m.%Y")

    text = build_mut_text(
        carry, dep, g.yat_kom, yat_kom,
        wdr, man_wdr, dlv, g.tes_kom, tes_kom,
        net, tarih_fmt
    )
    return text, net, txs


# ============================================================
# ONAY AKIŞI
# ============================================================
def ask_confirm(update: Update, key, label: str, amount: Decimal):
    uid = key[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Onayla", callback_data=f"confirm_{uid}"),
        InlineKeyboardButton("❌ İptal",  callback_data=f"cancel_{uid}")
    ]])
    update.message.reply_text(
        f"⚠️ Onay Gerekiyor\n\n"
        f"İşlem : {label}\n"
        f"Tutar : {fmt(amount)}\n\n"
        f"Devam etmek istiyor musun?",
        reply_markup=kb
    )


def record_or_confirm(update: Update, ttype: str, label: str, amount: Decimal, note: str = ""):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    key = (chat_id, user_id)

    if abs(amount) >= CONFIRM_LIMIT:
        pending[key] = {"ttype": ttype, "amount": amount, "label": label, "note": note}
        ask_confirm(update, key, label, amount)
        return

    db = Session()
    g = get_group(db, chat_id)
    add_tx(db, g.id, ttype, amount, note)
    db.close()
    update.message.reply_text(f"✅ {label} kaydedildi: {fmt(amount)}")


def handle_callback(update: Update, ctx: CallbackContext):
    q = update.callback_query
    q.answer()
    data = q.data
    uid = int(data.split("_")[1])
    key = (q.message.chat_id, uid)
    action = pending.get(key)

    if not action:
        return q.edit_message_text("❌ Süresi dolmuş veya işlem bulunamadı.")

    if data.startswith("confirm"):
        if action["ttype"] == "__reset__":
            db = Session()
            g = get_group(db, q.message.chat_id)
            db.query(Transaction).filter(
                Transaction.group_id == g.id,
                func.date(Transaction.created_at) == today_str(),
                Transaction.ttype != "carryover"
            ).delete(synchronize_session=False)
            db.commit()
            db.close()
            q.edit_message_text("✅ Bugünkü işlemler sıfırlandı.")
        else:
            db = Session()
            g = get_group(db, q.message.chat_id)
            add_tx(db, g.id, action["ttype"], action["amount"], action.get("note", ""))
            db.close()
            q.edit_message_text(f"✅ Kaydedildi\n{action['label']}: {fmt(action['amount'])}")
    else:
        q.edit_message_text("❌ İptal edildi.")

    pending.pop(key, None)


# ============================================================
# KOMUTLAR
# ============================================================
def cmd_start(u: Update, c: CallbackContext):
    db = Session()
    get_group(db, u.effective_chat.id)
    db.close()
    u.message.reply_text(
        "🏦 Mutabakat Bot'a Hoş Geldin!\n\n"
        "Tüm komutlar için: /yardim"
    )


def cmd_yardim(u: Update, c: CallbackContext):
    u.message.reply_text(
        "📖 KOMUT LİSTESİ\n\n"
        "━━━ İŞLEM KOMUTLARI ━━━\n"
        "/yatirim 500.000     → Yatırım ekle\n"
        "/cekim 100.000       → Çekim ekle\n"
        "/manuel 50.000       → Manuel çekim\n"
        "/teslimat 300.000    → Teslimat ekle\n"
        "/devir -150.000      → Devir (eksi olabilir)\n\n"
        "━━━ RAPOR KOMUTLARI ━━━\n"
        "/durum               → Anlık kasa özeti\n"
        "/mutabakat           → Detaylı mutabakat\n"
        "/gecmis 07.04.2026   → Geçmiş gün raporu\n\n"
        "━━━ YÖNETİM KOMUTLARI ━━━\n"
        "/komisyon 3.25 3     → Oranları güncelle\n"
        "/ayarlar             → Mevcut oranları gör\n"
        "/geri                → Son işlemi sil\n"
        "/sifirla             → Bugünü sıfırla\n\n"
        "━━━ FORMÜL ━━━\n"
        "Net = Devir + Yatırım\n"
        "    - Yatırım Kom\n"
        "    - Çekim - Manuel Çekim\n"
        "    - Teslimat - Teslimat Kom"
    )


def cmd_yatirim(u: Update, c: CallbackContext):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt or amt <= 0:
        return u.message.reply_text("❌ Kullanım: /yatirim 500.000")
    record_or_confirm(u, "deposit", "💰 Yatırım", amt)


def cmd_cekim(u: Update, c: CallbackContext):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt or amt <= 0:
        return u.message.reply_text("❌ Kullanım: /cekim 100.000")
    record_or_confirm(u, "withdraw", "💸 Çekim", amt)


def cmd_manuel(u: Update, c: CallbackContext):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt or amt <= 0:
        return u.message.reply_text("❌ Kullanım: /manuel 50.000")
    record_or_confirm(u, "manual_withdraw", "✋ Manuel Çekim", amt)


def cmd_teslimat(u: Update, c: CallbackContext):
    amt = parse_amount(c.args[0]) if c.args else None
    if not amt or amt <= 0:
        return u.message.reply_text("❌ Kullanım: /teslimat 300.000")
    record_or_confirm(u, "delivery", "🚚 Teslimat", amt)


def cmd_devir(u: Update, c: CallbackContext):
    raw = c.args[0] if c.args else None
    if not raw:
        return u.message.reply_text("❌ Kullanım: /devir -150.000  veya  /devir 50.000")
    amt = parse_amount(raw)
    if amt is None:
        return u.message.reply_text("❌ Geçersiz tutar.")
    db = Session()
    g = get_group(db, u.effective_chat.id)
    add_tx(db, g.id, "carryover", amt)
    db.close()
    u.message.reply_text(f"♻️ Devir güncellendi: {fmt(amt)}")


def cmd_komisyon(u: Update, c: CallbackContext):
    db = Session()
    g = get_group(db, u.effective_chat.id)

    if not c.args:
        db.close()
        return u.message.reply_text(
            f"📊 Mevcut Komisyon Oranları:\n\n"
            f"   Yatırım Kom : %{g.yat_kom}\n"
            f"   Teslimat Kom: %{g.tes_kom}\n\n"
            f"Değiştirmek için:\n"
            f"/komisyon <yatırım%> <teslimat%>\n"
            f"Örnek: /komisyon 3.25 3"
        )

    yat = parse_float(c.args[0]) if len(c.args) > 0 else None
    tes = parse_float(c.args[1]) if len(c.args) > 1 else None

    if yat is None or tes is None:
        db.close()
        return u.message.reply_text(
            "❌ Kullanım: /komisyon <yatırım%> <teslimat%>\n"
            "Örnek: /komisyon 3.25 3"
        )

    g.yat_kom = yat
    g.tes_kom = tes
    db.commit()
    db.close()
    u.message.reply_text(
        f"✅ Komisyon oranları güncellendi:\n\n"
        f"   Yatırım Kom : %{yat}\n"
        f"   Teslimat Kom: %{tes}"
    )


def cmd_ayarlar(u: Update, c: CallbackContext):
    db = Session()
    g = get_group(db, u.effective_chat.id)
    db.close()
    u.message.reply_text(
        f"⚙️ Grup Ayarları\n\n"
        f"Yatırım Komisyonu : %{g.yat_kom}\n"
        f"Teslimat Komisyonu: %{g.tes_kom}\n\n"
        f"Değiştirmek için: /komisyon <yat%> <tes%>"
    )


def cmd_durum(u: Update, c: CallbackContext):
    db = Session()
    g = get_group(db, u.effective_chat.id)
    text, _, _ = get_durum(db, g)
    db.close()
    u.message.reply_text(text)


def cmd_mutabakat(u: Update, c: CallbackContext):
    db = Session()
    g = get_group(db, u.effective_chat.id)
    text, net, txs = get_durum(db, g)

    if txs:
        type_label = {
            "deposit":         "💰 Yatırım",
            "withdraw":        "💸 Çekim",
            "manual_withdraw": "✋ Manuel",
            "delivery":        "🚚 Teslimat",
            "carryover":       "♻️ Devir"
        }
        lines = ["\n📋 İşlem Geçmişi (Bugün):"]
        for t in txs:
            label = type_label.get(t.ttype, t.ttype)
            zaman = t.created_at.strftime("%H:%M")
            lines.append(f"  {label}  {zaman}  →  {fmt(t.amount)}")
        text += "\n".join(lines)

    db.close()
    u.message.reply_text(text)


def cmd_gecmis(u: Update, c: CallbackContext):
    if not c.args:
        return u.message.reply_text("❌ Kullanım: /gecmis 07.04.2026")
    try:
        d = datetime.strptime(c.args[0], "%d.%m.%Y").date().isoformat()
    except ValueError:
        return u.message.reply_text("❌ Format: GG.AA.YYYY  Örnek: 07.04.2026")

    db = Session()
    g = get_group(db, u.effective_chat.id)
    text, _, _ = get_durum(db, g, d)
    db.close()
    u.message.reply_text(text)


def cmd_geri(u: Update, c: CallbackContext):
    db = Session()
    g = get_group(db, u.effective_chat.id)

    tx = db.query(Transaction).filter(
        Transaction.group_id == g.id,
        Transaction.ttype != "carryover"
    ).order_by(Transaction.id.desc()).first()

    if not tx:
        db.close()
        return u.message.reply_text("❌ Silinecek işlem bulunamadı.")

    label = {
        "deposit":         "💰 Yatırım",
        "withdraw":        "💸 Çekim",
        "manual_withdraw": "✋ Manuel Çekim",
        "delivery":        "🚚 Teslimat"
    }.get(tx.ttype, tx.ttype)

    amt = tx.amount
    db.delete(tx)
    db.commit()
    db.close()
    u.message.reply_text(f"♻️ Son işlem silindi:\n{label}: {fmt(amt)}")


def cmd_sifirla(u: Update, c: CallbackContext):
    key = (u.effective_chat.id, u.effective_user.id)
    pending[key] = {
        "ttype": "__reset__",
        "amount": Decimal("0"),
        "label": "SIFIRLA",
        "note": ""
    }
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Evet, Sıfırla", callback_data=f"confirm_{u.effective_user.id}"),
        InlineKeyboardButton("❌ İptal",          callback_data=f"cancel_{u.effective_user.id}")
    ]])
    u.message.reply_text(
        "⚠️ Bugünkü tüm işlemler silinecek!\nEmin misin?",
        reply_markup=kb
    )


# ============================================================
# MAIN
# ============================================================
def main():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start",     cmd_start))
    dp.add_handler(CommandHandler("yardim",    cmd_yardim))
    dp.add_handler(CommandHandler("yatirim",   cmd_yatirim))
    dp.add_handler(CommandHandler("cekim",     cmd_cekim))
    dp.add_handler(CommandHandler("manuel",    cmd_manuel))
    dp.add_handler(CommandHandler("teslimat",  cmd_teslimat))
    dp.add_handler(CommandHandler("devir",     cmd_devir))
    dp.add_handler(CommandHandler("komisyon",  cmd_komisyon))
    dp.add_handler(CommandHandler("ayarlar",   cmd_ayarlar))
    dp.add_handler(CommandHandler("durum",     cmd_durum))
    dp.add_handler(CommandHandler("mutabakat", cmd_mutabakat))
    dp.add_handler(CommandHandler("gecmis",    cmd_gecmis))
    dp.add_handler(CommandHandler("geri",      cmd_geri))
    dp.add_handler(CommandHandler("sifirla",   cmd_sifirla))
    dp.add_handler(CallbackQueryHandler(handle_callback))

    if WEBHOOK_URL:
        log.info(f"Webhook modu: {WEBHOOK_URL}")
        updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
        )
    else:
        log.info("Polling modu başlatılıyor...")
        updater.start_polling(drop_pending_updates=True)

    log.info("Bot hazır!")
    updater.idle()


if __name__ == "__main__":
    main()
