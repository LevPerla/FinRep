#!/usr/lib/python3.10 python3
# pylint: disable=unused-argument

import os
import sys
import pandas as pd

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackContext, CallbackQueryHandler
import logging
from datetime import datetime
import warnings
from dotenv import load_dotenv

from src.reports.main_report import create_main_report
from src.reports.year_report import create_year_report
from src.reports.month_report import create_month_report
from src import config

warnings.simplefilter(action='ignore', category=FutureWarning)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Load the .env file
load_dotenv()

# Define a base command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(f"""Hi, {user.mention_html()}! lets go""")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "There are some commnads: todo\n"
    )


async def warn_not_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Warn user"""
    await update.message.reply_text("Sorry, but I don't understand you, use /help to see how to use me")


async def error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Printing errors in console"""
    print(f"Update {update} cause error: {context.error}")


async def welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await context.bot.send_message(chat_id=chat_id, text="Hello! Welcome to the bot.\nTap /start command")
    
    
async def add_suggested_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
            [InlineKeyboardButton("Main", callback_data='report_main')],
            [InlineKeyboardButton("Year", callback_data='report_year')],
            [InlineKeyboardButton("Month", callback_data='report_month')],
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('Please choose an option:', reply_markup=reply_markup)
    
    
async def choose_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(context.chat_data)
    
    keyboard = []
    for currency in config.UNIQUE_TICKERS.keys():
        keyboard.append([InlineKeyboardButton(currency, callback_data=f'currency_{currency}')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text="Choose currency:", reply_markup=reply_markup)


async def send_report_query(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    
    if query.data in ['report_main', 'report_month', 'report_year']:
        # Save the chosen report type in context.user_data
        context.user_data['report_type'] = query.data
        await choose_currency(update, context)
        
    elif query.data.startswith('currency_'):
        chat_id = query.message.chat_id
        currency = query.data.split('_')[1]

        # Retrieve the chosen report type from context.user_data
        report_type = context.user_data.get('report_type')

        if report_type == 'report_main':
            report_png = create_main_report(currency, return_image=True)
            await context.bot.send_photo(chat_id, report_png)
        elif report_type == 'report_year':
            now = datetime.now()
            year = datetime.strftime(now, '%Y')
            report_png = create_year_report(currency=currency, year=year, return_image=True)
            await context.bot.send_photo(chat_id, report_png)
        elif report_type == 'report_month':
            now = datetime.now()
            year = datetime.strftime(now, '%Y')
            month = datetime.strftime(now, '%m')
            report_png = create_month_report(currency=currency, year=year, month=month, return_image=True)
            await context.bot.send_photo(chat_id, report_png)


def main() -> None:
    """Start the bot."""

    BOT_TOKEN = os.getenv('TG_TOKEN')

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # on different commands - answer in Telegram
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_message))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("get_actual_reports", add_suggested_actions))
    application.add_handler(CallbackQueryHandler(send_report_query))
    application.add_error_handler(error)

    # on non command i.e message - the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, warn_not_command))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    print("Hello. Cleint has just started.")
    main()
