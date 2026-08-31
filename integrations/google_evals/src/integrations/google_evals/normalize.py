"""Normalize a pinned webmcp-evals JSON report ({config, results}) into the
core call-level trial model (scaffolding only).

Notes captured from the upstream source (webmcp-tools@d39eae4): trials carry no
stable ID — only test name + runIndex — so FR-091 explicit binding is mandatory;
treat every imported field as untrusted input; redact before persistence (FR-090).
"""
