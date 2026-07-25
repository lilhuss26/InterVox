from dotenv import load_dotenv
import os
from langchain_anthropic import ChatAnthropic

load_dotenv()

# Unused. Kept for reference in case the Inception path is revived.
# Constructing it at import time made INCEPTION_API_KEY mandatory just to boot
# the app — ChatOpenAI raises OpenAIError on a missing key — for a model that
# nothing ever called.
#
# from pydantic import SecretStr
# from langchain_openai import ChatOpenAI
#
# mercury = ChatOpenAI(
#     model="mercury-2",
#     temperature=0,
#     api_key=SecretStr(os.getenv("INCEPTION_API_KEY")),
#     base_url="https://api.inceptionlabs.ai/v1"
# )

haiku = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
    api_key=os.getenv("ANTHROPIC_API_KEY")
)

sonnet = ChatAnthropic(
    model="claude-3-5-sonnet-20241022",
    temperature=0,
    api_key=os.getenv("ANTHROPIC_API_KEY")
)

MODELS = {
    "haiku": haiku,
    "sonnet": sonnet,
}

DEFAULT_MODEL = "haiku"
