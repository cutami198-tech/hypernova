import os
import ccxt
import pandas as pd

# --- PATCH KEAMANAN PANDAS ---
if not hasattr(pd.DataFrame, 'append'):
    pd.DataFrame.append = lambda self, other, **kwargs: pd.concat([self, other], **kwargs)

import pandas_ta_classic as ta
import time
import telebot
from telebot import apihelper
import logging
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from threading import Thread
from flask import Flask
import requests

# ==========================================
# 1. KONFIGURASI UTAMA & SECRETS
# ==========================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID', '').strip()
JSONBIN_MASTER_KEY = os.environ.get('JSONBIN_MASTER_KEY', '').strip()

# Parameter Strategi & Risiko Baru
TIMEFRAME = '5m'
MAX_OPEN_POSITIONS = 5

BB_LENGTH = 20
BB_STD = 2.0 
RSI_LENGTH = 14
CCI_LENGTH = 14 

# Persentase SL & TP statis
STOP_LOSS_PCT = 0.015  # 1.5%
TAKE_PROFIT_PCT = 0.02 # 2%
  
MAX_LEVERAGE = 5      
SIMULATION_BALANCE = 5000.0 

FEE_RATE = 0.00035 
MIN_VOLATILITY_PCT_CRYPTO = 0.0     
MIN_VOLATILITY_PCT_RWA = 0.0

RWA_BASES = [
    'AAPL', 'AMD', 'AMZN', 'BABA', 'COIN', 'CRBS', 'CRCL', 'CRWV', 'DRAM', 'EWY', 
    'GOOGL', 'HOOD', 'INTC', 'META', 'MRVL', 'MSFT', 'MSTR', 'MU', 'NFLX', 'NVDA', 
    'ORCL', 'PLTR', 'QNT', 'SKHX', 'SMSN', 'SNDK', 'TSLA', 'TSM',
    'BRENTOIL', 'CL', 'COPPER', 'GOLD', 'NATGAS', 'PALLADIUM', 'PLATINUM', 'SILVER', 'WHEAT',
    'SPCX', 'ANTHROPIC', 'OPENAI'
]

active_positions = {}  
last_scanned_minute = -1
alerted_candle_timestamps = {}
cached_tickers = {}

start_balances = {
    'day': SIMULATION_BALANCE, 'week': SIMULATION_BALANCE, 'month': SIMULATION_BALANCE,
    'last_date': datetime.now().strftime('%Y-%m-%d')
}

period_stats = {
    'week': {'trades': 0, 'wins': 0, 'losses': 0},
    'month': {'trades': 0, 'wins': 0, 'losses': 0}
}

history_pnl = {'daily': {}, 'weekly': {}, 'monthly': {}}

telebot.logger.setLevel(logging.INFO)
apihelper.RETRY_ON_ERROR = True
apihelper.CONNECT_TIMEOUT = 30  
apihelper.READ_TIMEOUT = 30

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)

# ==========================================
# FUNGSI MANAJEMEN RISIKO (DYNAMIC LEVERAGE)
# ==========================================
def get_dynamic_leverage(symbol):
    clean_symbol = symbol.split('/')[0].replace("USDC", "").replace("USDT", "").replace("USD", "").strip().upper()
    LEV_5X = ["BTC", "ETH", "SP500"]
    LEV_4X = [
        "AAPL", "AMD", "AMZN", "BABA", "COIN", "CRBS", "CRCL", "CRWV", "DRAM", "EWY", 
        "GOOGL", "HOOD", "INTC", "META", "MRVL", "MSFT", "MSTR", "MU", "NFLX", "NVDA", 
        "ORCL", "PLTR", "QNT", "SKHX", "SMSN", "SNDK", "TSLA", "TSM",
        "BRENTOIL", "CL", "COPPER", "GOLD", "NATGAS", "PALLADIUM", "PLATINUM", "SILVER", "WHEAT",
        "SPCX"
    ]
    LEV_3X = ["HYPE"]
    
    if clean_symbol in LEV_5X: return 5
    elif clean_symbol in LEV_4X: return 4
    elif clean_symbol in LEV_3X: return 3
    else: return 2

