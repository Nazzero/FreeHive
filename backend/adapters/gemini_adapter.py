import httpx
from pathlib import Path

GEMINI_API_KEY = "AIzaSyAOPxgfhNMS_LDrUfhJel5R9TjEPowbZ6U"
GEMINI_MODEL = "gemini-3-flash-preview"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


class GeminiAdapter:
    """
    Calls Gemini via the Google Generative Language REST API.
    Uses the user's own API key from aistudio.google.com.
    Free tier: 25 requests/day for 2.5 Pro.
    """

    def __init__(self):
        self.conversation_history: list = []

    async def send_message(self, message: str) -> str:
        self.conversation_history.append({
            "role": "user",
            "parts": [{"text": message}]
        })

        payload = {
            "contents": self.conversation_history
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                GEMINI_API_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
                headers={"Content-Type": "application/json"}
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API error {response.status_code}: {response.text[:300]}"
            )

        data = response.json()

        try:
            reply = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Could not parse Gemini response: {data}")

        self.conversation_history.append({
            "role": "model",
            "parts": [{"text": reply}]
        })

        return reply

    def clear_history(self):
        self.conversation_history = []

    def is_authenticated(self) -> bool:
        return bool(GEMINI_API_KEY and GEMINI_API_KEY != "your-key-here")