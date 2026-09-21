"""HTTP adapter for Typesafe AI's "Jev": a classifier model that returns a
probability distribution over a fixed, pre-provisioned set of labels instead
of free text.

Alloy's judge role asks for a JSON object matching :class:`JudgeDecision`,
whose ``decision`` field is a closed five-way enum
(done/retry/consilium/human/abort). That is exactly Jev's "choice" question
type: label the state, return a probability per label. Jev cannot write
``reason``/``next_instructions`` -- those are prose -- so this runner leaves
them at their schema defaults and only fills ``decision`` and ``confidence``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from alloy.models import AgentResult, RunnerUnavailable, prompt_hash, utcnow

DEFAULT_API_BASE = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


def _enum_field(schema: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The first enum-valued property in a JSON schema, if any."""
    for field_name, spec in (schema.get("properties") or {}).items():
        enum_values = spec.get("enum")
        if enum_values:
            return field_name, list(enum_values)
    return None, []


class JevRunner:
    """Calls Jev's ``/v1/systemone`` endpoint for schema-constrained choice
    classification. Only usable for roles whose ``structured_schema`` names
    an enum property (e.g. the judge); anything else raises
    :class:`RunnerUnavailable` rather than guessing.
    """

    name = "jev"
    supports_native_schema = True

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        api_key_file: str | None = None,
        default_model: str | None = None,
        timeout_s: float = 30.0,
        log_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_base = api_base
        self.default_model = default_model or model
        self.api_key = api_key
        self.api_key_file = api_key_file
        self.timeout_s = timeout_s
        self.log_dir = log_dir
        self._transport = transport

    # -- capability ---------------------------------------------------------

    def _resolve_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_file:
            path = Path(self.api_key_file).expanduser()
            if path.exists():
                key = path.read_text(encoding="utf-8").strip()
                if key:
                    return key
        return os.environ.get("TYPESAFE_API_KEY")

    def available(self) -> bool:
        return self._resolve_api_key() is not None

    # -- execution ------------------------------------------------------

    async def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        model: str | None = None,
        timeout: timedelta | None = None,
        structured_schema: dict | None = None,
        on_spawn: Callable[[int], None] | None = None,  # no subprocess: nothing to report
    ) -> AgentResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RunnerUnavailable(
                "jev: no API key (set TYPESAFE_API_KEY, or configure api_key/api_key_file)"
            )
        if not structured_schema:
            raise RunnerUnavailable("jev: requires a structured_schema with an enum property")
        enum_field, labels = _enum_field(structured_schema)
        if enum_field is None:
            raise RunnerUnavailable("jev: structured_schema has no enum-valued property")

        effective_model = model or self.default_model
        digest = prompt_hash(prompt)
        started = utcnow()
        clock = time.monotonic()
        payload = {
            "state": prompt,
            "model": effective_model,
            "questions": {
                enum_field: {
                    "type": "choice",
                    "instructions": (
                        f"Choose the single correct value of '{enum_field}' for this state."
                    ),
                    "criteria": {label: label for label in labels},
                }
            },
        }

        limit_s = (timeout or timedelta(seconds=self.timeout_s)).total_seconds()
        try:
            async with httpx.AsyncClient(timeout=limit_s, transport=self._transport) as client:
                response = await client.post(
                    self.api_base,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            return AgentResult(
                runner=self.name, model=effective_model, ok=False, exit_code=-1,
                text="", structured=None, started_at=started, ended_at=utcnow(),
                duration_s=time.monotonic() - clock, prompt_hash=digest,
                error=f"jev request failed: {exc}",
            )

        duration = time.monotonic() - clock
        ok = response.status_code == 200
        body_text = response.text
        structured: dict[str, Any] | None = None
        usage: dict[str, Any] = {}
        probabilities: dict[str, float] = {}
        if ok:
            try:
                body = response.json()
            except ValueError:
                ok = False
                body = {}
            answer = (body.get("answers") or {}).get(enum_field, {})
            usage = body.get("usage") or {}
            probabilities = answer.get("probabilities") or {}
            structured = {
                enum_field: answer.get("choice"),
                "confidence": answer.get("confidence", 0.0),
            }
            if probabilities:
                # Jev writes no prose; the distribution is the reason. It ends
                # up in the bead note and `alloy run`'s outcome line.
                ranked = sorted(probabilities.items(), key=lambda item: -item[1])
                structured["reason"] = "jev p: " + ", ".join(
                    f"{label} {value:.2f}" for label, value in ranked
                )

        log_path = self._write_log(
            digest, payload, body_text, response.status_code, probabilities, started=started
        )
        return AgentResult(
            runner=self.name,
            model=effective_model,
            ok=ok,
            exit_code=0 if ok else response.status_code,
            text=body_text[:20000],
            structured=structured,
            started_at=started,
            ended_at=utcnow(),
            duration_s=duration,
            usage=usage,
            log_path=log_path,
            prompt_hash=digest,
            error=None if ok else f"jev http {response.status_code}: {body_text[:500]}",
        )

    def _write_log(
        self,
        digest: str,
        payload: dict[str, Any],
        body_text: str,
        status: int,
        probabilities: dict[str, float],
        *,
        started=None,
    ) -> str | None:
        if self.log_dir is None:
            return None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = int((started.timestamp() if started else time.time()) * 1000)
        path = self.log_dir / f"{stamp}-jev-{digest}.json"
        record = {
            "runner": self.name,
            "request": payload,
            "status": status,
            "response": body_text,
            "probabilities": probabilities,
        }
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return str(path)
