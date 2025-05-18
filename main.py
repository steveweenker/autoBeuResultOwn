import os
import re
import time
import asyncio
import logging
import threading
import requests
from flask import Flask
from telegram import Bot, Update
from telegram.error import TelegramError, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from weasyprint import HTML
from logging.handlers import RotatingFileHandler
from requests.exceptions import Timeout, ConnectionError, HTTPError

# Configuration
BOT_TOKEN = '7816996700:AAHvNQaiobTUY3-twSN6Rt67wuKGaZKUdr8'
CHAT_ID = '1683604901'
URL = 'https://results.beup.ac.in/BTech4thSem2024_B2022Results.aspx'
CHECK_INTERVAL = 30  # seconds
DOWN_NOTIFICATION_INTERVAL = 7200  # 2 hours in seconds
REG_NO_FILE = 'registration_numbers.txt'
BATCH_SIZE = 5  # Batch size for processing registration numbers
BATCH_DELAY = 10  # Delay between batches (seconds)
MESSAGE_DELAY = 2  # Delay between Telegram messages (seconds)
RETRY_MAX_ATTEMPTS = 3  # Max retries for Telegram API calls

# Set up logging with rotating file handler
logging.basicConfig(
    handlers=[
        RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler()
    ],
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Verify weasyprint dependencies
try:
    from weasyprint import HTML
except ImportError as e:
    logger.error(f"WeasyPrint is not properly installed: {e}")
    print("Error: WeasyPrint is not installed or missing dependencies. Please install it.")
    exit(1)

# Flask app for keep-alive
app = Flask(__name__)


@app.route('/')
def home():
    return "I'm alive!"


def run_flask():
    app.run(host='0.0.0.0', port=8080)


def keep_alive():
    t = threading.Thread(target=run_flask)
    t.start()


def is_website_up():
    """Check if the website is up."""
    try:
        response = requests.get(URL, timeout=10)
        return response.status_code == 200
    except requests.RequestException:
        return False


async def send_telegram_message(bot, message, retries=0):
    """Send a Telegram message with retry on rate limit."""
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info("Notification sent successfully")
        await asyncio.sleep(MESSAGE_DELAY)  # Delay to avoid flood control
        return True
    except RetryAfter as e:
        if retries >= RETRY_MAX_ATTEMPTS:
            logger.error(f"Max retries reached for Telegram message: {e}")
            return False
        await asyncio.sleep(e.retry_after)
        return await send_telegram_message(bot, message, retries + 1)
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        return False


async def close_bot_with_retry(bot, retries=0):
    """Close bot session with retry on rate limit."""
    try:
        await bot.close()
        logger.info("Bot session closed successfully")
    except RetryAfter as e:
        if retries >= RETRY_MAX_ATTEMPTS:
            logger.error(f"Max retries reached for bot.close: {e}")
            return
        await asyncio.sleep(e.retry_after)
        await close_bot_with_retry(bot, retries + 1)
    except TelegramError as e:
        logger.error(f"Error closing bot: {e}")


def clean_registration_number(reg_no: str) -> str:
    """Clean and validate registration number."""
    reg_no = reg_no.strip()
    if not re.match(r'^\d{11}$', reg_no):
        return ""
    return reg_no


def generate_pdf(html_content: str, reg_no: str) -> str:
    """Generate PDF from HTML content."""
    pdf_path = f"{reg_no}_result.pdf"
    try:
        HTML(string=html_content).write_pdf(pdf_path)
        logger.info(f"Generated PDF for {reg_no} at {pdf_path}")
        return pdf_path
    except Exception as e:
        logger.error(f"PDF generation failed for {reg_no}: {str(e)}")
        raise


async def process_registration_batch(bot, registration_numbers, batch_number):
    """Process a batch of registration numbers."""
    successful = []
    failed = []

    for registration_number in registration_numbers:
        registration_number = clean_registration_number(registration_number)
        if not registration_number:
            failed.append((registration_number, "Invalid format"))
            await send_telegram_message(bot, f"Invalid format: '{registration_number}'. Expected: 22156148011")
            continue

        result_url = f"https://results.beup.ac.in/ResultsBTech4thSem2024_B2022Pub.aspx?Sem=IV&RegNo={registration_number}"
        await send_telegram_message(bot, f"Processing {registration_number}...")

        for attempt in range(3):
            try:
                response = requests.get(result_url, timeout=10)
                response.raise_for_status()

                if "Invalid Registration Number" in response.text:
                    failed.append((registration_number, "Invalid registration number"))
                    await send_telegram_message(bot, f"Invalid registration number: {registration_number}")
                    break

                pdf_path = generate_pdf(response.text, registration_number)
                try:
                    with open(pdf_path, 'rb') as pdf_file:
                        await bot.send_document(
                            chat_id=CHAT_ID,
                            document=pdf_file,
                            caption=f"Result for {registration_number}"
                        )
                    successful.append(registration_number)
                    logger.info(f"Successfully sent PDF for {registration_number}")
                finally:
                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)
                        logger.info(f"Deleted PDF file: {pdf_path}")
                break

            except Timeout:
                if attempt < 2:
                    await send_telegram_message(bot, f"Timeout for {registration_number}. Retrying...")
                    await asyncio.sleep(5)
                    continue
                failed.append((registration_number, "Request timed out"))
                await send_telegram_message(bot,
                                            f"Failed to fetch results for {registration_number}. Request timed out.")
                logger.error(f"Timeout after 3 attempts for {registration_number}")
                break
            except ConnectionError:
                if attempt < 2:
                    await send_telegram_message(bot, f"Connection error for {registration_number}. Retrying...")
                    await asyncio.sleep(5)
                    continue
                failed.append((registration_number, "Connection error"))
                await send_telegram_message(bot,
                                            f"Failed to fetch results for {registration_number}. Connection error.")
                logger.error(f"Connection error after 3 attempts for {registration_number}")
                break
            except HTTPError as e:
                failed.append((registration_number, f"Server error (status code: {response.status_code})"))
                await send_telegram_message(bot,
                                            f"Server error for {registration_number}. Status code: {response.status_code}")
                logger.error(f"HTTP error for {registration_number}: {e}")
                break
            except Exception as e:
                failed.append((registration_number, "Unexpected error"))
                await send_telegram_message(bot, f"Failed to process {registration_number}. Please try again later.")
                logger.error(f"Unexpected error for {registration_number}: {e}")
                break

        await asyncio.sleep(1)  # Rate limiting between requests

    return successful, failed


