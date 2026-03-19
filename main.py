import pandas as pd
import ta
import yfinance as yf
import asyncio
import time
import logging
import threading
import os
from datetime import datetime
from flask import Flask
from telegram import Bot

TOKEN = "8623453596:AAFfUOnFh2faHWL0BKCeHhaCT6sdvR92bvQ"
CHAT_ID = 825330112

bot = Bot(token=TOKEN)

# журнал сигналів
logging.basicConfig(
    filename="signals.log",
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

pairs = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCHF OTC": "CHF=X",
    "NZDJPY OTC": "NZDJPY=X",
    "EURJPY OTC": "EURJPY=X",
    "EUR/CAD": "EURCAD=X",
    "CHF/NOK OTC": "CHFNOK=X",
    "AUD/CAD OTC": "AUDCAD=X",
    "GBP/JPY": "GBPJPY=X",
    "AUD/JPY": "AUDJPY=X",
    "NZD/USD": "NZDUSD=X"
}

last_signals_s1 = {}
last_signals_s2 = {}
cooldown_s2 = {}
error_cooldown = {}
last_morning_summary = None

# ===== СТРАТЕГІЯ 1: подвійний таймфрейм =====
def analyze_s1(pair_name, symbol):
    df5 = yf.download(symbol, interval="5m", period="1d", auto_adjust=True, progress=False)
    if df5.empty or len(df5) < 200:
        return None
    close5 = df5['Close'].squeeze()
    df5['ema200'] = ta.trend.ema_indicator(close5, window=200)
    trend_up = float(close5.iloc[-1]) > float(df5['ema200'].iloc[-1])
    trend_down = not trend_up

    df1 = yf.download(symbol, interval="1m", period="1d", auto_adjust=True, progress=False)
    if df1.empty or len(df1) < 50:
        return None
    close1 = df1['Close'].squeeze()
    df1['ema50'] = ta.trend.ema_indicator(close1, window=50)
    df1['rsi'] = ta.momentum.rsi(close1, window=14)

    close_last = float(close1.iloc[-1])
    ema50_last = float(df1['ema50'].iloc[-1])
    rsi_last = float(df1['rsi'].iloc[-1])

    signal = None
    if trend_up and rsi_last < 45 and close_last < ema50_last:
        signal = f"🟢 BUY {pair_name}\n📐 Стратегія 1 (1m + 5m)"
    elif trend_down and rsi_last > 55 and close_last > ema50_last:
        signal = f"🔴 SELL {pair_name}\n📐 Стратегія 1 (1m + 5m)"

    if signal == last_signals_s1.get(pair_name):
        return None
    last_signals_s1[pair_name] = signal
    return signal


# ===== СТРАТЕГІЯ 2: ATR + MACD + BB + вихідні + cooldown =====
def is_active_time():
    now = datetime.utcnow()
    if now.weekday() >= 5:  # субота=5, неділя=6
        return False
    return 7 <= now.hour <= 20

def analyze_s2(pair_name, symbol):
    if not is_active_time():
        return None

    df = yf.download(symbol, interval="1m", period="1d", auto_adjust=True, progress=False)

    # перевірка порожніх або недостатніх даних
    if df is None or df.empty or len(df) < 50:
        return None

    close = df['Close'].squeeze()
    high = df['High'].squeeze()
    low = df['Low'].squeeze()

    df['ema50'] = ta.trend.ema_indicator(close, window=50)
    df['ema200'] = ta.trend.ema_indicator(close, window=200)
    df['rsi'] = ta.momentum.rsi(close, window=14)
    df['atr'] = ta.volatility.average_true_range(high, low, close, window=14)

    # MACD
    macd_obj = ta.trend.MACD(close)
    df['macd'] = macd_obj.macd()
    df['macd_sig'] = macd_obj.macd_signal()

    # Bollinger Bands
    bb_obj = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df['bb_upper'] = bb_obj.bollinger_hband()
    df['bb_lower'] = bb_obj.bollinger_lband()

    ema50 = float(df['ema50'].iloc[-1])
    ema200 = float(df['ema200'].iloc[-1])
    rsi = float(df['rsi'].iloc[-1])
    atr_last = float(df['atr'].iloc[-1])
    atr_mean = float(df['atr'].mean())
    cl = float(close.iloc[-1])
    cl_prev = float(close.iloc[-2])
    macd = float(df['macd'].iloc[-1])
    macd_sig = float(df['macd_sig'].iloc[-1])
    bb_upper = float(df['bb_upper'].iloc[-1])
    bb_lower = float(df['bb_lower'].iloc[-1])

    # фільтр флету
    if atr_last < atr_mean:
        return None

    # фільтр слабкої свічки
    open_last = float(df['Open'].squeeze().iloc[-1])
    body = abs(cl - open_last)
    if body < float(close.std()) * 0.15:
        return None

    signal = None

    # BUY: тренд вгору + відкат до EMA50 + RSI + MACD бичачий + ціна біля нижньої BB
    if (ema50 > ema200 and
            cl <= ema50 and
            25 < rsi < 50 and
            cl > cl_prev and
            macd > macd_sig and
            cl <= bb_lower * 1.002):
        signal = "BUY"

    # SELL: тренд вниз + відкат до EMA50 + RSI + MACD ведмежий + ціна біля верхньої BB
    elif (ema50 < ema200 and
          cl >= ema50 and
          50 < rsi < 75 and
          cl < cl_prev and
          macd < macd_sig and
          cl >= bb_upper * 0.998):
        signal = "SELL"

    if not signal:
        return None

    # cooldown 5 хвилин
    now = time.time()
    if pair_name in cooldown_s2 and now - cooldown_s2[pair_name] < 300:
        return None

    text = f"{'🟢 BUY' if signal == 'BUY' else '🔴 SELL'} {pair_name}\n⏱ 1-2 хв\n🚀 ULTRA сигнал"

    if text == last_signals_s2.get(pair_name):
        return None

    last_signals_s2[pair_name] = text
    cooldown_s2[pair_name] = now

    # запис у журнал
    logging.info(f"{signal} | {pair_name} | RSI={rsi:.1f} | MACD={'▲' if macd > macd_sig else '▼'} | BB={'lower' if signal == 'BUY' else 'upper'}")

    return text


