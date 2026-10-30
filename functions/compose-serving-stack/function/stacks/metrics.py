# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""What Modelplane calls each metric the components it installs emit.

Reviewed, pinned data, the same discipline as the component lists. A
MetricMapping is the extension point for a component Modelplane ships no
statements for; these are the ones it does.

Renaming is only safe where the measurements agree. SGLang's
inter_token_latency is not vLLM's time per output token, so neither is
renamed onto a shared name and the gateway supplies that measurement for
both. A histogram is renamed only where its bucket boundaries match, which is
why SGLang's latency histograms are absent here: they resolve to a hundred
milliseconds where vLLM's resolve to one, and a quantile across the two is
wrong rather than approximate. SGLang's time to first token is still worth
having, since the gateway's own histogram stops at a minute and a long prompt
on a busy engine waits longer than that, so it keeps a name of its own,
modelplane_sglang_ttft_seconds, which no other source shares.

Matching has to hold across the fleet, not just between two engines, because
a query sums buckets by boundary and a series with different boundaries
corrupts the sum for everyone. So each modelplane_* histogram has
exactly one source. The engines' token histograms can't meet that even alone:
vLLM sizes their buckets from --max-model-len, so two vLLM deployments
disagree. So they're exported only as their sums and counts, which have no buckets.

