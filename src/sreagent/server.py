"""HTTP server: POST /alert (AlertManager webhook), POST /ask, GET /investigations.

One investigation runs at a time. /alert answers immediately and investigates
in a background thread, because an investigation takes longer than
AlertManager's webhook timeout. /ask answers in the response.

Alerts that cannot run (busy, recently investigated, resolved) get a 200
"skipped" reply rather than an error: AlertManager would retry an error, and
its repeat_interval already re-sends unresolved alerts later.
"""

import hmac
import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import alerts
import confidence
import prompts
from agent import Agent, Investigation
from config import Config
from redact import redact_public

log = logging.getLogger("sreagent.server")

MAX_ALERTS_PER_PAYLOAD = 20
RECENT_RESULTS = 20


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class Runner:
    """Runs one investigation at a time and keeps the most recent results."""

    def __init__(self, agent_factory: Callable[[list], Agent], config: Config,
                 spawn: Callable[[Callable[[], None]], None], pr_opener=None,
                 collect_state: Callable[[str], list] = None):
        # agent_factory(extra_tools) builds an Agent; alerts get the PR
        # proposal tool, /ask never does.
        self._agent_factory = agent_factory
        self._config = config
        self._spawn = spawn
        self._pr_opener = pr_opener
        # collect_state(service) -> [(label, output)]: read-only tools run by
        # code, for comments about alerts that are not investigated again
        self._collect_state = collect_state
        self._busy = threading.Lock()
        # One investigation per incident (namespace/service) within the window
        self.dedup = alerts.Deduplicator(config.dedup_minutes * 60)
        # One follow-up comment per incident and alert name within the window
        self.followups = alerts.Deduplicator(config.dedup_minutes * 60)
        self.results: deque = deque(maxlen=RECENT_RESULTS)

    def follow_up(self, incident: str, alert: dict) -> None:
        """An alert for an incident investigated recently: if an agent PR is open for
        it, add the alert and the current state to that PR. Runs in the background."""
        name = alert.get("labels", {}).get("alertname", "")
        if self._pr_opener is None or self.followups.is_recent((incident, name)):
            return
        self.followups.mark((incident, name))

        def work():
            import pr_tool
            try:
                existing = self._pr_opener.open_pr_for(incident)
                if existing is None or not self._config.open_prs:
                    return
                service = incident.split("/", 1)[1]
                state = self._collect_state(service) if self._collect_state else []
                body = pr_tool.followup_comment(str(alerts.key_of(alert)), alert=alert,
                                                evidence=state)
                self._pr_opener.comment(existing["number"], body)
                log.info("follow-up comment", extra={"incident": incident, "alert": name,
                                                    "pull_request": existing["number"]})
            except Exception:
                log.exception("follow-up comment failed", extra={"incident": incident})

        self._spawn(work)

    def try_start_alert(self, key: alerts.AlertKey, payload: dict, incident: str) -> bool:
        if not self._busy.acquire(blocking=False):
            return False
        self.dedup.mark(incident)
        self.followups.mark((incident, key.alertname))
        investigation_id = uuid.uuid4().hex[:8]

        def work():
            try:
                assessment = self._assessment_tool()
                proposal = None
                if self._pr_opener is not None:
                    import pr_tool  # needs ruamel.yaml
                    proposal = pr_tool.ProposalTool(self._pr_opener, str(key), assessment,
                                                    self._config.recent_merge_minutes)
                agent = self._agent_factory([assessment.tool()]
                                            + ([proposal.tool()] if proposal else []))
                assessment.attach_to(agent)
                system = prompts.system_prompt("investigate", self._config.app_namespace,
                                               self._config.max_tool_calls)
                result = agent.run(system, prompts.with_current_time(prompts.alert_task(payload)))
                pr = None
                if proposal is not None:
                    pr = pr_tool.finish(proposal, result, str(key), investigation_id,
                                        self._config.open_prs, assessment)
                self._record("alert", investigation_id, str(key), result, pull_request=pr,
                             assessment=assessment, note=confidence.note_no_change(
                                 assessment, pr is not None and pr.get("status") in ("opened", "dry_run")))
            except Exception:
                log.exception("investigation crashed", extra={"id": investigation_id})
            finally:
                self._busy.release()

        self._spawn(work)
        return True

    def ask(self, question: str):
        """Run synchronously. Returns None when another investigation is running."""
        if not self._busy.acquire(blocking=False):
            return None
        try:
            assessment = self._assessment_tool()   # scored, but /ask never proposes a change
            agent = self._agent_factory([assessment.tool()])
            assessment.attach_to(agent)
            system = prompts.system_prompt("ask", self._config.app_namespace,
                                           self._config.max_tool_calls)
            result = agent.run(system, prompts.with_current_time(question))
            return self._record("ask", uuid.uuid4().hex[:8], question, result,
                                assessment=assessment)
        finally:
            self._busy.release()

    def _assessment_tool(self):
        return confidence.AssessmentTool(self._config.min_confidence_for_pr,
                                         self._config.min_confidence_for_rollback)

    def _record(self, kind: str, investigation_id: str, subject: str,
                result: Investigation, pull_request=None, assessment=None, note="") -> dict:
        entry = {
            "id": investigation_id,
            "kind": kind,
            "subject": subject,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": result.outcome,
            "tool_calls": len(result.evidence),
            "max_tool_calls": self._config.max_tool_calls,
            "elapsed_seconds": round(result.elapsed_seconds, 1),
            # What people read: no secrets, node names, IPs or account IDs
            "answer": redact_public(result.answer),
            "note": note,
            "confidence": assessment.summary() if assessment else None,
            "evidence": [{"tool": e.tool, "input": e.input, "is_error": e.is_error}
                         for e in result.evidence],
            "pull_request": json.loads(redact_public(json.dumps(pull_request))) if pull_request else None,
        }
        self.results.appendleft(entry)
        log.info("investigation result", extra={k: entry[k] for k in (
            "id", "kind", "subject", "outcome", "tool_calls", "elapsed_seconds", "answer",
            "note", "pull_request")} | {"score": entry["confidence"] and entry["confidence"]["score"]})
        return entry


