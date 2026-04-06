import httpx
from pathlib import Path

GEMINI_API_KEY = "AIzaSyAOPxgfhNMS_LDrUfhJel5R9TjEPowbZ6U"
GEMINI_MODEL = "gemini-3-flash-preview"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


class GeminiAdapter:
    """
    Calls Gemini via the Google Generative Language REST API.
    History is rebuilt from DB on session resume.
    """

    def __init__(self):
        self.conversation_history: list = []

    def load_history(self, history: list[dict]):
        """
        Rebuild internal history from DB messages.
        history: list of {"role": "user"|"assistant", "content": str}
        Converts to Gemini format: role "assistant" → "model"
        """
        self.conversation_history = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}]
            }
            for m in history
        ]

    async def send_message(self, message: str, history: list[dict] = None) -> str:
        # Rebuild from DB if adapter history is empty (restart or first use)
        if not self.conversation_history and history:
            self.load_history(history)

        self.conversation_history.append({
            "role": "user",
            "parts": [{"text": message}]
        })

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                GEMINI_API_URL,
                params={"key": GEMINI_API_KEY},
                json={"contents": self.conversation_history},
                headers={"Content-Type": "application/json"}
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API error {response.status_code}: {response.text[:300]}"
            )

        data = response.json()

        try:
            reply = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
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