# ==========================================
# 2. SISTEM OTAK AI (FIREWORKS AI)
# ==========================================
def ask_ai(system_prompt, user_prompt):
    api_key = os.environ.get('FIREWORKS_API_KEY', '').strip()
    if not api_key: return "*(AI Offline: Variabel 'FIREWORKS_API_KEY' belum dipasang)*"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "agentrouter/claude-opus-5", 
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.4
        }
        url = "https://agentrouter.org/v1/chat/completions"
        res = requests.post(url, json=payload, headers=headers, timeout=45)
        
        if res.status_code != 200: return f"*(Error HTTP {res.status_code}: {res.text[:200]})*"
        try: return res.json()['choices'][0]['message']['content'].strip()
        except Exception: return f"*(Gagal Parsing JSON! Balasan server: {res.text[:250]}...)*"
    except Exception as e: return f"*(Koneksi AI Terputus/Timeout: {str(e)})*"

# ==========================================
# 3. SISTEM MEMORI JSONBIN
# ==========================================
def load_memory():
    global SIMULATION_BALANCE, active_positions, start_balances, history_pnl, period_stats
    try:
        headers = {'X-Master-Key': JSONBIN_MASTER_KEY}
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        response = requests.get(url, headers=headers)
        data = response.json().get('record', {})
        SIMULATION_BALANCE = data.get('balance', 5000.0)
        active_positions = data.get('positions', {})
        start_balances = data.get('start_balances', {'day': SIMULATION_BALANCE, 'week': SIMULATION_BALANCE, 'month': SIMULATION_BALANCE, 'last_date': datetime.now().strftime('%Y-%m-%d')})
        period_stats = data.get('period_stats', {'week': {'trades': 0, 'wins': 0, 'losses': 0}, 'month': {'trades': 0, 'wins': 0, 'losses': 0}})
        history_pnl = data.get('history_pnl', {'daily': {}, 'weekly': {}, 'monthly': {}})
    except: pass

def save_memory():
    global SIMULATION_BALANCE, active_positions, start_balances, history_pnl, period_stats
    try:
        headers = {'Content-Type': 'application/json', 'X-Master-Key': JSONBIN_MASTER_KEY}
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        payload = {'balance': SIMULATION_BALANCE, 'positions': active_positions, 'start_balances': start_balances, 'period_stats': period_stats, 'history_pnl': history_pnl}
        requests.put(url, json=payload, headers=headers)
    except: pass

def check_performance_reset():
    global start_balances, SIMULATION_BALANCE, history_pnl, period_stats, active_positions
    try:
        now = datetime.now()
        current_date_str = now.strftime('%Y-%m-%d')
        last_date_str = start_balances.get('last_date', current_date_str)
        current_equity = SIMULATION_BALANCE + sum(pos['margin'] for pos in active_positions.values())

        if current_date_str != last_date_str:
            last_date = datetime.strptime(last_date_str, '%Y-%m-%d')
            history_pnl['daily'][last_date_str] = current_equity - start_balances['day']
            start_balances['day'] = current_equity
            if now.isocalendar()[1] != last_date.isocalendar()[1]: 
                week_str = f"{last_date.year}-W{last_date.isocalendar()[1]}"
                history_pnl['weekly'][week_str] = {'pnl': current_equity - start_balances['week'], 'trades': period_stats['week']['trades'], 'wins': period_stats['week']['wins'], 'losses': period_stats['week']['losses']}
                start_balances['week'] = current_equity
                period_stats['week'] = {'trades': 0, 'wins': 0, 'losses': 0}
            if now.month != last_date.month: 
                month_str = last_date.strftime('%Y-%m')
                history_pnl['monthly'][month_str] = {'pnl': current_equity - start_balances['month'], 'trades': period_stats['month']['trades'], 'wins': period_stats['month']['wins'], 'losses': period_stats['month']['losses']}
                start_balances['month'] = current_equity
                period_stats['month'] = {'trades': 0, 'wins': 0, 'losses': 0}
            start_balances['last_date'] = current_date_str
            save_memory()
    except: pass

