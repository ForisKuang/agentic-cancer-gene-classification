"""Verify benchmark accounting independently of live API tests."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from benchmarks import run_openevidence_benchmark as benchmark
from src.models.schema import AnnotationResult, GeneAnnotation


async def test_concurrent_usage_attribution_and_live_oe_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark.settings, "anthropic_api_key", "test")
    monkeypatch.setattr(benchmark.settings, "openevidence_api_key", "test")
    monkeypatch.setattr(benchmark.llm_client, "record_llm_usage", lambda *args: None)
    monkeypatch.setattr(benchmark.settings, "openevidence_enabled", False)
    monkeypatch.setattr(benchmark.settings, "openevidence_timeout_seconds", 60)
    computes = []

    async def compute():
        computes.append(True)
        return {"value": len(computes)}

    async def annotate(gene):
        await asyncio.sleep(0 if gene == "B" else .01)
        reporter = (benchmark.literature if gene == "B" else benchmark.llm_client)
        reporter.record_llm_usage("test-model", "synthesis", SimpleNamespace(
            input_tokens=10 if gene == "A" else 20, output_tokens=3,
            cache_read_input_tokens=4, cache_creation_input_tokens=5,
        ))
        return GeneAnnotation(gene=gene)

    async def pipeline(genes, force_refresh, on_annotation):
        assert force_refresh
        # Reference cache is cold and isolated, OE always recomputes.
        first = await benchmark.literature.cached_call("same", compute)
        second = await benchmark.literature.cached_call("same", compute)
        assert first == second
        await benchmark.openevidence.cached_call("same", compute)
        await benchmark.openevidence.cached_call("same", compute)
        assert len(computes) == 3
        annotations = await asyncio.gather(*(
            benchmark.orchestrator._annotate_gene(gene=g) for g in ("A", "B")
        ))
        for annotation in annotations:
            await on_annotation(annotation)
        return AnnotationResult(run_id="test", timestamp="test", fusions_processed=2,
                                genes_annotated=2, annotations=annotations)

    monkeypatch.setattr(benchmark.orchestrator, "_annotate_gene", annotate)
    monkeypatch.setattr(benchmark.orchestrator, "run_pipeline", pipeline)
    await benchmark.run("enabled", tmp_path, 900)
    data = json.loads((tmp_path / "enabled.json").read_text())
    assert data["status"] == "complete"
    assert data["per_gene"]["A"]["llm_calls"][0]["input_tokens"] == 10
    assert data["per_gene"]["B"]["llm_calls"][0]["input_tokens"] == 20
    assert data["per_gene"]["A"]["llm_calls"][0]["cache_read_input_tokens"] == 4
    assert data["per_gene"]["B"]["annotation"]["gene"] == "B"
    assert benchmark.CURRENT_GENE.get() == "unattributed"


async def test_existing_run_is_never_overwritten(tmp_path):
    target = tmp_path / "disabled.json"
    target.write_text('{"sentinel": true}')
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        await benchmark.run("disabled", tmp_path, 900)
    assert json.loads(target.read_text()) == {"sentinel": True}


def test_token_accounting_includes_cache_input_and_escalation():
    from benchmarks.compare_openevidence import tokens
    result = tokens([
        {"input_tokens": 10, "cache_creation_input_tokens": 100,
         "cache_read_input_tokens": 0, "output_tokens": 20},
        {"input_tokens": 30, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 200, "output_tokens": 40},
    ])
    assert result["total_input_tokens"] == 340
    assert result["output_tokens"] == 60
    assert result["calls"] == 2


def test_comparison_rejects_incomplete_arms():
    from benchmarks.compare_openevidence import compare
    with pytest.raises(ValueError, match="Both arms must be complete"):
        compare({"status": "running"}, {"status": "complete"}, {})


async def test_model_benchmark_uses_live_path_and_saved_core_evidence(tmp_path, monkeypatch):
    import httpx
    from benchmarks import openevidence_model_benchmark as models

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=httpx.ByteStream(
            b'data: {"text": "FLAURA improves PFS. [[1]]"}\n\n'))

    monkeypatch.setattr(models.httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(respond))
    monkeypatch.setattr(models.settings, "openevidence_api_key", "test-only-key")
    original_model = models.settings.openevidence_model
    original_timeout = models.settings.openevidence_timeout_seconds
    cached_hits = []

    async def seeded_cache_hit(*args, **kwargs):
        cached_hits.append(True)
        return {"text": "Cached answer must not be reused"}

    monkeypatch.setattr(models.oe, "cached_call", seeded_cache_hit)
    await models.run_models(tmp_path, genes=["EGFR", "EML4::ALK"])
    data = json.loads((tmp_path / "models.json").read_text())
    assert data["status"] == "complete"
    assert data["paid_call_attempts"] == 4
    assert [r["model"] for r in requests] == ["osler", "osler", "darwin", "darwin"]
    assert "EML4::ALK fusion" in requests[1]["text"]
    row = data["per_gene"]["EGFR"]["osler"]
    assert row["ttft_seconds"] is not None
    assert row["core_evidence"]["pmids"]
    assert row["distilled"]["trial_mentions"][0]["trial"] == "FLAURA"
    assert models.settings.openevidence_model == original_model
    assert models.settings.openevidence_timeout_seconds == original_timeout
    assert models.oe.cached_call is seeded_cache_hit
    assert cached_hits == []
    assert "test-only-key" not in (tmp_path / "models.json").read_text()


async def test_model_rejection_stops_before_other_paid_calls(tmp_path, monkeypatch):
    import httpx
    from benchmarks import openevidence_model_benchmark as models

    monkeypatch.setattr(models.settings, "openevidence_api_key", "test-only-key")
    monkeypatch.setattr(models.httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(400, text='{"error":"unknown model osler"}')))
    with pytest.raises(RuntimeError, match="osler rejected"):
        await models.run_models(tmp_path, genes=["EGFR", "TP53"])
    data = json.loads((tmp_path / "models.json").read_text())
    assert data["paid_call_attempts"] == 1
    assert data["status"] == "osler_rejected"
    attempt = data["per_gene"]["EGFR"]["osler"]["attempts"][0]
    assert attempt["http_status"] == 400
    assert "unknown model osler" in attempt["error_message"]


async def test_model_benchmark_budget_counts_transport_attempts(tmp_path, monkeypatch):
    import httpx
    from benchmarks import openevidence_model_benchmark as models

    data = {"paid_call_attempts": 45}
    row = {"attempts": []}
    transport = models.ObservedTransport(httpx.MockTransport(
        lambda request: pytest.fail("must not send request")), data, row, tmp_path / "data.json")
    with pytest.raises(models.CallBudgetExceeded):
        await transport.handle_async_request(httpx.Request("POST", "https://example.com"))
    assert data["paid_call_attempts"] == 45


def test_model_comparison_distinguishes_missing_reference_from_zero_recall():
    from benchmarks.compare_openevidence import compare_models, percentile
    base = {"status": "success", "wall_seconds": 12, "ttft_seconds": 2,
            "answer_chars": 30, "analysis": {"text": "FLAURA-2 and NCT04988295"},
            "distilled": {"guidelines": [], "trial_mentions": [], "citation_count": 3},
            "additive": {"citation_count": 2}}
    candidate = {**base, "analysis": {"text": "No named trials."}}
    data = {"status": "complete", "models": ["osler", "darwin"], "genes": ["EGFR"],
            "paid_call_attempts": 2, "per_gene": {"EGFR": {"osler": candidate, "darwin": base}}}
    result = compare_models(data)
    row = result["per_gene"][0]
    assert row["guidelines_agreement"]["darwin_reference_recall"] is None
    assert row["trials_agreement"]["darwin_reference_recall"] == 0
    assert "FLAURA2" in row["trials_agreement"]["reference"]
    assert result["summary"]["osler"]["mean_additive_citations"] == 2
    assert percentile([1, 11], .9) == 10


async def test_observed_stream_excludes_widgets_and_partial_events():
    import httpx
    from time import perf_counter
    from benchmarks.openevidence_model_benchmark import ObservedStream

    class SplitStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"text": "REACTCOMPONENT!:!InlineGenerationStep!:!{}"}\n\n'
            assert attempt["ttft_seconds"] is None
            yield b'data: {"text": "[[1]]"}\n\n'
            assert attempt["ttft_seconds"] is None
            yield b'data: {"text": "Real'
            assert attempt["ttft_seconds"] is None
            yield b' answer."}\r\n\r\n'

        async def aclose(self):
            pass

    attempt = {"start": perf_counter(), "start_byte": perf_counter(),
               "first_byte_seconds": None, "ttft_seconds": None}
    observed = ObservedStream(SplitStream(), attempt)
    chunks = [chunk async for chunk in observed]
    assert len(chunks) == 4
    assert attempt["ttft_seconds"] is not None
    assert attempt["first_byte_seconds"] <= attempt["ttft_seconds"]


async def test_blinded_judge_withholds_model_names_and_disables_retries(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from benchmarks import judge_openevidence_models as judge_module

    pair = {m: {"analysis": {"question": "Question", "text": "An evidence answer",
                           "citations": [{"title": "Citation", "url": "https://example.com",
                                          "source_texts": ["Long source quote"]}]},
                "wall_seconds": 10} for m in ("osler", "darwin")}
    (tmp_path / "models.json").write_text(json.dumps({
        "status": "complete", "paid_call_attempts": 2, "genes": ["EGFR"],
        "per_gene": {"EGFR": pair}}))

    class Client:
        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def messages(self):
            return self

        async def create(self, **kwargs):
            content = kwargs["messages"][0]["content"]
            assert "osler" not in content and "darwin" not in content
            assert "wall_seconds" not in content
            assert "source_texts" not in content and "Long source quote" not in content
            assert "https://example.com" in content
            score = {"factual_correctness": 4, "specificity": 4, "hallucination_safety": 4}
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
                "per_gene": [{"gene": "EGFR", "A": score, "B": score,
                              "comparison": "Similar", "spot_check_flags": "None"}]})],
                usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 1}))

    monkeypatch.setattr(judge_module, "make_async_sdk_client", Client)
    await judge_module.judge(tmp_path)
    result = json.loads((tmp_path / "blinded_judge.json").read_text())
    assert result["paid_judge_calls"] == 1
    assert set(result["mapping"]["EGFR"].values()) == {"osler", "darwin"}
    ledger = tmp_path / "judge_attempts.json"
    recorded = json.loads(ledger.read_text())
    assert recorded["total_benchmark_api_attempts"] == 3
    assert recorded["attempts"][0]["result_stem"] == "blinded_judge"
    ledger.write_text(json.dumps({"attempts": [{"status": "rejected"}] * 43}))
    with pytest.raises(SystemExit, match="Insufficient remaining paid-call budget"):
        await judge_module.judge(tmp_path, suffix="over_budget")
    assert not (tmp_path / "blinded_judge_over_budget.json").exists()


@pytest.mark.parametrize("wrap,timeout", [(False, True), (True, True), (True, False)])
async def test_model_error_classification_unwraps_retry_errors(tmp_path, monkeypatch, wrap, timeout):
    import httpx
    from concurrent.futures import Future
    from tenacity import RetryError
    from benchmarks import openevidence_model_benchmark as models

    error = httpx.ReadTimeout("fixture timeout") if timeout else httpx.ConnectError("fixture error")
    if wrap:
        future = Future()
        future.set_exception(error)
        error = RetryError(future)

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(models.settings, "openevidence_api_key", "test-only-key")
    monkeypatch.setattr(models.oe, "_post_streaming_analysis", fail)
    await models.run_models(tmp_path, genes=["EGFR"], models=("darwin",))
    row = json.loads((tmp_path / "models.json").read_text())["per_gene"]["EGFR"]["darwin"]
    assert row["status"] == ("timeout" if timeout else "error")
    assert row["error_type"] == ("ReadTimeout" if timeout else "ConnectError")


def test_judge_guard_counts_all_recorded_attempts_and_reserves_last_slot(tmp_path):
    from benchmarks.judge_openevidence_models import reserve_judge_attempt

    source = {"paid_call_attempts": 34}
    ledger = tmp_path / "judge_attempts.json"
    ledger.write_text(json.dumps({"attempts": [{"status": "rejected"}] * 10}))
    reserve_judge_attempt(tmp_path, source, "last_slot", "fixture-model")
    saved = json.loads(ledger.read_text())
    assert saved["total_judge_attempts"] == 11
    assert saved["total_benchmark_api_attempts"] == 45
    assert saved["attempts"][-1]["status"] == "attempted"
    with pytest.raises(SystemExit, match="Insufficient remaining paid-call budget"):
        reserve_judge_attempt(tmp_path, source, "must_not_call", "fixture-model")
    assert json.loads(ledger.read_text()) == saved


def test_trial_labels_exclude_ordinary_words_and_ambiguous_drug_names():
    from benchmarks.compare_openevidence import compare_models

    raw = {"status": "success", "wall_seconds": 12, "ttft_seconds": 2,
           "answer_chars": 30, "analysis": {"text":
               "toxicity profile; PROFILE; solo; SOLO; PRIMA-1; ARROW; crown; CROWN; NCT04988295"},
           "distilled": {"guidelines": [], "trial_mentions": [], "citation_count": 0},
           "additive": {"citation_count": 0}}
    data = {"status": "complete", "models": ["osler", "darwin"],
            "genes": ["EGFR", "AIRE"], "paid_call_attempts": 4,
            "per_gene": {gene: {"osler": raw, "darwin": raw} for gene in ("EGFR", "AIRE")}}
    result = compare_models(data)
    assert result["per_gene"][0]["trials_agreement"]["reference"] == ["CROWN", "NCT04988295"]
    assert result["groups"]["established"]["genes"] == ["EGFR"]
    assert result["groups"]["negative_controls"]["genes"] == ["AIRE"]
    assert result["groups"]["established"]["agreement"]["trials"]["micro_recall"] == 1
    assert result["groups"]["negative_controls"]["card_guideline_totals"]["darwin"] == 0
