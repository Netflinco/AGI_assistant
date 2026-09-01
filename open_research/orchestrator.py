"""Evidence-first Open Research service with no inspection/Office dependency."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
import re
import time
import uuid
from typing import Any, Callable

from agent_governance.audit import summary_hash
from agent_governance.contracts import GateContext, GateDecision, GateError
from agent_governance.gate_engine import GateEngine
from agent_governance.policy_registry import feature_enabled
from agent_governance.runtime import research_quota_decision, reserve_research_request

from .boundary import egress_decision
from .claims import ACTUAL_RELEASE, SCHEDULED_RELEASE, ResearchClaim, evaluate_event_date_claims, evaluate_generic_evidence, extract_event_date_claims, extract_policy_appointment_claims
from .evidence import Evidence, assess_evidences, normalize_citations, relevant_to_query
from .gateway import ResearchGatewayError, SearchGateway
from .detail_fetch import DetailFetcher
from .evidence_reasoner import EvidenceReasoner, EvidenceReasonerError
from .intent import EntityResolver
from .memory import active_memories, archive_memory
from .planner import FactAssessment, ResearchQuery, build_plan, classify_fact_intent
from .retention import NO_MEMORY, allows_reuse, retention_class
from .source_policy import SourcePolicy, load_active_source_policies, reviewed_domains


class ResearchError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class OpenResearchService:
    """Orchestrates plan → Tavily → evidence → memory under G0-G7 decisions."""

    def __init__(self, conn: Any, gateway: SearchGateway, *, now: Callable[[], datetime] | None = None,
                 audit_logger: Callable[..., Any] | None = None, resolver: EntityResolver | None = None,
                 detail_fetcher: DetailFetcher | None = None, reasoner: EvidenceReasoner | None = None):
        self.conn = conn
        self.gateway = gateway
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.audit_logger = audit_logger
        self.resolver = resolver or EntityResolver()
        self.detail_fetcher = detail_fetcher
        self.reasoner = reasoner

    def run(self, *, tenant_id: str, user_id: str, conversation_id: str | None, question: str,
            workflow_id: str | None = None, request_id: str | None = None, force_refresh: bool = False,
            planning_query: str | None = None) -> dict[str, Any]:
        timestamp = self.now()
        planning_query = str(planning_query or question).strip()
        request_id = request_id or f"req_{uuid.uuid4().hex[:16]}"
        base_context = GateContext(
            request_id=request_id, tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
            requested_domain="OPEN_RESEARCH", action="SEARCH", workflow_id=workflow_id,
            input_summary_hash=summary_hash(question), data_classification="PUBLIC",
        )
        engine = GateEngine(self.conn, now=timestamp.isoformat(timespec="seconds"), audit_logger=self.audit_logger)
        try:
            preflight = engine.evaluate(base_context, [
                ("G0", lambda: GateDecision("G0", "ALLOW" if feature_enabled(self.conn, tenant_id, "open_research_enabled") else "BLOCK", "FEATURE_ENABLED" if feature_enabled(self.conn, tenant_id, "open_research_enabled") else "FEATURE_DISABLED")),
                ("G1", lambda: GateDecision("G1", "ALLOW", "OPEN_RESEARCH_ROUTE_CONFIRMED")),
                ("G2", lambda: egress_decision(planning_query)),
                ("G3", lambda: GateDecision("G3", "ALLOW", "RESEARCH_PLAN_ALLOWED", {"max_search_queries": 5, "max_detail_pages": 3})),
                ("G4", lambda: GateDecision("G4", "ALLOW", "READ_ONLY_AUTO_EXECUTION")),
                ("G5", lambda: research_quota_decision(self.conn, tenant_id=tenant_id, user_id=user_id, now=timestamp)),
            ])
        except GateError as exc:
            return self._blocked_result(exc.decision, question, request_id)

        rewrite = self.resolver.resolve(planning_query)
        if not rewrite.applied and rewrite.reason in {"ENTITY_AMBIGUOUS", "ENTITY_LOW_CONFIDENCE"}:
            decision = engine.record(base_context, GateDecision("G3", "REQUIRE_CONFIRMATION", "QUERY_REWRITE_CLARIFICATION_REQUIRED", {"candidates": list(rewrite.candidates)}))
            return self._blocked_result(decision, question, request_id, rewrite=rewrite.public_dict())
        assessment = classify_fact_intent(rewrite.rewritten_query)
        # The model is the primary evidence synthesiser for every public-fact
        # intent.  It receives only safe, bounded result cards and can never
        # answer without current-run citation IDs; deterministic extractors are
        # an availability fallback rather than a domain-registration gate.
        use_llm_synthesis = bool(self.reasoner and self.reasoner.configured)
        policies = load_active_source_policies(self.conn, now=timestamp)
        topic = self._topic(rewrite.rewritten_query)
        # Live / force-refresh requests must not receive a historical fact,
        # citation or source hint.  A following "那现在呢" is therefore a new
        # provider run whose only inherited material is its public entity slot.
        memories = [] if force_refresh or assessment.dynamic else active_memories(
            self.conn, tenant_id=tenant_id, user_id=user_id, topic=topic, now=timestamp,
        )
        memory_hint = None if force_refresh or assessment.dynamic else self._memory_hint(memories)
        plan = build_plan(
            planning_query,
            rewrite,
            assessment=assessment,
            memory_hint=memory_hint,
            reviewed_domains=reviewed_domains(policies, fact_intent=assessment.fact_intent),
        )
        run_id = f"res_{uuid.uuid4().hex[:16]}"
        self._create_run(
            run_id, tenant_id, user_id, conversation_id, workflow_id, question, rewrite.public_dict(), plan, assessment, timestamp,
            force_refresh=force_refresh,
        )

        # Historical event dates are reusable only after this method has
        # reloaded the user's own evidence IDs and source policies.  Future
        # schedules and all live facts always continue to Tavily.
        memory_claims, memory_evidences = self._load_reusable_memory(
            memories, assessment=assessment, policies=policies, now=timestamp,
        )
        if memory_claims and not force_refresh:
            status, deliverable_claims = self._quality(
                assessment, memory_evidences, memory_claims, policies, timestamp,
            )
            if status == "VERIFIED":
                quality = engine.record(base_context, GateDecision("G6", "ALLOW", "MEMORY_HIT_VERIFIED"))
                delivered = self._delivered_evidences(memory_evidences, deliverable_claims)
                answer = self._answer(status, rewrite, deliverable_claims, assessment, timestamp, memory_hit=True, evidences=delivered)
                brief = self._brief(run_id, tenant_id, user_id, topic, status, answer, delivered, assessment, timestamp)
                self._persist_evidence_and_finish(
                    run_id, tenant_id, user_id, memory_evidences, status, answer, brief, [], assessment, timestamp,
                )
                self.conn.execute(
                    "UPDATE open_research_runs SET retention_class=? WHERE run_id=?",
                    (retention_class(assessment, deliverable_claims), run_id),
                )
                delivery = engine.record(base_context, GateDecision("G7", "ALLOW", "RESEARCH_MEMORY_DELIVERED", {"status": status}))
                return self._result(
                    run_id, workflow_id, status, answer, rewrite, plan, delivered, brief, assessment,
                    request_id, [], preflight, quality, delivery, memory_hit=True, force_refresh=force_refresh,
                )

        fetched: list[Evidence] = []
        provider_requests: list[dict[str, Any]] = []
        try:
            # Charge once before the first outbound provider call.  Rejections
            # above, and a verified memory hit, use zero Tavily capacity.
            reserve_research_request(self.conn, tenant_id=tenant_id, user_id=user_id, request_value=planning_query, now=timestamp)
            for search_query in plan:
                started = time.perf_counter()
                result = self.gateway.search(
                    search_query.query,
                    freshness=search_query.freshness,
                    topic=search_query.topic,
                    include_domains=search_query.include_domains,
                )
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                provider_requests.append({
                    "query_hash": summary_hash(search_query.query), "provider": result.get("provider"), "request_id": result.get("request_id"),
                    "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
                    "credits": self._usage_credits(usage.get("credits")),
                })
                normalized = normalize_citations(
                    result.get("citations") or [],
                    fetched_at=str(result.get("fetched_at") or timestamp.isoformat(timespec="seconds")),
                    source_policies=policies,
                )
                assessed = assess_evidences(normalized, query=rewrite.rewritten_query, assessment=assessment, now=timestamp)
                fetched.extend(item for item in assessed if relevant_to_query(item, rewrite.rewritten_query))
                if not use_llm_synthesis:
                    candidate_claims = self._extract_claims(assessment, rewrite.rewritten_query, fetched, policies, timestamp)
                    candidate_status, _deliverable = self._quality(assessment, fetched, candidate_claims, policies, timestamp)
                    if candidate_status == "VERIFIED" and search_query.stop_after:
                        break
            # A detail page is an evidence reader, not a general crawler. It
            # only runs for Tavily result URLs already constrained above, and
            # only the returned 300-character fact fragment stays in memory.
            fetched = self._dedupe_evidences(fetched)
            fetched, _detail_trace = self._detail_enrich(
                fetched, rewrite.rewritten_query, assessment=assessment, policies=policies, now=timestamp,
            )
            fetched = assess_evidences(fetched, query=rewrite.rewritten_query, assessment=assessment, now=timestamp)
            if use_llm_synthesis:
                # The model receives the full three-query evidence pack.  Do
                # not let the old date-role evaluator decide whether a fourth
                # query is needed in this primary path.
                lead_queries = []
            else:
                detail_claims = self._extract_claims(assessment, rewrite.rewritten_query, fetched, policies, timestamp)
                # A lower-reputation page can reveal a precise value.  Use it
                # to issue one bounded corroboration query, without turning a
                # source catalogue into a retrieval allow-list.
                detail_status, _ = self._quality(assessment, fetched, detail_claims, policies, timestamp)
                lead_queries = [] if detail_status == "VERIFIED" else self._corroboration_queries(
                    detail_claims, policies, assessment, now=timestamp,
                )
            for search_query in lead_queries[:2]:
                plan.append(search_query)
                started = time.perf_counter()
                result = self.gateway.search(
                    search_query.query, freshness=search_query.freshness, topic=search_query.topic,
                    include_domains=search_query.include_domains,
                )
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                provider_requests.append({
                    "query_hash": summary_hash(search_query.query), "provider": result.get("provider"), "request_id": result.get("request_id"),
                    "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
                    "credits": self._usage_credits(usage.get("credits")),
                })
                normalized = normalize_citations(
                    result.get("citations") or [], fetched_at=str(result.get("fetched_at") or timestamp.isoformat(timespec="seconds")),
                    source_policies=policies,
                )
                assessed = assess_evidences(normalized, query=rewrite.rewritten_query, assessment=assessment, now=timestamp)
                fetched.extend(item for item in assessed if relevant_to_query(item, rewrite.rewritten_query))
            fetched = self._dedupe_evidences(fetched)
            fetched = assess_evidences(fetched, query=rewrite.rewritten_query, assessment=assessment, now=timestamp)
        except ResearchGatewayError as exc:
            return self._finish_unavailable(
                run_id, exc.code, rewrite, plan, provider_requests, assessment, engine, base_context, preflight,
            )

        claims = [] if use_llm_synthesis else self._extract_claims(
            assessment, rewrite.rewritten_query, fetched, policies, timestamp,
        )
        synthesis = None
        if use_llm_synthesis:
            try:
                synthesis = self.reasoner.synthesize(
                    query=rewrite.rewritten_query,
                    assessment=assessment,
                    evidences=fetched,
                    policies=policies,
                )
            except EvidenceReasonerError:
                # The legacy evaluator is an availability fallback only.  It
                # keeps a search result evidence-bound if the configured model
                # is temporarily unavailable; the model path is the primary
                # event-date decision engine whenever it is configured.
                synthesis = None
        if synthesis:
            status, deliverable_claims = synthesis.status, list(synthesis.claims)
        else:
            status, deliverable_claims = self._quality(assessment, fetched, claims, policies, timestamp)
        quality = engine.record(base_context, GateDecision("G6", "ALLOW" if status in {"VERIFIED", "PARTIALLY_VERIFIED"} else "DEGRADE", status))
        delivered = self._delivered_evidences(fetched, deliverable_claims)
        answer = self._answer(
            status, rewrite, deliverable_claims, assessment, timestamp, evidences=delivered,
            synthesis_summary=synthesis.summary if synthesis else None,
        )
        if synthesis:
            answer["evidence_synthesis"] = synthesis.public_dict()
        brief = self._brief(run_id, tenant_id, user_id, topic, status, answer, delivered, assessment, timestamp)
        self.conn.execute(
            "UPDATE open_research_runs SET plan_json=? WHERE run_id=?",
            (json.dumps([item.public_dict() for item in plan], ensure_ascii=False), run_id),
        )
        self._persist_evidence_and_finish(
            run_id, tenant_id, user_id, fetched, status, answer, brief, provider_requests, assessment, timestamp,
        )
        retention = retention_class(
            assessment, deliverable_claims if status in {"VERIFIED", "PARTIALLY_VERIFIED"} else [],
        )
        self.conn.execute("UPDATE open_research_runs SET retention_class=? WHERE run_id=?", (retention, run_id))
        if status in {"VERIFIED", "PARTIALLY_VERIFIED"} and allows_reuse(retention):
            self._archive(run_id, tenant_id, user_id, topic, rewrite, fetched, deliverable_claims, assessment, status, retention, timestamp)
        delivery = engine.record(base_context, GateDecision("G7", "ALLOW", "RESEARCH_DELIVERED", {"status": status}))
        return self._result(
            run_id, workflow_id, status, answer, rewrite, plan, delivered, brief, assessment,
            request_id, provider_requests, preflight, quality, delivery, memory_hit=False, force_refresh=force_refresh,
        )

    def get_run(self, *, run_id: str, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM open_research_runs WHERE run_id=? AND tenant_id=? AND user_id=?", (run_id, tenant_id, user_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["rewrite"] = json.loads(result.pop("rewrite_json"))
        result["plan"] = json.loads(result.pop("plan_json"))
        result["answer"] = json.loads(result.pop("answer_json") or "{}")
        result["provider_requests"] = json.loads(result.pop("provider_requests_json") or "[]")
        result["queries"] = [dict(item) for item in self.conn.execute(
            "SELECT query_id, query_hash, purpose, freshness, topic, provider, provider_request_id, status, created_at FROM open_research_queries WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()]
        result["citations"] = [dict(item) for item in self.conn.execute("SELECT * FROM open_research_evidence WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()]
        result["claims"] = []
        for item in self.conn.execute(
            """SELECT claim_id, subject, predicate, claim_value, territory, event_state,
                      evidence_ids_json, claim_status, confidence, claim_hash, claim_json, created_at
                 FROM open_research_claims WHERE run_id=? ORDER BY created_at""",
            (run_id,),
        ).fetchall():
            claim = dict(item)
            claim["evidence_ids"] = json.loads(claim.pop("evidence_ids_json") or "[]")
            claim["claim"] = json.loads(claim.pop("claim_json") or "{}")
            result["claims"].append(claim)
        brief = self.conn.execute("SELECT * FROM research_briefs WHERE producer_run_id=?", (run_id,)).fetchone()
        result["brief"] = json.loads(brief["brief_json"]) if brief else None
        return result

    def _blocked_result(self, decision: GateDecision, question: str, request_id: str, rewrite: dict | None = None) -> dict[str, Any]:
        return {"status": "BLOCKED", "reason_code": decision.reason_code, "gate": decision.public_dict(), "request_id": request_id,
                "rewrite": rewrite, "answer": "此请求未进入公开检索；系统没有发送任何内容到外部搜索服务。"}

    def _create_run(self, run_id: str, tenant_id: str, user_id: str, conversation_id: str | None, workflow_id: str | None,
                    question: str, rewrite: dict, plan: list, assessment: FactAssessment, now: datetime, *, force_refresh: bool) -> None:
        self.conn.execute(
            """INSERT INTO open_research_runs(run_id, tenant_id, user_id, conversation_id, workflow_id, status, question_hash,
               fact_intent, quality_status, territory_assumption, retention_class, force_fresh, rewrite_json, plan_json, answer_json, as_of, created_at, updated_at)
               VALUES(?,?,?,?,?,'QUERYING',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, tenant_id, user_id, conversation_id, workflow_id, summary_hash(question), assessment.fact_intent, "QUERYING",
             assessment.territory if assessment.territory_assumed else None, NO_MEMORY, 1 if force_refresh else 0, json.dumps(rewrite, ensure_ascii=False),
             json.dumps([item.public_dict() for item in plan], ensure_ascii=False), "{}", now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )

    def _persist_evidence_and_finish(self, run_id: str, tenant_id: str, user_id: str, evidences: list[Evidence], status: str, answer: dict,
                                     brief: dict, provider_requests: list[dict], assessment: FactAssessment, now: datetime) -> None:
        plan = self.conn.execute("SELECT plan_json FROM open_research_runs WHERE run_id=?", (run_id,)).fetchone()
        planned = json.loads((plan["plan_json"] if plan else "[]") or "[]")
        for index, query in enumerate(planned):
            provider_request = provider_requests[index] if index < len(provider_requests) else {}
            self.conn.execute(
                """INSERT INTO open_research_queries(query_id, run_id, query_hash, purpose, freshness, topic, provider, provider_request_id, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (f"qry_{uuid.uuid4().hex[:16]}", run_id, summary_hash(query.get("query") or ""), str(query.get("purpose") or ""),
                 str(query.get("freshness") or ""), str(query.get("topic") or ""), provider_request.get("provider"),
                 provider_request.get("request_id"), "EXECUTED" if index < len(provider_requests) else "NOT_EXECUTED", now.isoformat(timespec="seconds")),
            )
        for item in evidences:
            self.conn.execute(
                """INSERT OR REPLACE INTO open_research_evidence(evidence_id, run_id, title, canonical_url, publisher,
                    published_at, fetched_at, source_tier, source_policy_id, source_reputation, relevance_score, freshness_score, semantic_score, evidence_confidence, evidence_type, detail_fetch_status,
                    extraction_locator_type, fact_fragment_hash, detail_rejection_reason, snippet_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item.evidence_id, run_id, item.title, item.canonical_url, item.publisher, item.published_at, item.fetched_at,
                 item.source_tier, item.source_policy_id, item.source_reputation, item.relevance_score, item.freshness_score, item.semantic_score, item.evidence_confidence, item.evidence_type, item.detail_fetch_status,
                 item.extraction_locator_type, summary_hash(item.snippet) if item.detail_fetch_status == "DETAIL_FETCHED" else None,
                 item.detail_rejection_reason, summary_hash(item.snippet), now.isoformat(timespec="seconds")),
            )
        self.conn.execute(
            "UPDATE open_research_runs SET status=?, quality_status=?, answer_json=?, provider_requests_json=?, updated_at=? WHERE run_id=?",
            (status, status, json.dumps(answer, ensure_ascii=False), json.dumps(provider_requests, ensure_ascii=False), now.isoformat(timespec="seconds"), run_id),
        )
        self._record_provider_usage(run_id, tenant_id, user_id, provider_requests, status, now)
        self.conn.execute(
            """INSERT INTO research_briefs(brief_id, producer_run_id, tenant_id, user_id, status, content_hash, brief_json, expires_at, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (brief["brief_id"], run_id, brief["owner"]["tenant_id"], brief["owner"]["user_id"], status,
             brief["content_hash"], json.dumps(brief, ensure_ascii=False), brief["expires_at"], now.isoformat(timespec="seconds")),
        )
        for claim in brief.get("claims") or []:
            self.conn.execute(
                """INSERT INTO open_research_claims(
                       claim_id, run_id, subject, predicate, claim_value, territory, event_state,
                       evidence_ids_json, claim_status, confidence, claim_hash, claim_json, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"{claim['claim_id']}_{run_id[-8:]}", run_id, str(claim.get("subject") or ""), str(claim.get("predicate") or ""),
                    claim.get("value"), claim.get("territory"), claim.get("event_state"),
                    json.dumps(claim.get("evidence_ids") or []), str(claim.get("claim_status") or status),
                    float(claim.get("confidence") or 0), summary_hash(claim), json.dumps(claim, ensure_ascii=False),
                    now.isoformat(timespec="seconds"),
                ),
            )

    def _finish_unavailable(self, run_id: str, code: str, rewrite, plan, requests, assessment: FactAssessment, engine, context, preflight: list[GateDecision]) -> dict[str, Any]:
        now = self.now()
        status = "SEARCH_RATE_LIMITED" if code == "SEARCH_RATE_LIMITED" else "SEARCH_UNAVAILABLE"
        copy = "公开搜索请求已达到当前配额，请稍后再试；系统未生成未经证据核验的结论。" if status == "SEARCH_RATE_LIMITED" else "公开搜索服务暂时不可用，未生成未经证据核验的结论。"
        answer = {"text": copy, "status": status}
        self.conn.execute("UPDATE open_research_runs SET status=?, quality_status=?, answer_json=?, provider_requests_json=?, updated_at=? WHERE run_id=?",
                          (status, status, json.dumps(answer, ensure_ascii=False), json.dumps(requests, ensure_ascii=False), now.isoformat(timespec="seconds"), run_id))
        run = self.conn.execute("SELECT tenant_id, user_id FROM open_research_runs WHERE run_id=?", (run_id,)).fetchone()
        if run:
            self._record_provider_usage(run_id, run["tenant_id"], run["user_id"], requests, status, now)
        for index, item in enumerate(plan):
            request = requests[index] if index < len(requests) else {}
            self.conn.execute(
                """INSERT INTO open_research_queries(query_id, run_id, query_hash, purpose, freshness, topic, provider, provider_request_id, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (f"qry_{uuid.uuid4().hex[:16]}", run_id, summary_hash(item.query), item.purpose, item.freshness, item.topic,
                 request.get("provider"), request.get("request_id"), "EXECUTED" if index < len(requests) else "FAILED" if index == len(requests) else "NOT_EXECUTED", now.isoformat(timespec="seconds")),
            )
        decision = engine.record(context, GateDecision("G6", "DEGRADE", code))
        engine.record(context, GateDecision("G7", "ALLOW", "RESEARCH_DEGRADED_DELIVERY"))
        return {
            "run_id": run_id,
            "workflow_id": context.workflow_id,
            "status": status,
            "answer": answer,
            "as_of": now.isoformat(timespec="seconds"),
            "fact_intent": assessment.fact_intent,
            "territory_assumption": self._territory_assumption(assessment),
            "rewrite": rewrite.public_dict(),
            "plan": [item.public_dict() for item in plan],
            "citations": [],
            "claims": [],
            "memory_hit": False,
            "trace": {"provider_requests": requests, "gate_decisions": [item.public_dict() for item in preflight] + [decision.public_dict()]},
        }

    @staticmethod
    def _topic(query: str) -> str:
        title = __import__("re").search(r"《([^》]+)》", query)
        if title:
            return title.group(1)[:180]
        normalized = __import__("re").sub(r"[？?。！!]+$", "", query).strip()
        normalized = __import__("re").sub(r"(?:现任|当前|最新|现在|是谁|是什么|情况)$", "", normalized).strip()
        return normalized[:180]

    @staticmethod
    def _memory_hint(memories: list[dict]) -> dict | None:
        if not memories:
            return None
        try:
            return json.loads(memories[0]["memory_json"])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _territory_assumption(assessment: FactAssessment) -> dict | None:
        if not assessment.territory:
            return None
        return {
            "territory": assessment.territory,
            "label": assessment.territory_label,
            "assumed": assessment.territory_assumed,
        }

    @staticmethod
    def _display_date(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00")
            return f"{parsed.year} 年 {parsed.month} 月 {parsed.day} 日"
        except ValueError:
            return value

    @staticmethod
    def _extract_claims(
        assessment: FactAssessment,
        query: str,
        evidences: list[Evidence],
        policies: list[SourcePolicy],
        now: datetime,
    ) -> list[ResearchClaim]:
        if assessment.fact_intent == "EVENT_DATE":
            return extract_event_date_claims(evidences, query=query, policies=policies, now=now)
        if assessment.fact_intent == "POLICY_APPOINTMENT":
            structured = extract_policy_appointment_claims(evidences, query=query, policies=policies, now=now)
            if structured:
                return structured
        # Generic P0.5 intents do not manufacture a natural-language fact
        # value.  An explicit evidence-set claim is enough to bind the final
        # citation to the current chat/history result, while remaining
        # ineligible for reusable knowledge under the retention policy.
        return [
            ResearchClaim(
                claim_id=f"cl_evidence_{item.evidence_id.removeprefix('ev_')}",
                subject=query[:180], predicate="EVIDENCE_SET", value="VERIFIED_PUBLIC_EVIDENCE",
                territory=None, territory_label=None, event_state="", evidence_ids=(item.evidence_id,),
                source_tier=item.source_tier, source_policy_id=item.source_policy_id,
                claim_status="CANDIDATE",
                confidence=item.evidence_confidence or item.source_reputation,
                evidence_type=item.evidence_type, extraction_locator_type=item.extraction_locator_type,
            )
            for item in evidences
        ]

    def _detail_enrich(
        self,
        evidences: list[Evidence],
        query: str,
        *,
        assessment: FactAssessment,
        policies: list[SourcePolicy],
        now: datetime,
    ) -> tuple[list[Evidence], list[dict[str, str]]]:
        """Replace only detail-needed result snippets with bounded fragments.

        The original snippet never reaches persistence: the evidence table
        stores a hash only, and the replacement fragment is kept just long
        enough for deterministic claim extraction / current chat delivery.
        """
        if not self.detail_fetcher:
            return evidences, []
        match = re.search(r"《([^》]+)》", query)
        subject = match.group(1).strip() if match else ""
        if not subject:
            return evidences, []
        result: list[Evidence] = []
        trace: list[dict[str, str]] = []
        used_hosts: set[str] = set()
        # Reserve detail reads for the most promising *evidence*, based on the
        # content-first score.  Source reputation is only one input to that
        # score, so an unlisted but direct result remains eligible.
        ordered_evidences = sorted(
            evidences,
            key=lambda item: item.evidence_confidence,
            reverse=True,
        )
        for evidence in ordered_evidences:
            host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(evidence.canonical_url).hostname or ""
            # Do not let a title-level keyword shape bypass detail retrieval.
            # It incorrectly treated ``《戏台》定档7月25日，避开《长安的荔枝》``
            # as direct evidence for 《长安的荔枝》.  Only an already parsed,
            # semantically direct and currently meaningful claim can skip G2R.
            direct_claims = self._extract_claims(assessment, query, [evidence], policies, now)
            has_direct_claim = any(
                claim.date_role == ACTUAL_RELEASE
                or (claim.date_role == SCHEDULED_RELEASE and claim.value > now.date().isoformat())
                for claim in direct_claims
            ) if assessment.fact_intent == "EVENT_DATE" else bool(direct_claims)
            needs_detail = evidence.semantic_score < 0.75 or not has_direct_claim
            if not needs_detail or host in used_hosts or len(used_hosts) >= 3:
                result.append(evidence)
                continue
            used_hosts.add(host)
            fetched = self.detail_fetcher.fetch(
                evidence.canonical_url,
                entity=subject,
                predicates=("上映", "定档", "公映", "发行", "发布", "上线", "开幕", "开演"),
            )
            trace.append({"evidence_id": evidence.evidence_id, "status": fetched.status, "reason": fetched.rejection_reason or ""})
            if fetched.status == "DETAIL_FETCHED" and fetched.fragment:
                result.append(replace(
                    evidence,
                    snippet=fetched.fragment[:300],
                    evidence_type="DETAIL_EVIDENCE",
                    detail_fetch_status=fetched.status,
                    extraction_locator_type=fetched.locator_type,
                ))
            else:
                result.append(replace(
                    evidence,
                    detail_fetch_status=fetched.status,
                    detail_rejection_reason=fetched.rejection_reason,
                ))
        return result, trace

    @staticmethod
    def _dedupe_evidences(evidences: list[Evidence]) -> list[Evidence]:
        """One canonical URL has one persisted evidence row per run.

        Detail-enriched evidence wins over a later duplicate SERP card so a
        correct bounded detail fact cannot be overwritten before G6/storage.
        """
        by_id: dict[str, Evidence] = {}
        order: list[str] = []
        for evidence in evidences:
            prior = by_id.get(evidence.evidence_id)
            if prior is None:
                by_id[evidence.evidence_id] = evidence
                order.append(evidence.evidence_id)
            elif evidence.detail_fetch_status == "DETAIL_FETCHED" and prior.detail_fetch_status != "DETAIL_FETCHED":
                by_id[evidence.evidence_id] = evidence
        return [by_id[item] for item in order]

    @staticmethod
    def _corroboration_queries(
        claims: list[ResearchClaim], policies: list[SourcePolicy], assessment: FactAssessment, *, now: datetime,
    ) -> list[ResearchQuery]:
        if assessment.fact_intent != "EVENT_DATE":
            return []
        output: list[ResearchQuery] = []
        seen: set[tuple[str, str | None, str]] = set()
        for claim in claims:
            is_historical_schedule = claim.date_role == SCHEDULED_RELEASE and claim.value <= now.date().isoformat()
            if (claim.confidence >= 0.90 and not is_historical_schedule) or not claim.value:
                continue
            key = (claim.subject, claim.territory, claim.value)
            if key in seen:
                continue
            seen.add(key)
            territory = claim.territory_label or assessment.territory_label or "中国大陆"
            try:
                year, month, day = claim.value.split("-", 2)
                display_value = f"{year}年{int(month)}月{int(day)}日"
            except (TypeError, ValueError):
                display_value = claim.value
            output.append(ResearchQuery(
                f"《{claim.subject}》 {display_value} {territory} 正式上映",
                "verify_historical_scheduled_date" if is_historical_schedule else "corroborate_secondary_detail_lead",
                "general", "general", 1,
            ))
        return output[:2]

    @staticmethod
    def _delivered_evidences(evidences: list[Evidence], claims: list[ResearchClaim]) -> list[Evidence]:
        """Only evidence backing a deliverable claim crosses to chat/history."""
        accepted_ids = {evidence_id for claim in claims for evidence_id in claim.evidence_ids}
        delivered: list[Evidence] = []
        seen: set[str] = set()
        for evidence in evidences:
            if evidence.evidence_id not in accepted_ids or evidence.evidence_id in seen:
                continue
            seen.add(evidence.evidence_id)
            delivered.append(replace(evidence, snippet=evidence.snippet[:300]))
        return delivered

    @staticmethod
    def _quality(
        assessment: FactAssessment,
        evidences: list[Evidence],
        claims: list[ResearchClaim],
        policies: list[SourcePolicy],
        now: datetime,
    ) -> tuple[str, list[ResearchClaim]]:
        if assessment.fact_intent == "EVENT_DATE":
            return evaluate_event_date_claims(
                claims,
                evidences=evidences,
                assessment=assessment,
                policies=policies,
                now=now,
            )
        status = evaluate_generic_evidence(
            evidences,
            fact_intent=assessment.fact_intent,
            policies=policies,
            now=now,
        )
        eligible_ids = {
            item.evidence_id for item in evidences
            if item.relevance_score >= 0.45 and item.semantic_score >= 0.50 and item.freshness_score > 0.0
        }
        deliverable = [item for item in claims if set(item.evidence_ids).intersection(eligible_ids)]
        return status, deliverable if status in {"VERIFIED", "PARTIALLY_VERIFIED"} else []

    def _load_reusable_memory(
        self,
        memories: list[dict],
        *,
        assessment: FactAssessment,
        policies: list[SourcePolicy],
        now: datetime,
    ) -> tuple[list[ResearchClaim], list[Evidence]]:
        if assessment.fact_intent not in {"EVENT_DATE", "POLICY_APPOINTMENT"}:
            return [], []
        raw_claims: list[dict] = []
        for item in memories:
            try:
                memory = json.loads(item["memory_json"])
            except (TypeError, ValueError):
                continue
            if memory.get("fact_intent") != assessment.fact_intent or memory.get("status") not in {"VERIFIED", "PARTIALLY_VERIFIED"}:
                continue
            for claim in memory.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                if assessment.fact_intent == "EVENT_DATE" and (claim.get("predicate") != "RELEASE_DATE" or str(claim.get("event_state") or "") != "RELEASED"):
                    continue
                if assessment.fact_intent == "POLICY_APPOINTMENT" and claim.get("predicate") not in {"CURRENT_OFFICE_HOLDER", "CURRENT_POLICY_STATUS"}:
                    continue
                raw_claims.append(claim)
        evidence_ids = sorted({str(evidence_id) for claim in raw_claims for evidence_id in (claim.get("evidence_ids") or []) if evidence_id})
        if not raw_claims or not evidence_ids:
            return [], []
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = self.conn.execute(
            f"""SELECT e.evidence_id, e.title, e.canonical_url, e.publisher, e.published_at,
                       e.fetched_at, e.source_tier, e.source_policy_id, e.source_reputation, e.relevance_score,
                       e.freshness_score, e.semantic_score, e.evidence_confidence
                  FROM open_research_evidence e
                  JOIN open_research_runs r ON r.run_id=e.run_id
                 WHERE r.tenant_id=? AND r.user_id=? AND e.evidence_id IN ({placeholders})
                 ORDER BY e.created_at DESC""",
            (*self._memory_owner(memories), *evidence_ids),
        ).fetchall()
        evidences = [
            Evidence(
                evidence_id=row["evidence_id"], title=row["title"], canonical_url=row["canonical_url"],
                publisher=row["publisher"], published_at=row["published_at"], fetched_at=row["fetched_at"],
                source_tier=row["source_tier"], snippet="", source_policy_id=row["source_policy_id"],
                source_reputation=float(row["source_reputation"] or 0.55), relevance_score=float(row["relevance_score"] or 0),
                freshness_score=float(row["freshness_score"] or 0), semantic_score=float(row["semantic_score"] or 0),
                evidence_confidence=float(row["evidence_confidence"] or 0),
            )
            for row in rows
        ]
        visible_evidence_ids = {item.evidence_id for item in evidences}
        claims = []
        for item in raw_claims:
            claim_evidence_ids = tuple(str(value) for value in (item.get("evidence_ids") or []) if str(value) in visible_evidence_ids)
            if not claim_evidence_ids:
                continue
            claims.append(ResearchClaim(
                claim_id=str(item.get("claim_id") or f"cl_{uuid.uuid4().hex[:16]}"),
                subject=str(item.get("subject") or ""), predicate=str(item.get("predicate") or ""), value=str(item.get("value") or ""),
                territory=str(item.get("territory") or "") or None,
                territory_label=str(item.get("territory_label") or "") or None,
                event_state=str(item.get("event_state") or ""), evidence_ids=claim_evidence_ids,
                source_tier=str(item.get("source_tier") or "SECONDARY"),
                source_policy_id=str(item.get("source_policy_id") or "") or None,
                claim_status=str(item.get("claim_status") or "VERIFIED"),
                confidence=float(item.get("confidence") or 0),
                date_role=str(item.get("date_role") or "") or None,
            ))
        return claims, evidences

    @staticmethod
    def _memory_owner(memories: list[dict]) -> tuple[str, str]:
        # Active memories are loaded with an exact tenant/user predicate.  The
        # owner travels only to the local SQL lookup, never to a provider.
        first = memories[0] if memories else {}
        return str(first.get("tenant_id") or ""), str(first.get("user_id") or "")

    @staticmethod
    def _result(
        run_id: str,
        workflow_id: str | None,
        status: str,
        answer: dict,
        rewrite,
        plan: list,
        evidences: list[Evidence],
        brief: dict,
        assessment: FactAssessment,
        request_id: str,
        provider_requests: list[dict],
        preflight: list[GateDecision],
        quality: GateDecision,
        delivery: GateDecision,
        *,
        memory_hit: bool,
        force_refresh: bool,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "status": status,
            "answer": answer,
            "as_of": brief.get("as_of"),
            "fact_intent": assessment.fact_intent,
            "territory_assumption": OpenResearchService._territory_assumption(assessment),
            "rewrite": rewrite.public_dict(),
            "plan": [item.public_dict() for item in plan],
            "citations": [item.public_dict() for item in evidences],
            "claims": list(answer.get("claims") or []),
            "brief": brief,
            "memory_hit": memory_hit,
            "memory_hit_count": 1 if memory_hit else 0,
            "force_fresh": force_refresh,
            "retention_class": retention_class(assessment, [
                ResearchClaim(
                    claim_id=str(item.get("claim_id") or ""), subject=str(item.get("subject") or ""),
                    predicate=str(item.get("predicate") or ""), value=str(item.get("value") or ""),
                    territory=item.get("territory"), territory_label=item.get("territory_label"),
                    event_state=str(item.get("event_state") or ""), evidence_ids=tuple(item.get("evidence_ids") or []),
                    source_tier=str(item.get("source_tier") or "SECONDARY"), source_policy_id=item.get("source_policy_id"),
                    date_role=str(item.get("date_role") or "") or None,
                ) for item in (answer.get("claims") or [])
            ]) if status in {"VERIFIED", "PARTIALLY_VERIFIED"} else NO_MEMORY,
            "trace": {
                "request_id": request_id,
                "provider_requests": provider_requests,
                "gate_decisions": [item.public_dict() for item in preflight] + [quality.public_dict(), delivery.public_dict()],
            },
        }

    @staticmethod
    def _answer(
        status: str,
        rewrite,
        claims: list[ResearchClaim],
        assessment: FactAssessment,
        now: datetime,
        *,
        memory_hit: bool = False,
        evidences: list[Evidence] | None = None,
        synthesis_summary: str | None = None,
    ) -> dict[str, Any]:
        as_of = now.isoformat(timespec="seconds")
        public_claims = [item.public_dict() for item in claims]
        if status == "VERIFIED" and claims and assessment.fact_intent == "EVENT_DATE":
            claim = claims[0]
            territory = claim.territory_label or assessment.territory_label or "目标地区"
            date_text = OpenResearchService._display_date(claim.value)
            subject = f"《{claim.subject}》" if claim.subject else "该影片"
            source = "依据你的已核验记录" if memory_hit else "已由公开来源核验"
            verb = "已于" if claim.date_role == ACTUAL_RELEASE else "计划于"
            return {
                "status": status,
                "claim_status": "VERIFIED",
                "claims": public_claims,
                "text": f"{subject}{verb} {date_text}在{territory}上映。{source}。",
            }
        if status == "VERIFIED" and claims and assessment.fact_intent == "POLICY_APPOINTMENT":
            claim = claims[0]
            source = "依据你的已核验记录" if memory_hit else "已由公开来源核验"
            return {
                "status": status,
                "claim_status": "VERIFIED",
                "claims": public_claims,
                "text": f"{claim.subject}为{claim.value}。{source}。",
            }
        if status == "PARTIALLY_VERIFIED" and claims and assessment.fact_intent == "EVENT_DATE":
            details = "；".join(
                f"{item.territory_label or '地区待确认'}：{OpenResearchService._display_date(item.value)}"
                for item in claims
            )
            has_explicit_territory = any(item.territory for item in claims)
            prefix = (
                "已核验到其他地区信息"
                if has_explicit_territory
                else "已核验到上映日期，但地区待确认"
            )
            return {
                "status": status,
                "claim_status": "PARTIALLY_VERIFIED",
                "claims": public_claims,
                "text": f"{prefix}：{details}；当前证据置信度不足以形成确定结论。",
            }
        if status == "CONFLICTING":
            return {
                "status": status,
                "claim_status": "CONFLICTING",
                "claims": public_claims,
                "text": f"暂无法确认{assessment.territory_label or '目标地区'}上映日期：可信来源给出了相互冲突的日期。",
            }
        if status in {"VERIFIED", "PARTIALLY_VERIFIED"}:
            if synthesis_summary:
                return {
                    "status": status,
                    "claim_status": status,
                    "claims": public_claims,
                    "text": synthesis_summary[:240].rstrip("。；;，, ") + "。",
                }
            direct = next((re.sub(r"\s+", " ", item.snippet).strip() for item in (evidences or []) if item.snippet.strip()), "")
            if direct:
                # This is an evidence quotation/summarization, not a newly
                # inferred fact.  It keeps weather/price/status answers direct
                # without allowing a model to hallucinate a value.
                return {
                    "status": status,
                    "claim_status": status,
                    "claims": public_claims,
                    "text": direct[:220].rstrip("。；;，, ") + "。",
                }
            return {
                "status": status,
                "claim_status": status,
                "claims": public_claims,
                "text": "已基于可核验公开来源完成检索。",
            }
        missing = "日期、地区或可交付证据" if assessment.fact_intent == "EVENT_DATE" else "相关、时效和语义均满足要求的公开证据"
        return {
            "status": "NO_AUTHORITATIVE_SOURCE",
            "claim_status": "UNVERIFIED",
            "claims": [],
            "text": f"已找到相关线索，但尚未找到可直接佐证该事实所需的{missing}。",
        }

    @staticmethod
    def _brief(run_id: str, tenant_id: str, user_id: str, topic: str, status: str, answer: dict,
               evidences: list[Evidence], assessment: FactAssessment, now: datetime) -> dict[str, Any]:
        citations = [{key: value for key, value in item.public_dict().items() if key != "snippet"} for item in evidences]
        claims = list(answer.get("claims") or []) if status in {"VERIFIED", "PARTIALLY_VERIFIED", "CONFLICTING"} else []
        # Keep the Research→Office boundary usable even for a generic result
        # when the model is unavailable.  The provenance claim remains bound
        # to evidence quality rather than a hard-coded trusted-domain set.
        if status in {"VERIFIED", "PARTIALLY_VERIFIED"} and not claims:
            candidates = [item for item in evidences if item.evidence_confidence >= 0.60]
            if candidates:
                claims = [{
                    "claim_id": f"cl_evidence_{run_id[-10:]}",
                    "subject": topic,
                    "predicate": "EVIDENCE_SET",
                    "value": "VERIFIED_PUBLIC_EVIDENCE",
                    "territory": None,
                    "territory_label": None,
                    "event_state": None,
                    "claim_status": status,
                    "confidence": max(item.evidence_confidence for item in candidates),
                    "evidence_ids": [item.evidence_id for item in candidates],
                    "source_tier": candidates[0].source_tier,
                    "source_policy_id": candidates[0].source_policy_id,
                }]
        payload = {
            "brief_id": f"brief_{uuid.uuid4().hex[:16]}", "producer_run_id": run_id,
            "owner": {"tenant_id": tenant_id, "user_id": user_id}, "answer_status": status,
            "as_of": now.isoformat(timespec="seconds"), "freshness": "dynamic" if assessment.dynamic else "stable", "topic": topic,
            "fact_intent": assessment.fact_intent, "territory_assumption": OpenResearchService._territory_assumption(assessment),
            "claims": claims, "citations": citations,
            "limitations": [] if status == "VERIFIED" else ["信息未形成目标地区的可核验确定事实；请以引用和截至时间为准。"],
            "policy_version": "p0-2026-08-20", "expires_at": (now.replace(microsecond=0).timestamp() + 60 * 86400),
        }
        payload["expires_at"] = datetime.fromtimestamp(payload["expires_at"], tz=now.tzinfo).isoformat()
        payload["content_hash"] = summary_hash({key: value for key, value in payload.items() if key != "content_hash"})
        return payload

    def _archive(self, run_id: str, tenant_id: str, user_id: str, topic: str, rewrite, evidences: list[Evidence],
                 claims: list[ResearchClaim], assessment: FactAssessment, status: str, lifecycle: str, now: datetime) -> None:
        # ``official_domain`` is retained as a compatibility field for old
        # private memories.  It now records the highest-reputation delivered
        # source, not a mandatory approval tier.
        official = max(evidences, key=lambda item: item.evidence_confidence or item.source_reputation, default=None)
        archive_memory(
            self.conn,
            memory_id=f"mem_{uuid.uuid4().hex[:16]}",
            tenant_id=tenant_id,
            user_id=user_id,
            topic=topic,
            value={
                "aliases": list(rewrite.candidates),
                "status": status,
                "fact_intent": assessment.fact_intent,
                "last_verified_at": now.isoformat(timespec="seconds"),
                "official_domain": (__import__('urllib.parse', fromlist=['urlparse']).urlparse(official.canonical_url).hostname if official else None),
                "evidence_ids": [item.evidence_id for item in evidences],
                "claims": [item.public_dict() for item in claims],
                "retention_class": lifecycle,
            },
            now=now,
        )

    @staticmethod
    def _usage_credits(value: object) -> int:
        try:
            return max(0, min(int(value), 1_000_000))
        except (TypeError, ValueError):
            return 0

    def _record_provider_usage(self, run_id: str, tenant_id: str, user_id: str, requests: list[dict], outcome: str, now: datetime) -> None:
        """Store aggregate-only provider telemetry; queries and bodies stay out."""
        for item in requests:
            provider = str(item.get("provider") or "tavily").lower()
            if provider != "tavily":
                continue
            self.conn.execute(
                """INSERT INTO open_research_provider_usage(
                     usage_id, run_id, tenant_id, user_id, provider, latency_ms, credits, outcome, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (f"rusage_{uuid.uuid4().hex[:16]}", run_id, tenant_id, user_id, provider,
                 self._usage_credits(item.get("latency_ms")), self._usage_credits(item.get("credits")), outcome,
                 now.isoformat(timespec="seconds")),
            )
