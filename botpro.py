import json
import asyncio
import websocket
import pandas as pd
import ta
import os
import logging

from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DERIV_TOKEN = os.getenv("DERIV_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100"]

users = {}
tasks = {}  # 🔥 controle das tasks

ultimo_ativo = None

# ===== DERIV =====
def conectar_deriv():
    ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089")
    ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    ws.recv()
    return ws

# ===== DADOS =====
def get_data(ws, symbol):
    req = {
        "ticks_history": symbol,
        "count": 120,
        "end": "latest",
        "style": "candles",
        "granularity": 300
    }

    ws.send(json.dumps(req))
    data = json.loads(ws.recv())

    df = pd.DataFrame(data["candles"])
    df["close"] = df["close"].astype(float)

    df["rsi"] = ta.momentum.RSIIndicator(df["close"]).rsi()

    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()

    df["ema"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()

    return df

# ===== SCORE =====
def calcular_score(df):
    last = df.iloc[-1]

    score = 0
    direcao = None

    if last["close"] > last["ema"]:
        score += 10
        direcao = "CALL"
    else:
        score += 10
        direcao = "PUT"

    if direcao == "CALL" and last["rsi"] < 45:
        score += 10

    if direcao == "PUT" and last["rsi"] > 55:
        score += 10

    if direcao == "CALL" and last["macd"] > last["macd_signal"]:
        score += 10

    if direcao == "PUT" and last["macd"] < last["macd_signal"]:
        score += 10

    return score, direcao

# ===== RANKING =====
def gerar_ranking(ws):
    ranking = []

    for symbol in SYMBOLS:
        try:
            df = get_data(ws, symbol)
            score, direcao = calcular_score(df)
            ranking.append((symbol, score, direcao))
        except:
            continue

    ranking.sort(key=lambda x: x[1], reverse=True)
    return ranking

# ===== ESCOLHER ATIVO =====
def escolher_ativo(ranking):
    global ultimo_ativo

    for symbol, score, direcao in ranking:
        if symbol != ultimo_ativo and score >= 15:
            ultimo_ativo = symbol
            return symbol, score, direcao

    return None, None, None

# ===== LOOP PRINCIPAL =====
async def rodar_bot(user_id, context):

    ws = conectar_deriv()
    stake = users[user_id]["stake"]

    try:
        while True:

            if not users[user_id]["ativo"]:
                logging.info(f"Bot parado para user {user_id}")
                break

            ranking = gerar_ranking(ws)
            symbol, score, direcao = escolher_ativo(ranking)

            if symbol:

                hora = datetime.now().strftime("%H:%M")
                link = f"https://app.deriv.com/?symbol={symbol}"

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Abrir na Deriv", url=link)]
                ])

                emoji = "🟢" if direcao == "CALL" else "🔴"

                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"""
SINAL DETECTADO

Ativo: {symbol}
Direção: {emoji} {direcao}
Valor: ${stake}
Tempo: 5 MIN

Horário: {hora}
Score: {score}
""",
                    reply_markup=keyboard
                )

            else:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="Analisando mercado... Nenhuma oportunidade."
                )

            await asyncio.sleep(300)

    except asyncio.CancelledError:
        logging.info(f"Task cancelada para user {user_id}")

# ===== TELEGRAM =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [InlineKeyboardButton("Conservador (0.5$)", callback_data="risk_0.5")],
        [InlineKeyboardButton("Moderado (1$)", callback_data="risk_1")],
        [InlineKeyboardButton("Agressivo (2$)", callback_data="risk_2")]
    ]

    await update.message.reply_text(
        "Escolha seu nível de risco:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def escolher(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    valor = float(query.data.split("_")[1])

    users[user_id] = {"stake": valor, "ativo": False}

    keyboard = [
        [InlineKeyboardButton("Iniciar Bot", callback_data="start_bot")],
        [InlineKeyboardButton("Parar Bot", callback_data="stop_bot")]
    ]

    await query.edit_message_text(
        f"Valor definido: ${valor}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def controle(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "start_bot":

        if users[user_id]["ativo"]:
            await query.edit_message_text("Já está rodando.")
            return

        users[user_id]["ativo"] = True

        task = asyncio.create_task(rodar_bot(user_id, context))
        tasks[user_id] = task

        await query.edit_message_text("Bot iniciado.")

    elif query.data == "stop_bot":

        users[user_id]["ativo"] = False

        if user_id in tasks:
            tasks[user_id].cancel()
            del tasks[user_id]

        await query.edit_message_text("Bot parado.")

# ===== MAIN =====
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(escolher, pattern="risk_"))
app.add_handler(CallbackQueryHandler(controle, pattern="start_bot|stop_bot"))

print("BOT ONLINE")
app.run_polling()