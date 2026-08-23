"""Cloud Trace / Cloud Monitoring / Cloud Logging telemetry for this system's
ADK agents — the code-level half of Gemini Enterprise Agent Platform's
"Agent Observability" (docs.cloud.google.com/gemini-enterprise-agent-platform
/optimize/observability/overview).

WHY NOT THE DOCUMENTED CLI FLAG. GEAP's own setup doc for ADK
(docs.cloud.google.com/stackdriver/docs/instrumentation/ai-agent-adk) is
written around `adk web --otel_to_cloud` / `adk api_server --otel_to_cloud`.
This project doesn't launch agents that way — services/orchestrator_service
and services/state_service are their own FastAPI apps that build ADK
LlmAgents and drive them via InMemoryRunner (tools/adk_runner.py), so there is
no `adk web` process for that flag to attach to.

Verified directly against the installed google-adk 2.6.3 (not assumed from
docs): `--otel_to_cloud` ultimately calls a real, private-but-readable
function, google/adk/cli/api_server.py::_setup_gcp_telemetry(), built from
three public pieces — google.adk.telemetry.google_cloud.get_gcp_exporters(),
.get_gcp_resource(), and google.adk.telemetry.setup.maybe_set_otel_providers()
— that can be called directly at process startup instead. This module
reproduces that function's real logic, not a guess at what it might do.

get_gcp_resource() matters more than it looks: without it,
maybe_set_otel_providers() falls back to a generic OTel resource with no
gcp.project_id attribute, and telemetry.googleapis.com's OTLP endpoint
rejects every export with a real, confirmed-live 400 ("Resource is missing
required attribute 'gcp.project_id'").

METRICS, DISCLOSED LIMITATION IN LOCAL DEV. Even with the correct resource,
Cloud Monitoring's OTLP ingestion separately requires enough resource
attributes to resolve a concrete "monitored resource" (instance identity) —
confirmed live: it 400s here with "prometheus_target resource type must have
an instance specified", because get_gcp_resource()'s GoogleCloudResourceDetector
step can't reach the GCP metadata server (metadata.google.internal) outside
real GCP compute. Traces and logs are unaffected by this. Expected to resolve
automatically once these services actually run on Cloud Run/GCE/GKE, where
that metadata server is real — not verified against an actual Cloud Run
deployment, since this project runs locally today.

WHAT THIS DOES NOT COVER. GEAP's Observability tab (Agent Platform > Agent
Registry > <agent> > Traces) is a separate, additional layer on top of this:
per docs.cloud.google.com/agent-registry/register-custom-adk-agents, seeing
an agent there at all requires it to be manually registered in the Agent
Registry — a real `gcloud agent-registry services create` call against an
A2A-format Agent Card, which this project does not currently serve. Not
implemented here (a real GCP registration action, deliberately left for an
explicit decision rather than done silently). What IS real and complete here:
every agent's model calls and span data are shipped to actual Cloud Trace /
Cloud Monitoring / Cloud Logging the moment this is enabled — visible today in
the plain Cloud Trace / Logs Explorer consoles, and would also populate the
GEAP Registry's own Traces tab once that separate registration step is done,
since both read from the same underlying telemetry.googleapis.com data.

OFF BY DEFAULT. OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY
means real prompt/response text leaves the process as span events — harmless
for this project's synthetic patient data, but a real data-handling choice
that shouldn't turn on silently. Set SURGGRAPH_ENABLE_CLOUD_TELEMETRY=true to
enable.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("SURGGRAPH_ENABLE_CLOUD_TELEMETRY", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

_configured = False


def setup_cloud_observability(service_name: str) -> None:
    """Call once, at process startup, before serving any requests.

    No-op if SURGGRAPH_ENABLE_CLOUD_TELEMETRY is unset, or if already called
    once in this process (OTel providers are process-global; calling this
    twice would just log ADK's own "already set" warning for no benefit).
    """
    global _configured
    if not _ENABLED:
        logger.info("Cloud telemetry disabled (SURGGRAPH_ENABLE_CLOUD_TELEMETRY unset) for %s", service_name)
        return
    if _configured:
        return

    # Read by ADK's own OTel instrumentation when it creates spans, not by
    # this function — must land before the first agent call, hence setting it
    # here at startup rather than deferring to first use.
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "EVENT_ONLY")

    import google.auth
    from google.adk.telemetry.google_cloud import get_gcp_exporters, get_gcp_resource
    from google.adk.telemetry.setup import maybe_set_otel_providers

    google_auth = google.auth.default()
    _, project_id = google_auth

    hooks = get_gcp_exporters(
        enable_cloud_tracing=True,
        enable_cloud_metrics=True,
        enable_cloud_logging=True,
        google_auth=google_auth,
    )
    maybe_set_otel_providers([hooks], otel_resource=get_gcp_resource(project_id))
    _configured = True
    logger.info("Cloud telemetry enabled for %s -> Cloud Trace/Monitoring/Logging", service_name)