def send_telegram_alert(message):
    def _send_task():
        for attempt in range(5): 
            try:
                bot.send_message(TELEGRAM_CHAT_ID, message, parse_mode='Markdown', timeout=30)
                return 
            except: time.sleep(5) 
    Thread(target=_send_task, daemon=True).start()

# ==========================================
# 4. MENU TELEGRAM & CHATBOT TERMINAL
# ==========================================
def get_main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📈 Posisi Aktif", callback_data="menu_posisi"))
    markup.row(InlineKeyboardButton("💰 Saldo", callback_data="menu_saldo"), InlineKeyboardButton("📊 Performa", callback_data="sub_menu_performa"))
    return markup

def get_perf_menu():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📅 Harian", callback_data="perf_harian"), InlineKeyboardButton("🗓️ Mingguan", callback_data="perf_mingguan"))
    markup.row(InlineKeyboardButton("🌑 Bulanan", callback_data="perf_bulanan"))
    markup.row(InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama"))
    return markup

def get_back_button():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama"))
    return markup

@bot.message_handler(commands=['start', 'menu'])
def send_menu(message):
    bot.send_message(message.chat.id, "🤖 *Control Panel & AI Assistant*\nKetik pertanyaan untuk mengobrol dengan AI, atau gunakan menu di bawah:", reply_markup=get_main_menu(), parse_mode="Markdown")

@bot.message_handler(commands=['reset'])
def reset_bot_system(message):
    if str(message.chat.id) != str(TELEGRAM_CHAT_ID): return
    global SIMULATION_BALANCE, active_positions, history_pnl, period_stats, start_balances
    SIMULATION_BALANCE = 5000.0
    active_positions.clear()
    history_pnl = {'daily': {}, 'weekly': {}, 'monthly': {}}
    period_stats = {'week': {'trades': 0, 'wins': 0, 'losses': 0}, 'month': {'trades': 0, 'wins': 0, 'losses': 0}}
    start_balances = {'day': 5000.0, 'week': 5000.0, 'month': 5000.0, 'last_date': datetime.now().strftime('%Y-%m-%d')}
    save_memory()
    bot.reply_to(message, "🔄 *Sistem Berhasil Di-Reset!*\nSaldo dikembalikan ke *$5000.00*.", parse_mode="Markdown")

@bot.message_handler(func=lambda msg: not msg.text.startswith('/'))
def handle_ai_chat(message):
    try: bot.send_chat_action(message.chat.id, 'typing')
    except: pass
    
    def worker_thread(msg_obj):
        try:
            user_text = msg_obj.text.upper()
            if 'SOLANA' in user_text: user_text += ' SOL'
            if 'BITCOIN' in user_text: user_text += ' BTC'
            if 'ETHEREUM' in user_text: user_text += ' ETH'
            
            detected_symbol = None
            market_data_context = ""
            
            if cached_tickers:
                for sym in cached_tickers.keys():
                    base_coin = sym.split('/')[0]
                    if f" {base_coin} " in f" {user_text} " or f"${base_coin}" in user_text:
                        detected_symbol = sym
                        break
            
            if detected_symbol:
                try:
                    bars = exchange.fetch_ohlcv(detected_symbol, TIMEFRAME, limit=100)
                    if not bars or len(bars) < 30:
                        market_data_context = f"\n\n[DATA MARKET: Grafik {detected_symbol} terlalu sedikit untuk dianalisis.]"
                    else:
                        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df['RSI'] = df.ta.rsi(close=df['close'], length=RSI_LENGTH)
                        df['CCI'] = df.ta.cci(high=df['high'], low=df['low'], close=df['close'], length=CCI_LENGTH)
                        bb = df.ta.bbands(close=df['close'], length=BB_LENGTH, std=BB_STD)
                        df = pd.concat([df, bb], axis=1)
                        
                        last_bar = df.iloc[-1]
                        curr_price = last_bar['close']
                        curr_rsi = last_bar['RSI']
                        curr_cci = last_bar['CCI']
                        
                        bbl = last_bar[[c for c in df.columns if 'BBL' in c][0]]
                        bbu = last_bar[[c for c in df.columns if 'BBU' in c][0]]
                        
                        band_range = bbu - bbl
                        pct_b = (curr_price - bbl) / band_range if band_range > 0 else 0.5
                        
                        market_data_context = f"\n\n[DATA REAL-TIME BURSA - {detected_symbol} - TF {TIMEFRAME}]\n"
                        market_data_context += f"Harga: ${curr_price:.4f}\nRSI: {curr_rsi:.2f} | CCI: {curr_cci:.2f} | BB %B: {pct_b:.2f}\n"
                        market_data_context += "INSTRUKSI: Gunakan data ini untuk analisa."
                except Exception:
                    market_data_context = f"\n\n(Gagal menarik data bursa untuk {detected_symbol}.)"

            portfolio_context = "Tidak ada posisi aktif saat ini."
            if active_positions:
                portfolio_context = "\n".join([f"- {sym}: {pos['type']} (Entry: {pos['entry_price']}, Margin: ${pos['margin']:.2f})" for sym, pos in active_positions.items()])
            
            sys_prompt = f"""Kamu adalah pengamat teknikal. Gaya bicaramu analitis dan objektif.
            Portofolio saat ini: {portfolio_context}
            Saldo: ${SIMULATION_BALANCE:.2f}.{market_data_context}
            Jawab padat dan langsung ke intinya. Dilarang memberikan saran finansial."""
            
            reply = ask_ai(sys_prompt, msg_obj.text)
            
            try: bot.reply_to(msg_obj, reply, parse_mode="Markdown")
            except Exception: bot.reply_to(msg_obj, reply)
                
        except Exception as e:
            try: bot.reply_to(msg_obj, f"*(Error Internal Otak AI: {str(e)})*")
            except: pass

    Thread(target=worker_thread, args=(message,), daemon=True).start()

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global cached_tickers, SIMULATION_BALANCE, active_positions, period_stats, history_pnl
    try: bot.answer_callback_query(call.id)
    except: pass
    check_performance_reset()
    try:
        if call.data == "menu_utama":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="🤖 *Control Panel & AI Assistant*\nKetik pertanyaan apapun untuk mengobrol dengan AI, atau gunakan menu di bawah:", reply_markup=get_main_menu(), parse_mode="Markdown")
        
        elif call.data == "menu_posisi":
            markup = InlineKeyboardMarkup()
            if not active_positions:
                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="ℹ️ *Tidak ada posisi yang sedang terbuka.*", reply_markup=get_back_button(), parse_mode="Markdown")
                return
            
            msg = f"📋 *DAFTAR POSISI AKTIF ({len(active_positions)}/{MAX_OPEN_POSITIONS})*\n\n"
            tickers = cached_tickers
            if not tickers: msg += "⚠️ _(Sedang memanaskan data harga, mohon tunggu...)_\n\n"
            
            for sym, pos in active_positions.items():
                try:
                    curr = tickers.get(sym, {}).get('last')
                    if curr is None: curr = pos['entry_price']
                    gross_pnl = (curr - pos['entry_price']) * pos['qty'] if pos['type'] == 'LONG' else (pos['entry_price'] - curr) * pos['qty']
                    entry_value = pos['entry_price'] * pos['qty']
                    current_value = curr * pos['qty']
                    net_pnl = gross_pnl - (entry_value * FEE_RATE + current_value * FEE_RATE)
                    lev = pos.get('leverage', get_dynamic_leverage(sym))
                    margin = pos.get('margin', entry_value / lev)
                    pnl_pct = (net_pnl / margin) * 100 if margin > 0 else 0
                    msg += f"🔹 *{sym}* | {pos['type']} *{lev}x*\n   Entry: {pos['entry_price']} | Now: {curr}\n   Margin: *${margin:.2f}*\n   Net PnL: {'🟢' if net_pnl >= 0 else '🔴'} *${net_pnl:.2f}* ({pnl_pct:.2f}%)\n   Target: {pos['tp']} | SL: {pos['sl']}\n\n"
                    markup.add(InlineKeyboardButton(f"🛑 Tutup {sym}", callback_data=f"close_{sym}"))
                except: msg += f"🔹 *{sym}* | (Error Kalkulasi)\n\n"
            
            markup.add(InlineKeyboardButton("🔙 Kembali", callback_data="menu_utama"))
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, reply_markup=markup, parse_mode="Markdown")
            
        elif call.data.startswith("close_"):
            symbol_to_close = call.data.replace("close_", "")
            if symbol_to_close in active_positions:
                pos = active_positions[symbol_to_close]
                tickers = cached_tickers
                curr = tickers.get(symbol_to_close, {}).get('last')
                if curr is None: curr = pos['entry_price']
                
                gross_pnl = (curr - pos['entry_price']) * pos['qty'] if pos['type'] == 'LONG' else (pos['entry_price'] - curr) * pos['qty']
                open_fee = (pos['entry_price'] * pos['qty']) * FEE_RATE
                close_fee = (curr * pos['qty']) * FEE_RATE
                net_pnl = gross_pnl - (open_fee + close_fee)
                
                returned_to_balance = max(0, pos['margin'] + net_pnl)
                SIMULATION_BALANCE += returned_to_balance
                
                period_stats['week']['trades'] += 1; period_stats['month']['trades'] += 1
                if net_pnl > 0: period_stats['week']['wins'] += 1; period_stats['month']['wins'] += 1
                else: period_stats['week']['losses'] += 1; period_stats['month']['losses'] += 1
                
                del active_positions[symbol_to_close]
                save_memory()
                bot.answer_callback_query(call.id, f"✅ {symbol_to_close} ditutup paksa!")
                bot.send_message(call.message.chat.id, f"🛑 *TUTUP MANUAL*\nAset: {symbol_to_close}\nTipe: {pos['type']}\n*Net P&L:* *{'+' if net_pnl >= 0 else ''}${net_pnl:.2f}*\nDikembalikan: ${returned_to_balance:.2f}", parse_mode="Markdown")
                callback_query(type('obj', (object,), {'id': call.id, 'data': 'menu_posisi', 'message': call.message})())
            else:
                bot.answer_callback_query(call.id, "⚠️ Koin sudah tertutup.")

        elif call.data == "menu_saldo":
            tf, locked_margin = 0.0, 0.0
            if active_positions:
                tickers = cached_tickers
                for sym, pos in active_positions.items():
                    curr = tickers.get(sym, {}).get('last') if tickers else None
                    if curr is None: curr = pos['entry_price']
                    gross_tf = (curr - pos['entry_price']) * pos['qty'] if pos['type'] == 'LONG' else (pos['entry_price'] - curr) * pos['qty']
                    fees = (pos['entry_price'] * pos['qty'] * FEE_RATE) + (curr * pos['qty'] * FEE_RATE)
                    tf += (gross_tf - fees)
                    locked_margin += pos['margin']
            total_equity = SIMULATION_BALANCE + locked_margin + tf
            msg = f"💰 *LAPORAN DOMPET*\n\n💵 *Saldo Tersedia:* ${SIMULATION_BALANCE:.2f}\n🔒 *Margin Terkunci:* ${locked_margin:.2f}\n🌊 *Floating Net:* {'🟢' if tf >= 0 else '🔴'} ${tf:.2f}\n🛡️ *Ekuitas:* ${total_equity:.2f}"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, reply_markup=get_back_button(), parse_mode="Markdown")
        
        elif call.data == "sub_menu_performa":
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="📊 *PILIH PERIODE LAPORAN*", reply_markup=get_perf_menu(), parse_mode="Markdown")
            
        elif call.data == "perf_harian":
            msg = "📅 *PERFORMA HARIAN*\n\n"
            if not history_pnl.get('daily'): msg += "_Belum ada data rekapan harian (Bot baru berjalan)._\n"
            else:
                for date_str, pnl in list(history_pnl['daily'].items())[-7:]: 
                    msg += f"▪️ {date_str}: {'🟢' if pnl >= 0 else '🔴'} *${pnl:.2f}*\n"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, reply_markup=get_back_button(), parse_mode="Markdown")

        elif call.data == "perf_mingguan":
            msg = "🗓️ *PERFORMA MINGGUAN*\n\n"
            if not history_pnl.get('weekly'): msg += "_Belum ada data rekapan mingguan._\n"
            else:
                for week_str, stats in list(history_pnl['weekly'].items())[-4:]: 
                    msg += f"▪️ {week_str}: {'🟢' if stats['pnl'] >= 0 else '🔴'} *${stats['pnl']:.2f}*\n"
                    msg += f"   Win: {stats['wins']} | Loss: {stats['losses']}\n"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, reply_markup=get_back_button(), parse_mode="Markdown")

        elif call.data == "perf_bulanan":
            msg = "🌑 *PERFORMA BULANAN*\n\n"
            if not history_pnl.get('monthly'): msg += "_Belum ada data rekapan bulanan._\n"
            else:
                for m_str, stats in list(history_pnl['monthly'].items())[-12:]: 
                    msg += f"▪️ {m_str}: {'🟢' if stats['pnl'] >= 0 else '🔴'} *${stats['pnl']:.2f}*\n"
                    msg += f"   Win: {stats['wins']} | Loss: {stats['losses']}\n"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=msg, reply_markup=get_back_button(), parse_mode="Markdown")
            
    except: pass

