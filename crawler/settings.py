import os

BOT_NAME = "tender_intelligence"

SPIDER_MODULES = ["crawler.spiders"]
NEWSPIDER_MODULE = "crawler.spiders"

ROBOTSTXT_OBEY = False
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://pguser:pgpass123@localhost:5433/tender-intelligence",
)