async def process_registration_numbers(bot):
    """Process all registration numbers from text file in batches."""
    if not os.path.exists(REG_NO_FILE):
        logger.error(f"Registration numbers file {REG_NO_FILE} not found")
        await send_telegram_message(bot, f"❌ Error: {REG_NO_FILE} not found")
        return False

    try:
        with open(REG_NO_FILE, 'r') as file:
            registration_numbers = [line.strip() for line in file if line.strip()]
    except Exception as e:
        logger.error(f"Failed to read {REG_NO_FILE}: {e}")
        await send_telegram_message(bot, f"❌ Error reading {REG_NO_FILE}: {e}")
        return False

    if not registration_numbers:
        logger.info("No registration numbers found in file")
        await send_telegram_message(bot, "⚠️ No registration numbers found in file")
        return False

    logger.info(f"Processing {len(registration_numbers)} registration numbers")
    await send_telegram_message(bot, f"Starting to process {len(registration_numbers)} registration numbers...")

    # Process in batches
    all_successful = []
    all_failed = []
    for i in range(0, len(registration_numbers), BATCH_SIZE):
        batch = registration_numbers[i:i + BATCH_SIZE]
        logger.info(f"Processing batch {i // BATCH_SIZE + 1} with {len(batch)} numbers")
        await send_telegram_message(bot, f"Processing batch {i // BATCH_SIZE + 1} ({len(batch)} numbers)...")

        successful, failed = await process_registration_batch(bot, batch, i // BATCH_SIZE + 1)
        all_successful.extend(successful)
        all_failed.extend(failed)

        if i + BATCH_SIZE < len(registration_numbers):
            await asyncio.sleep(BATCH_DELAY)  # Delay between batches

    # Send summary
    if all_successful or all_failed:
        summary = f"Processing complete for {len(registration_numbers)} registration numbers:\n"
        if all_successful:
            summary += f"- Successful ({len(all_successful)}): {', '.join(all_successful)}\n"
        if all_failed:
            summary += f"- Failed ({len(all_failed)}):\n" + "\n".join(
                f"  {reg_no}: {reason}" for reg_no, reason in all_failed)
        await send_telegram_message(bot, summary)
    else:
        await send_telegram_message(bot, "No valid registration numbers processed.")

    await send_telegram_message(bot,
                                "✅ Automated processing complete. Now accepting custom registration numbers. Use /start for instructions.")
    return True


async def start(update: Update, context: CallbackContext) -> None:
    """Send instructions when the command /start is issued."""
    await update.message.reply_text(
        "Welcome to the Results Bot!\n\n"
        "Enter up to 10 registration numbers, one per line.\n"
        "Format: 11 digits (e.g., 22156148011)\n"
        "Example:\n"
        "22156148011\n"
        "22156148012\n"
        "22156148013\n\n"
        "Use /help for more information."
    )


async def help_command(update: Update, context: CallbackContext) -> None:
    """Send help information when the command /help is issued."""
    await update.message.reply_text(
        "Results Bot Help:\n\n"
        "1. Enter up to 10 registration numbers, one per line.\n"
        "2. Format: 11 digits (e.g., 22156148011).\n"
        "3. The bot will fetch results and send PDFs for each valid number.\n"
        "4. Use /start to begin or retry.\n\n"
        "For issues, contact the bot administrator."
    )


async def get_registration_numbers(update: Update, context: CallbackContext) -> None:
    """Handle received registration numbers for custom search."""
    if str(update.message.chat_id) != CHAT_ID:
        await update.message.reply_text("Unauthorized access. This bot is private.")
        logger.warning(f"Unauthorized access attempt from chat ID: {update.message.chat_id}")
        return

    registration_numbers = update.message.text.split('\n')
    if len(registration_numbers) > 10:
        await update.message.reply_text(
            "Too many registration numbers. Please send up to 10 at a time."
        )
        return

    successful = []
    failed = []

    for registration_number in registration_numbers:
        registration_number = clean_registration_number(registration_number)
        if not registration_number:
            failed.append((registration_number, "Invalid format"))
            await update.message.reply_text(
                f"Invalid format: '{registration_number}'. Expected: 22156148011"
            )
            continue

        result_url = f"https://results.beup.ac.in/ResultsBTech4thSem2024_B2022Pub.aspx?Sem=IV&RegNo={registration_number}"
        await update.message.reply_text(f"Processing {registration_number}...")

        for attempt in range(3):
            try:
                response = requests.get(result_url, timeout=10)
                response.raise_for_status()

                if "Invalid Registration Number" in response.text:
                    failed.append((registration_number, "Invalid registration number"))
                    await update.message.reply_text(f"Invalid registration number: {registration_number}")
                    break

                pdf_path = generate_pdf(response.text, registration_number)
                try:
                    with open(pdf_path, 'rb') as pdf_file:
                        await update.message.reply_document(
                            document=pdf_file,
                            caption=f"Result for {registration_number}"
                        )
                    successful.append(registration_number)
                    logger.info(f"Successfully sent PDF for {registration_number}")
                finally:
                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)
                        logger.info(f"Deleted PDF file: {pdf_path}")
                break

            except Timeout:
                if attempt < 2:
                    await update.message.reply_text(f"Timeout for {registration_number}. Retrying...")
                    await asyncio.sleep(5)
                    continue
                failed.append((registration_number, "Request timed out"))
                await update.message.reply_text(
                    f"Failed to fetch results for {registration_number}. Request timed out."
                )
                logger.error(f"Timeout after 3 attempts for {registration_number}")
                break
            except ConnectionError:
                if attempt < 2:
                    await update.message.reply_text(f"Connection error for {registration_number}. Retrying...")
                    await asyncio.sleep(5)
                    continue
                failed.append((registration_number, "Connection error"))
                await update.message.reply_text(
                    f"Failed to fetch results for {registration_number}. Connection error."
                )
                logger.error(f"Connection error after 3 attempts for {registration_number}")
                break
            except HTTPError as e:
                failed.append((registration_number, f"Server error (status code: {response.status_code})"))
                await update.message.reply_text(
                    f"Server error for {registration_number}. Status code: {response.status_code}"
                )
                logger.error(f"HTTP error for {registration_number}: {e}")
                break
            except Exception as e:
                failed.append((registration_number, "Unexpected error"))
                await update.message.reply_text(
                    f"Failed to process {registration_number}. Please try again later."
                )
                logger.error(f"Unexpected error for {registration_number}: {e}")
                break

        await asyncio.sleep(1)  # Rate limiting between requests

    # Send summary
    if successful or failed:
        summary = "Processing complete:\n"
        if successful:
            summary += f"- Successful: {', '.join(successful)}\n"
        if failed:
            summary += "- Failed:\n" + "\n".join(f"  {reg_no}: {reason}" for reg_no, reason in failed)
        await update.message.reply_text(summary)
    else:
        await update.message.reply_text(
            "No valid registration numbers processed. Use /start to try again."
        )