# ==========================================
# 5. ENGINE TRADING HYPERLIQUID 
# ==========================================
exchange = ccxt.hyperliquid({'enableRateLimit': True, 'options': {'defaultType': 'swap'}, 'timeout': 15000})

def get_all_hyperliquid_symbols():
    try:
        exchange.load_markets()
        return [sym for sym, mkt in exchange.markets.items() if mkt.get('linear') and mkt.get('active') and '/USDC' in sym]
    except: return []

def manage_active_positions():
    global active_positions, SIMULATION_BALANCE, period_stats, cached_tickers
    if not active_positions: return
    try:
        tickers = cached_tickers
        if not tickers: return
        closed_symbols = []
        memory_changed = False
        for symbol, pos in active_positions.items():
            curr = tickers.get(symbol, {}).get('last')
            if curr is None: continue 
            close_trade, gross_pnl, reason = False, 0.0, ""
            
            if pos['type'] == 'LONG':
                if curr >= pos['tp']: close_trade, reason, gross_pnl, exit_price = True, "✅ TAKE PROFIT", (pos['tp'] - pos['entry_price']) * pos['qty'], pos['tp']
                elif curr <= pos['sl']: close_trade, reason, gross_pnl, exit_price = True, "❌ STOP LOSS", (pos['sl'] - pos['entry_price']) * pos['qty'], pos['sl']
            else:
                if curr <= pos['tp']: close_trade, reason, gross_pnl, exit_price = True, "✅ TAKE PROFIT", (pos['entry_price'] - pos['tp']) * pos['qty'], pos['tp']
                elif curr >= pos['sl']: close_trade, reason, gross_pnl, exit_price = True, "❌ STOP LOSS", (pos['entry_price'] - pos['sl']) * pos['qty'], pos['sl']
            
            if close_trade:
                open_fee = (pos['entry_price'] * pos['qty']) * FEE_RATE
                close_fee = (exit_price * pos['qty']) * FEE_RATE
                net_pnl = gross_pnl - (open_fee + close_fee)
                returned_to_balance = max(0, pos['margin'] + net_pnl)
                SIMULATION_BALANCE += returned_to_balance
                
                period_stats['week']['trades'] += 1; period_stats['month']['trades'] += 1
                if net_pnl > 0: period_stats['week']['wins'] += 1; period_stats['month']['wins'] += 1
                else: period_stats['week']['losses'] += 1; period_stats['month']['losses'] += 1
                
                send_telegram_alert(f"🏁 *TRADE DITUTUP* 🏁\nAset: {symbol}\nTipe: {pos['type']}\n💡 Alasan: *{reason}*\n*Net P&L (Bersih):* *{'+' if net_pnl >= 0 else ''}${net_pnl:.2f}*\n💰 Saldo Tersedia: ${SIMULATION_BALANCE:.2f}")
                closed_symbols.append(symbol)
                memory_changed = True
                
        for s in closed_symbols: del active_positions[s]
        if memory_changed: save_memory()
    except: pass

