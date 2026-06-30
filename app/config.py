import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-mini"

VALID_INTENTS = [
    "infrastructure",
    "deployment", 
    "monitoring",
    "security",
    "troubleshooting"
]