from __future__ import annotations

import os
from typing import Any


class DigestAI:
    def __init__(self) -> None:
        self.endpoint = os.getenv("FOUNDRY_ENDPOINT")
        self.model = os.getenv("FOUNDRY_MODEL")
        api_key = os.getenv("FOUNDRY_API_KEY")
        self._client: Any = None
        if self.endpoint:
            if not self.model or not api_key:
                raise ValueError(
                    "FOUNDRY_MODEL and FOUNDRY_API_KEY are required when FOUNDRY_ENDPOINT is set"
                )
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(base_url=self.endpoint, api_key=api_key)

    async def json_completion(self, system: str, user: str) -> dict[str, Any]:
        if self._client is None:
            return {}
        response = await self._client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
        )
        import json

        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("model response must be a JSON object")
        return result

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