# ===== РАНКОВЕ ЗВЕДЕННЯ =====
async def send_morning_summary():
    global last_morning_summary
    now = datetime.utcnow()
    today = now.date()

    if now.hour == 7 and now.minute < 2 and last_morning_summary != today:
        last_morning_summary = today
        rows = []
        for name, symbol in pairs.items():
            try:
                df = yf.download(symbol, interval="1m", period="1d", auto_adjust=True, progress=False)
                if df is None or df.empty or len(df) < 50:
                    rows.append(f"❌ {name}: немає даних")
                    continue
                close = df['Close'].squeeze()
                high = df['High'].squeeze()
                low = df['Low'].squeeze()
                rsi = float(ta.momentum.rsi(close, window=14).iloc[-1])
                ema50 = float(ta.trend.ema_indicator(close, window=50).iloc[-1])
                ema200 = float(ta.trend.ema_indicator(close, window=200).iloc[-1])
                atr = ta.volatility.average_true_range(high, low, close, window=14)
                atr_ok = float(atr.iloc[-1]) >= float(atr.mean())
                trend = "🔼 UP" if ema50 > ema200 else "🔽 DOWN"
                rows.append(f"{name}: {trend} | RSI={rsi:.0f} | ATR={'✅' if atr_ok else '❌'}")
            except:
                rows.append(f"❌ {name}: помилка")

        msg = f"📊 Ранкове зведення ({now.strftime('%d.%m.%Y')})\n\n" + "\n".join(rows)
        await bot.send_message(chat_id=CHAT_ID, text=msg)


# ===== ОСНОВНИЙ ЦИКЛ =====
async def run_bot():
    await bot.send_message(chat_id=CHAT_ID, text="🤖 Бот запущено!\n📐 Стратегія 1 + 🚀 Стратегія 2 активні")

    while True:
        await send_morning_summary()

        for pair_name, symbol in pairs.items():
            # Стратегія 1
            try:
                sig1 = analyze_s1(pair_name, symbol)
                if sig1:
                    await bot.send_message(chat_id=CHAT_ID, text=sig1)
                    print(sig1)
            except Exception as e:
                now = time.time()
                if error_cooldown.get(f"s1_{pair_name}", 0) + 600 < now:
                    error_cooldown[f"s1_{pair_name}"] = now
                    await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ С1 помилка: {pair_name}")
                print(f"[С1] {pair_name}: {e}")

            # Стратегія 2
            try:
                sig2 = analyze_s2(pair_name, symbol)
                if sig2:
                    await bot.send_message(chat_id=CHAT_ID, text=sig2)
                    print(sig2)
            except Exception as e:
                now = time.time()
                if error_cooldown.get(f"s2_{pair_name}", 0) + 600 < now:
                    error_cooldown[f"s2_{pair_name}"] = now
                    await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ С2 помилка: {pair_name}")
                print(f"[С2] {pair_name}: {e}")

        await asyncio.sleep(60)



# ===== ВЕБ-СЕРВЕР (потрібен для деплою) =====
app = Flask(__name__)

@app.route("/")
def index():
    return "🤖 Trading Bot працює!", 200

def start_bot():
    asyncio.run(run_bot())

if __name__ == "__main__":
    thread = threading.Thread(target=start_bot, daemon=True)
    thread.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
