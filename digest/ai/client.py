from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class AIConfigurationError(RuntimeError):
    """Foundry rejected the request because of credentials, endpoint, or model configuration."""


class DigestAI:
    def __init__(self) -> None:
        self.endpoint = os.getenv("FOUNDRY_ENDPOINT")
        self.model = os.getenv("FOUNDRY_MODEL")
        api_key = os.getenv("FOUNDRY_API_KEY")
        self._api_key = api_key or ""
        self._client: Any = None
        if self.endpoint:
            if not self.model or not api_key:
                raise ValueError(
                    "FOUNDRY_MODEL and FOUNDRY_API_KEY are required when FOUNDRY_ENDPOINT is set"
                )
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(base_url=self.endpoint, api_key=api_key)

    def _describe_target(self) -> str:
        parts = urlsplit(self.endpoint or "")
        return f"endpoint host={parts.netloc or '?'} path={parts.path or '/'} model={self.model}"

    async def json_completion(self, system: str, user: str) -> dict[str, Any]:
        if self._client is None:
            return {}
        from openai import AuthenticationError, NotFoundError, PermissionDeniedError

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as exc:
            detail = str(exc.message)
            if self._api_key:
                detail = detail.replace(self._api_key, "***")
            raise AIConfigurationError(
                f"Foundry rejected the request with HTTP {exc.status_code} "
                f"({self._describe_target()}). "
                "Check that FOUNDRY_API_KEY belongs to the same resource as FOUNDRY_ENDPOINT, "
                "that FOUNDRY_ENDPOINT is the OpenAI v1 route "
                "(https://<resource>.openai.azure.com/openai/v1/), and that FOUNDRY_MODEL is "
                f"the deployment name. Detail: {detail}"
            ) from exc
        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("model response must be a JSON object")
        return result

    async def preflight(self) -> None:
        """Fail fast on configuration errors before spending calls on every item."""
        if self._client is None:
            return
        await self.json_completion(
            "You are a health check. Reply with the JSON object {\"ok\": true}.",
            "Return json.",
        )
        logger.warning("Foundry preflight succeeded (%s)", self._describe_target())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
