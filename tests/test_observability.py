import asyncio
import logging
from contextlib import contextmanager

from fastapi.testclient import TestClient

from src import main, observability
from src.main import app
from src.models.schema import AnnotationResult, GeneAnnotation, ResolvedGene
from src.observability import NoopSpan, gene_latency_tag, record_user_seen, stable_user_key
from src.pipeline import llm_client, orchestrator


class FakeStatsd:
    def __init__(self):
        self.calls = []

    def increment(self, metric, value=1, tags=None):
        self.calls.append(("increment", metric, value, tags))

    def distribution(self, metric, value, tags=None):
        self.calls.append(("distribution", metric, value, tags))

    def set(self, metric, value, tags=None):
        self.calls.append(("set", metric, value, tags))


@contextmanager
def noop_trace(*args, **kwargs):
    yield NoopSpan()


def test_record_user_seen_hashes_user_identifier(monkeypatch):
    fake_statsd = FakeStatsd()
    monkeypatch.setattr("src.observability.settings.datadog_metrics_enabled", True)
    monkeypatch.setattr("src.observability._statsd_client", fake_statsd)

    record_user_seen("User@Example.com", tags=["mode:core"])

    assert fake_statsd.calls == [
        ("set", "users.active", stable_user_key("User@Example.com"), ["mode:core"])
    ]
    assert "User@Example.com" not in str(fake_statsd.calls)


def test_record_user_seen_counts_anonymous_requests(monkeypatch):
    fake_statsd = FakeStatsd()
    monkeypatch.setattr("src.observability.settings.datadog_metrics_enabled", True)
    monkeypatch.setattr("src.observability._statsd_client", fake_statsd)

    record_user_seen(None, tags=["mode:full"])

    assert fake_statsd.calls == [
        ("increment", "users.anonymous_requests", 1, ["mode:full"])
    ]


async def test_run_pipeline_emits_gene_and_input_metrics(monkeypatch):
    metric_calls = []

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (
                ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True),
                ["BRAF::TP53"],
            ),
            "TP53": (
                ResolvedGene(input_symbol="TP53", canonical_symbol="TP53", resolved=True),
                ["BRAF::TP53"],
            ),
        }

    async def fake_annotate_gene(*, gene, **kwargs):
        return GeneAnnotation(gene=gene, timings_ms={"total": 5.0})

    def fake_increment(metric, value=1, tags=None):
        metric_calls.append(("increment", metric, value, tags))

    def fake_distribution(metric, value, tags=None):
        metric_calls.append(("distribution", metric, value, tags))

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator, "increment", fake_increment)
    monkeypatch.setattr(orchestrator, "distribution", fake_distribution)
    monkeypatch.setattr(orchestrator, "trace", noop_trace)

    result = await orchestrator.run_pipeline(["BRAF::TP53"], mode="core")

    assert result.genes_annotated == 2
    tags = ["mode:core", "local_backend:sdk", "skip_literature_for_oncokb:False"]
    assert ("increment", "pipeline.runs", 1, tags) in metric_calls
    assert ("increment", "inputs.submitted", 1, tags) in metric_calls
    assert ("increment", "genes.queried", 2, tags) in metric_calls
    assert any(call[1] == "pipeline.duration_ms" for call in metric_calls)


async def test_run_pipeline_tags_gene_metrics_with_cache_status_and_fusion(monkeypatch):
    metric_calls = []

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (
                ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True),
                ["BRAF::TP53"],
            ),
            "MYH9": (
                ResolvedGene(input_symbol="MYH9", canonical_symbol="MYH9", resolved=True),
                ["MYH9"],
            ),
        }

    async def fake_annotate_gene(*, gene, fusions, **kwargs):
        # Real _annotate_gene/synthesis code always sets .fusions from the
        # fusions it was called with (see orchestrator.py's own branches) —
        # mirror that contract here rather than leaving it at the model default.
        return GeneAnnotation(
            gene=gene,
            fusions=list(fusions),
            timings_ms={"total": 5.0},
            cache_status="refreshed",
        )

    def fake_increment(metric, value=1, tags=None):
        metric_calls.append(("increment", metric, value, tags))

    def fake_distribution(metric, value, tags=None):
        metric_calls.append(("distribution", metric, value, tags))

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator, "increment", fake_increment)
    monkeypatch.setattr(orchestrator, "distribution", fake_distribution)
    monkeypatch.setattr(orchestrator, "trace", noop_trace)

    await orchestrator.run_pipeline(["BRAF::TP53", "MYH9"], mode="core")

    base_tags = ["mode:core", "local_backend:sdk", "skip_literature_for_oncokb:False"]
    fusion_gene_tags = base_tags + ["cache_status:refreshed", "is_fusion:True"]
    non_fusion_gene_tags = base_tags + ["cache_status:refreshed", "is_fusion:False"]
    assert ("increment", "genes.annotated", 1, fusion_gene_tags) in metric_calls
    assert ("increment", "genes.annotated", 1, non_fusion_gene_tags) in metric_calls
    assert any(
        call[1] == "gene.total_duration_ms"
        and call[3] == fusion_gene_tags + [gene_latency_tag("BRAF")]
        for call in metric_calls
    )
    assert any(
        call[1] == "gene.total_duration_ms"
        and call[3] == non_fusion_gene_tags + [gene_latency_tag("MYH9")]
        for call in metric_calls
    )


