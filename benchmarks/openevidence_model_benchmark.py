"""Live sidecar model comparison; no production cache reads or writes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from time import perf_counter
from unittest.mock import patch

import httpx
from tenacity import RetryError

from benchmarks.run_openevidence_benchmark import GENES, write_json
from src.config import settings
from src.pipeline import openevidence as oe


class CallBudgetExceeded(RuntimeError):
    pass


_ORPHAN_TAIL_KEY = '", "kind": "'


def _is_pending_orphan_tail(text: str) -> bool:
    """Whether `text` may be the start of a headless widget tail still streaming.

    A complete tail is removed by oe._strip_generation_step_widgets; an
    incomplete one (or a fragment cut before its `", "kind": "` key) is kept.
    """
    if oe._ORPHAN_WIDGET_TAIL_START.match(text):
        return True
    hex_prefix = re.match(r"[0-9a-fA-F-]{1,36}", text)
    return bool(hex_prefix) and _ORPHAN_TAIL_KEY.startswith(text[hex_prefix.end():])


def has_prose(text: str) -> bool:
    """Whether accumulated OpenEvidence answer text contains real prose yet.

    Widgets are split across SSE events at arbitrary points, so this is
    judged on the joined text so far, not per event. After the shared widget
    strip, a marker whose JSON decodes is dropped; one whose JSON is still
    open truncates the text there (rest of the widget hasn't arrived).
    """
    stripped = oe._strip_generation_step_widgets(text)
    kept, position = [], 0
    while (start := stripped.find(oe._GENERATION_STEP_MARKER, position)) >= 0:
        kept.append(stripped[position:start])
        try:
            _, position = oe._JSON_DECODER.raw_decode(
                stripped, start + len(oe._GENERATION_STEP_MARKER))
        except json.JSONDecodeError:
            position = len(stripped)
    kept.append(stripped[position:])
    remainder = oe._CITATION_MARKER_PATTERN.sub("", "".join(kept)).strip()
    if oe._GENERATION_STEP_MARKER in text and _is_pending_orphan_tail(remainder):
        return False
    return bool(remainder)


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream, attempt):
        self.stream = stream
        self.attempt = attempt

    async def __aiter__(self):
        buffered = b""
        text_parts = []
        async for chunk in self.stream:
            if self.attempt["first_byte_seconds"] is None:
                self.attempt["first_byte_seconds"] = perf_counter() - self.attempt.pop("start_byte")
            buffered = (buffered + chunk).replace(b"\r\n", b"\n")
            # Only complete SSE events count; TTFT is the first one after which the
            # accumulated text (widgets excluded) holds prose — see has_prose.
            complete = buffered.rsplit(b"\n\n", 1)
            events = []
            if len(complete) == 2:
                buffered = complete[1]
                for payload in oe._iter_sse_payloads(complete[0].decode("utf-8", errors="replace")):
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
            if self.attempt["ttft_seconds"] is None:
                for event in events:
                    if event.get("text") and "table" not in event:
                        text_parts.append(event["text"])
                if events and has_prose("".join(text_parts)):
                    self.attempt["ttft_seconds"] = perf_counter() - self.attempt["start"]
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class ObservedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport, data, row, target):
        self.transport, self.data, self.row, self.target = transport, data, row, target

    async def handle_async_request(self, request):
        if self.data["paid_call_attempts"] >= 45:
            raise CallBudgetExceeded("45-call hard cap reached")
        self.data["paid_call_attempts"] += 1
        attempt = {"number": self.data["paid_call_attempts"], "ttft_seconds": None,
                   "first_byte_seconds": None, "start": perf_counter(),
                   "start_byte": perf_counter()}
        self.row["attempts"].append(attempt)
        write_json(self.target, self.data)
        try:
            response = await self.transport.handle_async_request(request)
            attempt["http_status"] = response.status_code
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                attempt["error_message"] = body.replace(settings.openevidence_api_key, "[SECRET]")
            response.stream = ObservedStream(response.stream, attempt)
            return response
        except httpx.HTTPError as exc:
            attempt["transport_error"] = type(exc).__name__
            raise

    async def aclose(self):
        await self.transport.aclose()


def core_evidence(gene, source):
    if gene == "EML4::ALK":
        annotations = json.loads(source.with_name("eml4_alk_fusion_qualitative.json").read_text())[
            "annotations"]
    else:
        annotations = [json.loads(source.read_text())["per_gene"][gene]["annotation"]]
    return {
        "pmids": sorted({p for a in annotations for p in a["citations"]}),
        "titles": sorted({c["title"] for a in annotations for c in a["evidence_cards"]}),
    }


async def run_models(output: Path, timeout: float = 900, concurrency: int = 3,
                     models=("osler", "darwin"), genes=None):
    if timeout < 600 or not 1 <= concurrency <= 3:
        raise ValueError("Require timeout >=600s and concurrency 1..3")
    if not settings.openevidence_api_key.strip():
        raise SystemExit("OpenEvidence credential missing")
    genes = genes or ["EGFR"] + [g for g in GENES if g != "EGFR"] + ["EML4::ALK"]
    output.mkdir(parents=True, exist_ok=True)
    target = output / "models.json"
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {target}")
    source = Path("benchmarks/results/openevidence_pointed_20260908/disabled.json")
    data = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
            "genes": genes, "models": list(models), "paid_call_attempts": 0,
            "timeout_seconds": timeout, "concurrency": concurrency,
            "cache_policy": "OpenEvidence cached_call bypassed; no Redis access",
            "core_evidence_source": str(source), "per_gene": {}}
    old_model, old_timeout = settings.openevidence_model, settings.openevidence_timeout_seconds
    settings.openevidence_timeout_seconds = timeout

    async def live_cache(key, compute, ttl_seconds=None):
        return await compute()

    async def one(gene, model):
        evidence = core_evidence(gene, source)
        row = {"attempts": [], "core_evidence": evidence}
        data["per_gene"].setdefault(gene, {})[model] = row
        started = perf_counter()
        transport = ObservedTransport(httpx.AsyncHTTPTransport(), data, row, target)
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                analysis = await oe.OpenEvidenceClient().get_gene_analysis(
                    "ALK" if "::" in gene else gene,
                    fusion=gene if "::" in gene else None, client=client)
            distilled = oe.distill_openevidence(analysis)
            additive = oe.distill_additive_openevidence(
                analysis, core_pmids=evidence["pmids"], core_titles=evidence["titles"])
            row.update(status="success", analysis=analysis.model_dump(),
                       distilled=distilled.model_dump(), additive=additive.model_dump(),
                       answer_chars=len(analysis.text), answer_words=len(analysis.text.split()))
        except Exception as exc:
            underlying = exc.last_attempt.exception() if isinstance(exc, RetryError) else exc
            row.update(status="timeout" if isinstance(underlying, (httpx.TimeoutException,
                       TimeoutError)) else "error", error_type=type(underlying).__name__)
            if isinstance(underlying, CallBudgetExceeded):
                raise
            if model == "osler" and any(400 <= a.get("http_status", 0) < 500
                                        for a in row["attempts"]):
                data["status"] = "osler_rejected"
                raise RuntimeError("osler rejected; see saved HTTP status and error_message") from None
        finally:
            row["wall_seconds"] = perf_counter() - started
            row["ttft_seconds"] = next((a["ttft_seconds"] for a in row["attempts"]
                                        if a["ttft_seconds"] is not None), None)
            for a in row["attempts"]:
                a.pop("start", None)
                a.pop("start_byte", None)
            write_json(target, data)
            print(f'{model} {gene}: {row.get("status", "error")} '
                  f'{row["wall_seconds"]:.1f}s; calls={data["paid_call_attempts"]}', flush=True)

    try:
        with patch.object(oe, "cached_call", live_cache):
            for model in models:
                settings.openevidence_model = model
                # Probe before spending on the remaining genes.
                await one(genes[0], model)
                for start in range(1, len(genes), concurrency):
                    tasks = [asyncio.create_task(one(gene, model))
                             for gene in genes[start:start + concurrency]]
                    try:
                        await asyncio.gather(*tasks)
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
        data["status"] = "complete"
    except BaseException:
        if data["status"] == "running":
            data["status"] = "failed"
        raise
    finally:
        settings.openevidence_model, settings.openevidence_timeout_seconds = old_model, old_timeout
        data["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(target, data)
