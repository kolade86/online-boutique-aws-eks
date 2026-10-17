"""HTTP server: POST /alert (AlertManager webhook), POST /ask, GET /investigations,
and GET /chat, a page for asking questions through /ask.

One investigation runs at a time. /alert answers immediately and investigates
in a background thread, because an investigation takes longer than
AlertManager's webhook timeout. /ask answers in the response.

Alerts that cannot run (busy, recently investigated, resolved) get a 200
"skipped" reply rather than an error: AlertManager would retry an error, and
its repeat_interval already re-sends unresolved alerts later.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
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
CHAT_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.html")


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
        # For the same investigations: the alert payload and every tool output as
        # the agent saw it, so `cli.py capture` can turn an incident into a
        # regression case. Served only by /investigations/<id>/recording.
        self.recordings: deque = deque(maxlen=RECENT_RESULTS)

    def follow_up(self, incident: str, group: list) -> None:
        """Alerts for an incident investigated recently: if an agent PR is open for
        it, add the alerts not yet mentioned, and the current state, to that PR as
        one comment. AlertManager groups by service, so one call can carry several
        alert names. Runs in the background."""
        new, names = [], []
        for a in group:
            name = a.get("labels", {}).get("alertname", "")
            if name not in names and not self.followups.is_recent((incident, name)):
                new.append(a)
                names.append(name)
        if self._pr_opener is None or not new:
            return
        for name in names:
            self.followups.mark((incident, name))

        def work():
            import pr_tool
            try:
                existing = self._pr_opener.open_pr_for(incident)
                if existing is None or not self._config.open_prs:
                    return
                service = incident.split("/", 1)[1]
                state = self._collect_state(service) if self._collect_state else []
                body = pr_tool.followup_comment(str(alerts.key_of(new[0])), alerts=new,
                                                evidence=state)
                self._pr_opener.comment(existing["number"], body)
                log.info("follow-up comment", extra={"incident": incident, "alerts": names,
                                                    "pull_request": existing["number"]})
            except Exception:
                log.exception("follow-up comment failed", extra={"incident": incident})

        self._spawn(work)

    def try_start_alert(self, key: alerts.AlertKey, payload: dict, incident: str) -> bool:
        if not self._busy.acquire(blocking=False):
            return False
        self.dedup.mark(incident)
        # Every alert this investigation covers, not just the first: when
        # AlertManager re-sends the group, they are not news for the PR.
        for name in {key.alertname} | {a.get("labels", {}).get("alertname", "")
                                       for a in payload.get("alerts", [])}:
            self.followups.mark((incident, name))
        investigation_id = uuid.uuid4().hex[:8]

        def work():
            try:
                started_at = _utc_now()
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
                                 assessment, pr is not None and pr.get("status") in ("opened", "dry_run")),
                             started_at=started_at, alert=payload)
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
            started_at = _utc_now()
            assessment = self._assessment_tool()   # scored, but /ask never proposes a change
            agent = self._agent_factory([assessment.tool()])
            assessment.attach_to(agent)
            system = prompts.system_prompt("ask", self._config.app_namespace,
                                           self._config.max_tool_calls)
            result = agent.run(system, prompts.with_current_time(question))
            return self._record("ask", uuid.uuid4().hex[:8], question, result,
                                assessment=assessment, started_at=started_at)
        finally:
            self._busy.release()

    def _assessment_tool(self):
        return confidence.AssessmentTool(self._config.min_confidence_for_pr,
                                         self._config.min_confidence_for_rollback)

    def _record(self, kind: str, investigation_id: str, subject: str,
                result: Investigation, pull_request=None, assessment=None, note="",
                started_at: str = None, alert: dict = None) -> dict:
        entry = {
            "id": investigation_id,
            "kind": kind,
            "subject": subject,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "model": self._config.model,
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
        # Not redacted for the public: capture needs the real names to give the
        # whole case one consistent set of placeholders, and redacts before it
        # writes anything. Secrets were already removed before the agent saw them.
        self.recordings.appendleft({
            **entry, "answer": result.answer, "alert": alert,
            "evidence": [{"tool": e.tool, "input": e.input, "is_error": e.is_error,
                          "output": e.output} for e in result.evidence]})
        log.info("investigation result", extra={k: entry[k] for k in (
            "id", "kind", "subject", "outcome", "tool_calls", "elapsed_seconds", "answer",
            "note", "pull_request")} | {"score": entry["confidence"] and entry["confidence"]["score"]})
        return entry


def load_chat_page(path: str = CHAT_PAGE) -> tuple[str, dict]:
    """The chat page and the headers it is served with.

    Read once at start-up, so a missing or broken page stops the server
    starting instead of failing on the first request. The page's one inline
    script is allowed by its hash; nothing else may run, and the page may only
    talk to this server.
    """
    try:
        with open(path, encoding="utf-8") as f:
            page = f.read()
    except OSError as e:
        raise RuntimeError(f"chat page {path} cannot be read ({e}); the image must include "
                           "chat.html next to server.py") from None
    scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    if len(scripts) != 1:
        raise RuntimeError(f"chat page {path} must have exactly one inline <script>, "
                           f"found {len(scripts)}")
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode()
    headers = {
        "Content-Security-Policy": (
            f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
    return page, headers


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _start_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="investigation").start()


def create_app(config: Config, agent_factory: Callable[[list], Agent],
               spawn=_start_thread, pr_opener=None, collect_state=None,
               chat_page: str = CHAT_PAGE) -> FastAPI:
    if not config.api_token:
        raise ValueError("SREAGENT_API_TOKEN must be set to run the server")
    chat_html, chat_headers = load_chat_page(chat_page)

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

    # No token: the page holds no secrets. It asks for the token and sends it to /ask.
    @app.get("/chat", response_class=HTMLResponse)
    def chat():
        return HTMLResponse(chat_html, headers=chat_headers)

    @app.post("/alert", dependencies=[Depends(authorized)])
    def alert(payload: dict):
        decisions = []
        started = None
        firing = alerts.firing(payload)[:MAX_ALERTS_PER_PAYLOAD]
        # One decision per incident. AlertManager groups by service, so one
        # payload can hold several alert names (and pods) for one incident.
        incidents: dict = {}
        for a in firing:
            incidents.setdefault(alerts.incident_of(a), []).append(a)
        for incident, group in incidents.items():
            worth = [a for a in group if alerts.worth_investigating(a, config.app_namespace)[0]]
            # The first alert worth investigating names the decision, so one
            # that is not (another alert name, a lower severity) cannot hide it
            key = alerts.key_of(worth[0] if worth else group[0])
            decision = {"key": str(key), "incident": incident}
            age = runner.dedup.age(incident)
            if not worth:
                why = alerts.worth_investigating(group[0], config.app_namespace)[1]
                decisions.append({**decision, "status": "skipped", "reason": why})
            elif age is not None:
                decisions.append({**decision, "status": "skipped",
                                  "reason": f"part of incident {incident}, investigated "
                                            f"{int(age // 60)} minute(s) ago"})
                runner.follow_up(incident, worth)
            elif started is not None or not runner.try_start_alert(
                    key, {**payload, "alerts": worth}, incident):
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

    @app.get("/investigations/{investigation_id}/recording", dependencies=[Depends(authorized)])
    def recording(investigation_id: str):
        """One investigation with its alert payload and full tool outputs, for
        `cli.py capture`. Contains node names and IPs: keep it out of Git."""
        for entry in runner.recordings:
            if entry["id"] == investigation_id:
                return entry
        raise HTTPException(status_code=404, detail="no such investigation (only the most "
                                                    f"recent {RECENT_RESULTS} are kept)")

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