def _start_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="investigation").start()


def create_app(config: Config, agent_factory: Callable[[list], Agent],
               spawn=_start_thread, pr_opener=None, collect_state=None) -> FastAPI:
    if not config.api_token:
        raise ValueError("SREAGENT_API_TOKEN must be set to run the server")

    app = FastAPI(title="sreagent", docs_url=None, redoc_url=None, openapi_url=None)
    runner = Runner(agent_factory, config, spawn, pr_opener, collect_state)
    app.state.runner = runner

    def authorized(authorization: str = Header(default="")):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                token.encode(), config.api_token.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/alert", dependencies=[Depends(authorized)])
    def alert(payload: dict):
        decisions = []
        started = None
        firing = alerts.firing(payload)[:MAX_ALERTS_PER_PAYLOAD]
        for a in firing:
            key, incident = alerts.key_of(a), alerts.incident_of(a)
            if any(d["incident"] == incident for d in decisions):
                continue  # one decision per incident (several pods or alerts of one service)
            decision = {"key": str(key), "incident": incident}
            worth, why = alerts.worth_investigating(a, config.app_namespace)
            age = runner.dedup.age(incident)
            if not worth:
                decisions.append({**decision, "status": "skipped", "reason": why})
            elif age is not None:
                decisions.append({**decision, "status": "skipped",
                                  "reason": f"part of incident {incident}, investigated "
                                            f"{int(age // 60)} minute(s) ago"})
                runner.follow_up(incident, a)
            elif started is not None or not runner.try_start_alert(
                    key, {**payload, "alerts": [
                        x for x in firing if alerts.incident_of(x) == incident
                        and alerts.worth_investigating(x, config.app_namespace)[0]]}, incident):
                decisions.append({**decision, "status": "skipped",
                                  "reason": "another investigation is running"})
            else:
                started = incident
                decisions.append({**decision, "status": "accepted"})
        if not decisions:
            decisions.append({"key": None, "incident": None, "status": "skipped",
                              "reason": "no firing alerts"})
        log.info("alert received", extra={"decisions": decisions})
        return {"decisions": decisions}

    @app.post("/ask", dependencies=[Depends(authorized)])
    def ask(request: AskRequest):
        entry = runner.ask(request.question)
        if entry is None:
            raise HTTPException(status_code=409, detail="another investigation is running")
        return entry

    @app.get("/investigations", dependencies=[Depends(authorized)])
    def investigations():
        return list(runner.results)

    return app


def main():
    import uvicorn

    import logger
    import wiring

    logger.configure()
    config = Config.from_env()
    registry = wiring.build_registry(config)

    def collect_state(service: str) -> list:
        """Current pods and events for a service, for follow-up comments."""
        out = []
        for name, args in (("list_pods", {"app": service}), ("list_events", {"app": service})):
            output, _ = registry.run(name, args)
            out.append((f"{name} app={service}", output))
        return out

    app = create_app(config, lambda extra_tools: wiring.build_agent(config, extra_tools),
                     pr_opener=wiring.build_pr_opener(config), collect_state=collect_state)
    uvicorn.run(app, host="0.0.0.0", port=config.port, access_log=False)


if __name__ == "__main__":
    main()