def scan_for_signals(all_tickers, symbols):
    global active_positions, alerted_candle_timestamps, SIMULATION_BALANCE
    
    # --- FITUR BARU: BATAS MAKSIMAL POSISI ---
    if len(active_positions) >= MAX_OPEN_POSITIONS:
        return 
        
    try:
        filtered = []
        for s in symbols:
            if s in all_tickers:
                pct = all_tickers[s].get('percentage')
                if pct is None: pct = 0.0
                base_coin = s.split('/')[0] if '/' in s else s
                threshold = MIN_VOLATILITY_PCT_RWA if base_coin in RWA_BASES else MIN_VOLATILITY_PCT_CRYPTO
                if abs(pct) >= threshold: filtered.append((s, pct))
        
        changed = False
        for symbol, c24 in filtered:
            if len(active_positions) >= MAX_OPEN_POSITIONS:
                break
                
            if symbol in active_positions: continue
            time.sleep(0.3) 
            
            try:
                bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=200)
                if len(bars) < 50: continue
                df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                df['RSI'] = df.ta.rsi(close=df['close'], length=RSI_LENGTH)
                df['CCI'] = df.ta.cci(high=df['high'], low=df['low'], close=df['close'], length=CCI_LENGTH)
                bb = df.ta.bbands(close=df['close'], length=BB_LENGTH, std=BB_STD)
                df = pd.concat([df, bb], axis=1)
                
                bbl = [c for c in df.columns if 'BBL' in c][0]
                bbu = [c for c in df.columns if 'BBU' in c][0]
                
                t2, t1 = df.iloc[-3], df.iloc[-2]
                candle_ts = int(t1['timestamp'])
                
                if alerted_candle_timestamps.get(symbol) == candle_ts: continue
                
                band_range = t1[bbu] - t1[bbl]
                pct_b = (t1['close'] - t1[bbl]) / band_range if band_range > 0 else 0.5
                
                # ===============================================
                # 1. SINYAL LONG (Reversion)
                # ===============================================
                if (pct_b < 0.2) and (t1['RSI'] < 40) and (t1['CCI'] < -100):
                    ep = t1['close']
                    sl = ep * (1 - STOP_LOSS_PCT)      
                    tp = ep * (1 + TAKE_PROFIT_PCT)    
                    
                    # --- SIZING: 25% dari Saldo Tersedia ---
                    margin = SIMULATION_BALANCE * 0.25
                    
                    if SIMULATION_BALANCE >= margin and margin >= 2.0:
                        dyn_lev = get_dynamic_leverage(symbol)
                        qty = (margin * dyn_lev) / ep
                        
                        SIMULATION_BALANCE -= margin
                        active_positions[symbol] = {'type': 'LONG', 'entry_price': ep, 'tp': round(tp, 4), 'sl': round(sl, 4), 'qty': qty, 'leverage': dyn_lev, 'margin': margin}
                        
                        def _send_ai_signal(sym, entry_p, t1_rsi, t1_cci, t1_pctb, marg, lev, target_p, stop_l):
                            ai_reason = ask_ai("Kamu adalah analis teknikal reversal.", f"Koin {sym} masuk zona oversold. BB %B di {t1_pctb:.2f}, RSI di {t1_rsi:.1f}, CCI di {t1_cci:.1f}. Apa potensinya? Analisis 2 kalimat.")
                            send_telegram_alert(f"🟢 *LONG DIALIRKAN (Reversion)* 🟢\nAset: {sym}\n💡 AI: *{ai_reason}*\nEntry: {entry_p}\nMargin: *${marg:.2f}* ({lev}x)\nTP (2%): {round(target_p, 4)} | SL (1.5%): {round(stop_l, 4)}")
                        Thread(target=_send_ai_signal, args=(symbol, ep, t1['RSI'], t1['CCI'], pct_b, margin, dyn_lev, tp, sl), daemon=True).start()
                        
                        alerted_candle_timestamps[symbol] = candle_ts
                        changed = True
                    
                # ===============================================
                # 2. SINYAL SHORT (Reversion)
                # ===============================================
                elif (pct_b > 0.8) and (t1['RSI'] > 60) and (t1['CCI'] > 100):
                    ep = t1['close']
                    sl = ep * (1 + STOP_LOSS_PCT)      
                    tp = ep * (1 - TAKE_PROFIT_PCT)    
                    
                    # --- SIZING: 25% dari Saldo Tersedia ---
                    margin = SIMULATION_BALANCE * 0.25
                    
                    if SIMULATION_BALANCE >= margin and margin >= 2.0:
                        dyn_lev = get_dynamic_leverage(symbol)
                        qty = (margin * dyn_lev) / ep
                        
                        SIMULATION_BALANCE -= margin
                        active_positions[symbol] = {'type': 'SHORT', 'entry_price': ep, 'tp': round(tp, 4), 'sl': round(sl, 4), 'qty': qty, 'leverage': dyn_lev, 'margin': margin}
                        
                        def _send_ai_signal_short(sym, entry_p, t1_rsi, t1_cci, t1_pctb, marg, lev, target_p, stop_l):
                            ai_reason = ask_ai("Kamu adalah analis teknikal reversal.", f"Koin {sym} masuk zona overbought ekstrem. BB %B di {t1_pctb:.2f}, RSI di {t1_rsi:.1f}, CCI di {t1_cci:.1f}. Apa potensinya? Analisis 2 kalimat.")
                            send_telegram_alert(f"🔴 *SHORT DIALIRKAN (Reversion)* 🔴\nAset: {sym}\n💡 AI: *{ai_reason}*\nEntry: {entry_p}\nMargin: *${marg:.2f}* ({lev}x)\nTP (2%): {round(target_p, 4)} | SL (1.5%): {round(stop_l, 4)}")
                        Thread(target=_send_ai_signal_short, args=(symbol, ep, t1['RSI'], t1['CCI'], pct_b, margin, dyn_lev, tp, sl), daemon=True).start()
                        
                        alerted_candle_timestamps[symbol] = candle_ts
                        changed = True
            except: continue 
        if changed: save_memory()
    except: pass