async def test_gene_total_duration_excludes_concurrency_slot_wait(monkeypatch):
    """With N genes and a concurrency limit below N, genes that queue for a
    slot must not have that wait folded into gene.total_duration_ms."""
    metric_calls = []
    genes = [f"GENE{i}" for i in range(6)]
    work_s = 0.1

    async def fake_normalize_fusions(inputs):
        return {
            gene: (ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True), [gene])
            for gene in genes
        }

    async def fake_annotate_gene(*, gene, fusions, **kwargs):
        await asyncio.sleep(work_s)
        return GeneAnnotation(gene=gene, fusions=list(fusions), cache_status="refreshed")

    monkeypatch.setattr(orchestrator.settings, "annotation_gene_concurrency", 2)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator, "increment", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "distribution",
        lambda metric, value, tags=None: metric_calls.append((metric, value, tags)),
    )
    monkeypatch.setattr(orchestrator, "trace", noop_trace)

    await orchestrator.run_pipeline(genes, mode="core")

    totals = [value for metric, value, _ in metric_calls if metric == "gene.total_duration_ms"]
    waits = sorted(value for metric, value, _ in metric_calls if metric == "gene.queue_wait_ms")
    assert len(totals) == len(genes)
    assert len(waits) == len(genes)
    # 6 genes / 2 slots = 3 waves of ~100ms. Previously the last wave reported
    # ~300ms; each gene's own work is ~100ms regardless of its position.
    for total in totals:
        assert work_s * 1000 * 0.9 <= total < work_s * 1000 * 1.8
    # Wait time is still captured: 2 genes start immediately, 2 wait ~1 wave, 2 wait ~2 waves.
    assert waits[0] < 20 and waits[1] < 20
    assert all(wait >= work_s * 1000 * 0.9 for wait in waits[2:4])
    assert all(wait >= work_s * 1000 * 1.8 for wait in waits[4:6])
    # Queue wait uses the same tags as total duration.
    total_tags = sorted(tuple(tags) for metric, _, tags in metric_calls if metric == "gene.total_duration_ms")
    wait_tags = sorted(tuple(tags) for metric, _, tags in metric_calls if metric == "gene.queue_wait_ms")
    assert total_tags == wait_tags


async def test_cache_hit_gene_emits_no_queue_wait(monkeypatch):
    metric_calls = []

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True), ["BRAF"]),
        }

    async def fake_reuse_cached_annotation(*, gene, fusions, **kwargs):
        return GeneAnnotation(gene=gene, fusions=list(fusions), cache_status="reused")

    async def fail_annotate_gene(**kwargs):
        raise AssertionError("cache hit should not annotate")

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_maybe_reuse_cached_annotation", fake_reuse_cached_annotation)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_annotate_gene)
    monkeypatch.setattr(orchestrator, "increment", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "distribution",
        lambda metric, value, tags=None: metric_calls.append((metric, value, tags)),
    )
    monkeypatch.setattr(orchestrator, "trace", noop_trace)

    await orchestrator.run_pipeline(["BRAF"], mode="core")

    metrics = [metric for metric, _, _ in metric_calls]
    assert "gene.total_duration_ms" in metrics
    assert "gene.queue_wait_ms" not in metrics


async def test_gene_annotation_duration_tagged_with_cache_status_fusion_and_gene(monkeypatch):
    metric_calls = []
    monkeypatch.setattr(
        orchestrator,
        "distribution",
        lambda metric, value, tags=None: metric_calls.append((metric, value, tags)),
    )
    monkeypatch.setattr(observability.settings, "datadog_gene_latency_watchlist", "ALK")

    await orchestrator._annotate_gene(
        gene="ALK",
        fusions=["EML4::ALK"],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=False),
        unresolvable=True,
        mode="core",
    )

    assert metric_calls == [
        (
            "gene.annotation.duration_ms",
            metric_calls[0][1],
            [
                "mode:core",
                "local_backend:sdk",
                "tumor_type_present:False",
                "skip_literature_for_oncokb:False",
                "cache_status:bypassed",
                "is_fusion:True",
                "gene:ALK",
            ],
        )
    ]