docs/utils/validate/validate_manifests.py holds every recipe's engine to the
versions and flags these mappings need, so a change to one changes both.
"""

from models.ai.modelplane.metricmapping import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The front door, which measures every request it proxies under the
# OpenTelemetry GenAI conventions, for whatever engine is behind it. These are
# the SLO metrics: one component, one bucket layout, so a fleet quantile over
# them is sound.
_GATEWAY = {
    "gen_ai_server_request_duration_seconds": "modelplane_frontend_request_duration_seconds",
    "gen_ai_server_time_to_first_token_seconds": "modelplane_frontend_ttft_seconds",
    "gen_ai_server_time_per_output_token_seconds": "modelplane_frontend_tpot_seconds",
}

# The front door's own count of requests in flight to each model, which the
# collector keeps for the fleet gateway's model routes only (see
# collector._IN_FLIGHT). The engines' running and waiting requests well above
# it, for minutes, is work no caller is waiting for.
_GATEWAY_PROXY = {
    "envoy_cluster_upstream_rq_active": "modelplane_frontend_requests_active",
}

# The engines, which explain what the gateway measured. vLLM publishes these
# names from v0.9.2.
_VLLM = {
    "vllm:time_to_first_token_seconds": "modelplane_request_ttft_seconds",
    "vllm:e2e_request_latency_seconds": "modelplane_request_duration_seconds",
    "vllm:request_queue_time_seconds": "modelplane_request_queue_seconds",
    "vllm:request_prefill_time_seconds": "modelplane_request_prefill_seconds",
    "vllm:request_decode_time_seconds": "modelplane_request_decode_seconds",
    "vllm:num_requests_running": "modelplane_requests_running",
    "vllm:num_requests_waiting": "modelplane_requests_waiting",
    "vllm:kv_cache_usage_perc": "modelplane_kv_cache_utilization_ratio",
    "vllm:num_preemptions_total": "modelplane_requests_preempted_total",
    "vllm:prefix_cache_hits_total": "modelplane_prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total": "modelplane_prefix_cache_lookups_total",
}

# SGLang's prefix-cache and retraction counters count what vLLM's do, in the
# same units: prefix-cache hits and lookups in prompt tokens, so hits over
# lookups is a hit rate for either engine. SGLang counts when a request
# finishes where vLLM counts when it's scheduled, and labels its hits with a
# cache_source (device, host, storage). sglang:queue_time_seconds is a
# per-request histogram like vLLM's queue time, but its buckets differ from
# vLLM's and between SGLang versions, so it isn't renamed (#523).
_SGLANG = {
    "sglang:num_running_reqs": "modelplane_requests_running",
    "sglang:num_queue_reqs": "modelplane_requests_waiting",
    "sglang:token_usage": "modelplane_kv_cache_utilization_ratio",
    "sglang:num_retracted_requests_total": "modelplane_requests_preempted_total",
    "sglang:cached_tokens_total": "modelplane_prefix_cache_hits_total",
    "sglang:prompt_tokens_total": "modelplane_prefix_cache_lookups_total",
    "sglang:time_to_first_token_seconds": "modelplane_sglang_ttft_seconds",
    "sglang:http_requests_active": "modelplane_engine_requests_active",
}

# Each engine's prompt and generation token histograms, taken as counters: the
# prompt and generated tokens of the requests it finished, and from the prompt
# histogram's count, the requests. A prompt's tokens include any the prefix
# cache served. Both engines observe these histograms when a request finishes,
# so tokens and requests count the same requests and divide into tokens per
# request. The engines' plain token counters, vllm:prompt_tokens_total and
# sglang:prompt_tokens_total, count tokens as they're processed instead,
# including requests that never finish, and SGLang's already feeds
# modelplane_prefix_cache_lookups_total. modelplane_responses_total, from
# vllm:request_success_total, counts vLLM's finished requests by how they ended,
# and can differ slightly from this count, as on aborted requests. Dashboards
# count requests with modelplane_engine_requests_total, which every engine has.
# SGLang v0.5.10 publishes these histograms only with
# --collect-tokens-histogram as well as --enable-metrics; from v0.5.12 it
# always does.
_VLLM_TOKENS = ("vllm:request_prompt_tokens", "vllm:request_generation_tokens")
_SGLANG_TOKENS = ("sglang:prompt_tokens_histogram", "sglang:generation_tokens_histogram")

# The endpoint picker. A router's queue is a different measurement from an
# engine's, so it keeps a name of its own.
_PICKER = {
    "llm_d_epp_scheduler_e2e_duration_seconds": "modelplane_route_decision_seconds",
}

# The GPUs, through whichever vendor's exporter the stack installed. DCGM
# reports framebuffer memory in MiB and energy in millijoules, and each is
# renamed onto a name that claims a different unit, so each says so.
_GPU = {
    "DCGM_FI_DEV_FB_USED": "modelplane_gpu_memory_used_bytes",
    "DCGM_FI_DEV_FB_FREE": "modelplane_gpu_memory_free_bytes",
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE": "modelplane_gpu_compute_active_ratio",
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "modelplane_gpu_tensor_active_ratio",
    "DCGM_FI_PROF_DRAM_ACTIVE": "modelplane_gpu_memory_bandwidth_ratio",
    "DCGM_FI_DEV_GPU_TEMP": "modelplane_gpu_temperature_celsius",
    "DCGM_FI_DEV_POWER_USAGE": "modelplane_gpu_power_watts",
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "modelplane_energy_joules_total",
}

_GPU_UNITS = {
    "DCGM_FI_DEV_FB_USED": "Mebibytes",
    "DCGM_FI_DEV_FB_FREE": "Mebibytes",
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "Millijoules",
}

# The AMD GPUs, through the AMD GPU operator's device-metrics-exporter, onto
# the same modelplane_gpu_* names the DCGM series map to, so a dashboard reads
# one surface whichever vendor's pool served it. The exporter reports VRAM in
# MiB, activity in percent and energy in microjoules, each renamed onto a name
# that claims a different unit, so each says so. Junction temperature is the
# one the Instinct MI3xx series reports (MI2xx reports edge instead).
# Tensor activity (MFMA utilization, the analog of DCGM's tensor-pipe
# activity) is a GPU_PROF_* field the exporter ships disabled; the stack's
# exporter config turns ProfilerMetrics on to publish it (see the AMD
# chart pin in clouds/existing.py) at the cost of the single profiler
# instance the hardware allows. The exporter reports its accumulated energy
# as a gauge where DCGM's is a counter, and modelplane_energy_joules_total is
# a counter, so the energy series is marked fromType Gauge and the collector
# converts it to a monotonic sum - one name, one type, whichever vendor feeds
# it.
_AMD_GPU = {
    "gpu_used_vram": "modelplane_gpu_memory_used_bytes",
    "gpu_free_vram": "modelplane_gpu_memory_free_bytes",
    "gpu_gfx_activity": "modelplane_gpu_compute_active_ratio",
    "gpu_umc_activity": "modelplane_gpu_memory_bandwidth_ratio",
    "gpu_prof_tensor_active_percent": "modelplane_gpu_tensor_active_ratio",
    "gpu_junction_temperature": "modelplane_gpu_temperature_celsius",
    "gpu_power_usage": "modelplane_gpu_power_watts",
    "gpu_energy_consumed": "modelplane_energy_joules_total",
}

_AMD_GPU_UNITS = {
    "gpu_used_vram": "Mebibytes",
    "gpu_free_vram": "Mebibytes",
    "gpu_gfx_activity": "Percent",
    "gpu_umc_activity": "Percent",
    "gpu_prof_tensor_active_percent": "Percent",
    "gpu_energy_consumed": "Microjoules",
}

_AMD_GPU_TYPES = {"gpu_energy_consumed": "Gauge"}


# How each request vLLM finished ended, by vLLM's finished_reason renamed to
# reason: stop, length and abort at v0.9.2, and error and repetition as well
# from v0.23.0. A client that disconnects should end its request as abort, so abort
# staying at 0 while clients give up means the disconnect isn't reaching the
# engine. SGLang publishes no counter by how a request ended.
_VLLM_RESPONSES: dict[str, object] = {
    "from": "vllm:request_success_total",
    "to": "modelplane_responses_total",
    "labels": [{"name": "reason", "from": "finished_reason"}],
}


# How the HTTP requests SGLang answered ended, by status. SGLang counts them
# by raw status code, which design/telemetry.md keeps out of the series: a label
# with forty values is forty series per deployment. So the codes an
# OpenAI-compatible server returns fold into the gateway's three classes, ok,
# client_error and server_error, and a rarer code keeps its number. The
# counter also carries the endpoint, which a query needs, because SGLang counts
# /health and /metrics as well as completions. SGLang's speculative-decoding
# gauges aren't mapped: spec_accept_rate measured accepted tokens over draft
# tokens at v0.5.10 and accepted drafts over proposed drafts at v0.5.12, so one
# name would hold two measurements.
_STATUS_CLASSES = {
    "200": "ok",
    **dict.fromkeys(("400", "404", "422", "429"), "client_error"),
    **dict.fromkeys(("500", "503"), "server_error"),
}
_SGLANG_RESPONSES: dict[str, object] = {
    "from": "sglang:http_responses_total",
    "to": "modelplane_engine_responses_total",
    "labels": [{"name": "status", "from": "status_code", "values": _STATUS_CLASSES}],
}


def _tokens(prompt: str, generation: str) -> list[dict[str, object]]:
    """An engine's token histograms as tokens by direction, and requests."""
    return [
        {
            "from": prompt,
            "part": "Sum",
            "to": "modelplane_engine_tokens_total",
            "labels": [{"name": "direction", "value": "input"}],
        },
        {
            "from": generation,
            "part": "Sum",
            "to": "modelplane_engine_tokens_total",
            "labels": [{"name": "direction", "value": "output"}],
        },
        {"from": prompt, "part": "Count", "to": "modelplane_engine_requests_total"},
    ]