async def error_handler(update: object, context: CallbackContext) -> None:
    """Log errors and notify user."""
    logger.error(f"Update {update} caused error {context.error}")
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("An error occurred. Please try again or use /help.")


async def start_custom_search():
    """Start Telegram bot for custom searches."""
    try:
        application = Application.builder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, get_registration_numbers))
        application.add_error_handler(error_handler)

        logger.info("Starting Telegram bot for custom searches...")
        await application.initialize()
        await application.start()
        await application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Error starting custom search bot: {e}")
        raise
    finally:
        try:
            await application.stop()
            await application.shutdown()
            logger.info("Custom search bot stopped and shut down")
        except Exception as e:
            logger.error(f"Error shutting down custom search bot: {e}")


async def monitor_website():
    """Monitor the website until it's up, then process registration numbers and start custom search."""
    bot = Bot(token=BOT_TOKEN)
    last_down_notification = 0

    logger.info("Starting website monitor...")
    await send_telegram_message(bot, "🔔 Result monitor started")

    try:
        while True:
            current_time = time.time()

            if is_website_up():
                logger.info("Website is UP")
                await send_telegram_message(bot, f"🎉 Website is LIVE!\n{URL}")
                break
            else:
                logger.info("Website is DOWN")
                if current_time - last_down_notification >= DOWN_NOTIFICATION_INTERVAL:
                    await send_telegram_message(bot, f"⚠️ Website is still DOWN\n{URL}")
                    last_down_notification = current_time

            await asyncio.sleep(CHECK_INTERVAL)

        # Process registration numbers from file
        success = await process_registration_numbers(bot)
        if not success:
            logger.info("File processing failed or empty, proceeding to custom search mode")

        # Close the initial bot session
        await close_bot_with_retry(bot)

        # Start custom search mode
        await start_custom_search()

    except KeyboardInterrupt:
        await send_telegram_message(bot, "🛑 Monitor stopped manually")
    except Exception as e:
        logger.error(f"Monitor error: {e}")
        await send_telegram_message(bot, f"❌ Monitor stopped due to error: {e}")
    finally:
        await close_bot_with_retry(bot)


async def main():
    """Main function to start monitoring and bot."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID is not set.")
        print("Error: BOT_TOKEN or CHAT_ID is missing.")
        exit(1)

    # Start Flask keep-alive server
    keep_alive()

    # Start monitoring website and bot
    await monitor_website()


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except Exception as e:
        logger.error(f"Main loop error: {e}")
    finally:
        if not loop.is_closed():
            loop.close()