def test_gene_latency_tag_buckets_watchlisted_gene(monkeypatch):
    monkeypatch.setattr(observability.settings, "datadog_gene_latency_watchlist", "ALK,ROS1")

    assert gene_latency_tag("alk") == "gene:ALK"


def test_gene_latency_tag_buckets_unlisted_gene_as_other(monkeypatch):
    monkeypatch.setattr(observability.settings, "datadog_gene_latency_watchlist", "ALK,ROS1")

    assert gene_latency_tag("MYH9") == "gene:other"


def test_record_llm_usage_emits_request_count_and_token_distributions(monkeypatch):
    metric_calls = []
    monkeypatch.setattr(
        llm_client, "increment", lambda metric, value=1, tags=None: metric_calls.append(("increment", metric, value, tags))
    )
    monkeypatch.setattr(
        llm_client, "distribution", lambda metric, value, tags=None: metric_calls.append(("distribution", metric, value, tags))
    )

    class FakeUsage:
        input_tokens = 1200
        output_tokens = 340
        cache_creation_input_tokens = 500
        cache_read_input_tokens = 2000

    llm_client.record_llm_usage("claude-haiku-4-5-20251001", "selection", FakeUsage())

    tags = ["model:claude-haiku-4-5-20251001", "model_purpose:selection"]
    assert ("increment", "llm.requests", 1, tags) in metric_calls
    assert ("distribution", "llm.tokens.input", 1200, tags) in metric_calls
    assert ("distribution", "llm.tokens.output", 340, tags) in metric_calls
    assert ("distribution", "llm.tokens.cache_creation", 500, tags) in metric_calls
    assert ("distribution", "llm.tokens.cache_read", 2000, tags) in metric_calls


def test_record_llm_usage_tags_unspecified_purpose_and_handles_missing_usage(monkeypatch):
    metric_calls = []
    monkeypatch.setattr(
        llm_client, "increment", lambda metric, value=1, tags=None: metric_calls.append(("increment", metric, value, tags))
    )
    monkeypatch.setattr(
        llm_client, "distribution", lambda metric, value, tags=None: metric_calls.append(("distribution", metric, value, tags))
    )

    llm_client.record_llm_usage("claude-opus-4-7", "", None)

    tags = ["model:claude-opus-4-7", "model_purpose:unspecified"]
    assert metric_calls == [("increment", "llm.requests", 1, tags)]


async def test_complete_sdk_records_llm_usage(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        llm_client, "record_llm_usage", lambda model, purpose, usage: recorded.update(
            model=model, purpose=purpose, usage=usage
        )
    )

    class FakeUsage:
        input_tokens = 10
        output_tokens = 5
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    class FakeToolUseBlock:
        type = "tool_use"
        name = "draft_feedback_issue"
        input = {"title": "ok"}

    class FakeResponse:
        content = [FakeToolUseBlock()]
        usage = FakeUsage()

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm_client, "make_async_sdk_client", lambda: FakeClient())

    result = await llm_client._complete_sdk(
        model="claude-haiku-4-5-20251001",
        system="sys",
        user="user",
        tool={"name": "draft_feedback_issue"},
        max_tokens=100,
        model_purpose="selection",
    )

    assert result == {"title": "ok"}
    assert recorded == {"model": "claude-haiku-4-5-20251001", "purpose": "selection", "usage": FakeResponse.usage}


def test_statsd_omits_host_port_when_unconfigured(monkeypatch):
    """DogStatsd must fall through to its own DD_DOGSTATSD_URL/DD_AGENT_HOST
    detection (the cluster's injected Unix socket) rather than being pinned
    to a UDP host:port that doesn't exist in the pod."""
    captured = {}

    class FakeDogStatsd:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(observability, "_statsd_client", None)
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability.settings, "datadog_statsd_host", "")
    monkeypatch.setattr(observability.settings, "datadog_metrics_namespace", "acgc")
    monkeypatch.setitem(
        __import__("sys").modules,
        "datadog",
        type("_module", (), {"DogStatsd": FakeDogStatsd}),
    )

    observability._statsd()

    assert captured == {"namespace": "acgc"}
    assert "host" not in captured
    assert "port" not in captured


def test_statsd_uses_configured_host_port_when_set(monkeypatch):
    captured = {}

    class FakeDogStatsd:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(observability, "_statsd_client", None)
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability.settings, "datadog_statsd_host", "10.0.0.5")
    monkeypatch.setattr(observability.settings, "datadog_statsd_port", 9125)
    monkeypatch.setattr(observability.settings, "datadog_metrics_namespace", "acgc")
    monkeypatch.setitem(
        __import__("sys").modules,
        "datadog",
        type("_module", (), {"DogStatsd": FakeDogStatsd}),
    )

    observability._statsd()

    assert captured == {"namespace": "acgc", "host": "10.0.0.5", "port": 9125}