def _mapping(
    name: str,
    pairs: dict[str, str],
    units: dict[str, str] | None = None,
    tokens: tuple[str, str] | None = None,
    extra: tuple[dict[str, object], ...] = (),
    types: dict[str, str] | None = None,
) -> v1alpha1.MetricMapping:
    units = units or {}
    types = types or {}
    metrics: list[dict[str, object]] = [
        {"from": src, "to": dst}
        | ({"fromUnit": units[src]} if src in units else {})
        | ({"fromType": types[src]} if src in types else {})
        for src, dst in pairs.items()
    ]
    if tokens:
        metrics += _tokens(*tokens)
    metrics += extra
    return v1alpha1.MetricMapping(
        metadata=metav1.ObjectMeta(name=name),
        spec=v1alpha1.Spec(metrics=[v1alpha1.Metric.model_validate(m) for m in metrics]),
    )


def mappings() -> list[v1alpha1.MetricMapping]:
    """The mappings Modelplane provides, as the kind an operator would write.

    Built as MetricMappings rather than as collector configuration so that
    Modelplane's own renames reach the collector down the same path an
    operator's do, and a built-in that breaks breaks the path everyone uses.
    """
    return [
        _mapping("modelplane-gateway", _GATEWAY),
        _mapping("modelplane-gateway-proxy", _GATEWAY_PROXY),
        _mapping("modelplane-vllm", _VLLM, tokens=_VLLM_TOKENS, extra=(_VLLM_RESPONSES,)),
        _mapping("modelplane-sglang", _SGLANG, tokens=_SGLANG_TOKENS, extra=(_SGLANG_RESPONSES,)),
        _mapping("modelplane-picker", _PICKER),
        _mapping("modelplane-gpu", _GPU, _GPU_UNITS),
        _mapping("modelplane-gpu-amd", _AMD_GPU, _AMD_GPU_UNITS, types=_AMD_GPU_TYPES),
    ]


BUILTIN_MAPPINGS = mappings()
