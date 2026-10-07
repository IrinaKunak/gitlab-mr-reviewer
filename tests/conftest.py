"""Keep the suite hermetic: reviewer.config runs load_dotenv() on import, so a
developer's .env (e.g. AI_PROVIDER=openrouter) would leak into every Settings().
load_dotenv never overrides variables already set, so pinning them here wins."""

import os

os.environ["AI_PROVIDER"] = "anthropic"