def test_annotate_endpoint_records_user_header(monkeypatch):
    seen = {}

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        skip_literature_for_oncokb=False,
        mode="full",
    ):
        return AnnotationResult(
            run_id="run-1",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="BRAF")],
            timings_ms={"total": 2.0},
        )

    async def fake_persist_run_result(http_request, request_payload, result):
        return None

    def fake_record_user_seen(user_id, tags=None):
        seen["user_id"] = user_id
        seen["tags"] = tags

    app.state.run_store = None
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main, "_persist_run_result", fake_persist_run_result)
    monkeypatch.setattr(main, "record_user_seen", fake_record_user_seen)
    client = TestClient(app)

    response = client.post(
        "/v1/annotate",
        json={"fusions": ["BRAF::TP53"], "mode": "core"},
        headers={"x-user-id": "curator@example.com"},
    )

    assert response.status_code == 200
    assert seen == {
        "user_id": "curator@example.com",
        "tags": ["mode:core", "local_backend:sdk", "skip_literature_for_oncokb:False"],
    }


def test_user_context_lifecycle():
    token = observability.set_user_context(
        user_id="curator@mskcc.org",
        email="curator@mskcc.org",
        name="MSK Curator",
        role="curator",
        domain="mskcc.org",
    )
    ctx = observability.get_user_context()
    assert ctx["user_id"] == "curator@mskcc.org"
    assert ctx["email"] == "curator@mskcc.org"
    assert ctx["name"] == "MSK Curator"
    assert ctx["role"] == "curator"
    assert ctx["domain"] == "mskcc.org"

    observability.reset_user_context(token)
    assert observability.get_user_context() == {}


def test_datadog_log_record_factory_injects_user_context():
    token = observability.set_user_context(
        user_id="curator@mskcc.org",
        email="curator@mskcc.org",
        name="MSK Curator",
    )
    try:
        record = logging.LogRecord("test", logging.INFO, "test.py", 10, "Hello", (), None)
        record = main._datadog_log_record_factory(
            record.name, record.levelno, record.pathname, record.lineno,
            record.msg, record.args, record.exc_info
        )
        assert getattr(record, "usr.id") == "curator@mskcc.org"
        assert getattr(record, "usr.email") == "curator@mskcc.org"
        assert getattr(record, "usr.name") == "MSK Curator"
    finally:
        observability.reset_user_context(token)


def test_record_user_action_structured_logging_and_metrics(monkeypatch, caplog):
    fake_statsd = FakeStatsd()
    monkeypatch.setattr("src.observability.settings.datadog_metrics_enabled", True)
    monkeypatch.setattr("src.observability._statsd_client", fake_statsd)
    monkeypatch.setattr("src.observability.settings.datadog_tag_user_metrics", True)

    with caplog.at_level(logging.INFO):
        observability.record_user_action(
            "curator@mskcc.org",
            action="annotate",
            details={"mode": "core", "inputs_count": 2},
            tags=["mode:core"],
        )

    # Check DogStatsD metrics
    assert ("set", "users.active", stable_user_key("curator@mskcc.org"), ["mode:core"]) in fake_statsd.calls
    assert (
        "increment",
        "users.activity",
        1,
        ["mode:core", "action:annotate", "user:curator@mskcc.org"],
    ) in fake_statsd.calls

    # Check structured log
    assert any(
        "USER_ACTION: action=annotate user=curator@mskcc.org mode=core inputs_count=2" in rec.message
        for rec in caplog.records
    )


def test_record_user_action_anonymous(monkeypatch, caplog):
    fake_statsd = FakeStatsd()
    monkeypatch.setattr("src.observability.settings.datadog_metrics_enabled", True)
    monkeypatch.setattr("src.observability._statsd_client", fake_statsd)

    with caplog.at_level(logging.INFO):
        observability.record_user_action(None, action="request")

    assert fake_statsd.calls == [
        ("increment", "users.anonymous_requests", 1, ["action:request"])
    ]
    assert any("USER_ACTION: action=request user=anonymous" in rec.message for rec in caplog.records)


def test_tag_user_sets_active_span_metadata(monkeypatch):
    class FakeSpan:
        def __init__(self):
            self.tags = {}

        def set_tags(self, tags):
            self.tags.update(tags)

    fake_span = FakeSpan()

    class FakeTracer:
        def current_span(self):
            return fake_span

    monkeypatch.setitem(__import__("sys").modules, "ddtrace", type("_mod", (), {"tracer": FakeTracer()}))

    observability.tag_user(
        user_id="curator@mskcc.org",
        email="curator@mskcc.org",
        name="MSK Curator",
        role="curator",
    )

    assert fake_span.tags == {
        "usr.id": "curator@mskcc.org",
        "usr.email": "curator@mskcc.org",
        "usr.name": "MSK Curator",
        "usr.role": "curator",
    }