# ==========================================
# 6. RUNTIME & FLASK SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Server Bot Aktif: Berjalan Lancar di Railway!"

def run_telegram_bot():
    for _ in range(3):
        try: 
            bot.remove_webhook()
            time.sleep(1)
        except: pass
    
    while True:
        try: 
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e: 
            print(f"Error Telegram: {e}") 
            time.sleep(5) 

def run_trading_engine():
    global last_scanned_minute, cached_tickers
    load_memory()
    check_performance_reset()
    send_telegram_alert("🚀 *Sistem Menyala Ulang!* 🚀\nPenyamaran Firewall Aktif & Telegram bebas Delay.")
    
    while True:
        try:
            all_tickers = exchange.fetch_tickers()
            cached_tickers = all_tickers 
            manage_active_positions()
            
            now = datetime.now()
            if now.minute != last_scanned_minute:
                check_performance_reset()
                all_syms = get_all_hyperliquid_symbols()
                scan_for_signals(all_tickers, all_syms)
                last_scanned_minute = now.minute
                
            time.sleep(10)
        except: time.sleep(5)

if __name__ == '__main__':
    Thread(target=run_telegram_bot, daemon=True).start()
    Thread(target=run_trading_engine, daemon=True).start()
    
    # --- PENYESUAIAN KHUSUS RAILWAY ---
    port = int(os.environ.get('PORT', 7860))
    app.run(host='0.0.0.0', port=port)
