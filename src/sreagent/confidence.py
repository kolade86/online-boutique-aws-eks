"""Evidence score: how well the agent's conclusion is supported by the evidence it cited.

The model never states a percentage. It fills in a structured assessment
(submit_assessment): what it concluded, whether the cause was observed in a
tool result or inferred, which alternatives it ruled out, whether the timing
fits, what it could not verify - citing its own tool calls by number. This
module turns that into a score with a fixed rule.

What the score is and is not:
- It measures how well the conclusion is supported by the evidence the agent
  says it has. It is NOT a calibrated probability that the diagnosis is right.
- The code checks that every cited tool call exists and succeeded, and which
  tools the citations come from. It cannot check that the call actually
  proves the claim; that judgement is the model's. Stage 6 evaluation is what
  checks whether scores track correctness.
- The same assessment and the same tool calls always give the same score.
- What the model says it could not rule out or verify is never subtracted. A
  rule that did so scored a candid model below a less candid one on the same
  evidence. Those items are listed as open questions for the reader, together
  with the calls that failed, which the code lists itself.

The whole rule is the table below. Tune it here.
"""

from dataclasses import dataclass, field

# --- The scoring rule -------------------------------------------------------------

# Raise this when the assessment form or the rule changes, so recorded runs made
# under the old one are reported apart (evaluation.py). 1: doubts subtracted.
# 2: the observed rule and decisive_check.
RULE_VERSION = 2

POINTS = {
    "cause_observed": 50,         # meets the observed rule below
    "cause_inferred": 30,         # deduced from what was seen, or "observed" that does not meet the rule
    "corroborated": 10,           # the cause is cited from >= CORROBORATION_TOOLS different tools
    "decisive_check": 10,         # the check that separates the cause from the leading alternative, cited
    "alternative_ruled_out": 10,  # each, at most MAX_RULED_OUT, needs a valid citation
    "timing_matches": 20,         # needs a valid citation
    "timing_mismatch": -20,
}
CORROBORATION_TOOLS = 2
MAX_RULED_OUT = 2
# A single explanation that was never tested against another is the pattern of
# every wrong answer in regressions/: without at least one alternative ruled
# out, the score cannot reach the default PR threshold.
CAP_NO_ALTERNATIVE_RULED_OUT = 55
# An inferred cause never opens a pull request (change_blocked), so its score
# stays below the PR threshold too, however much else is cited.
CAP_INFERRED = 60
INCONCLUSIVE_SCORE = 20

# The observed rule. A cause counts as observed only when the citations cover
# both ends of it:
# - the workload's side: what happened to the pods, which is also what the alert
#   or the question already says;
# - the explaining side: the setting, change or controller state behind it. The
#   decisive check must cite this side.
# One tool's view of the failing pods is a symptom, however clearly it shows it:
# a wrong answer in regressions/ called "readiness probes" an observed cause
# from list_events alone and scored 73.
WORKLOAD_TOOLS = frozenset({"list_pods", "list_events", "get_pod_logs",
                            "prometheus_query", "prometheus_query_range"})
# get_resource is explaining-side only: it returns the configuration and
# controller state behind a failure (a Service's selector, an Argo CD sync
# result, a node's allocatable capacity), not what happened to the pods. In
# both groups, one get_resource call could meet the rule on its own.
EXPLAINING_TOOLS = frozenset({"read_repo_file", "recent_chart_commits", "describe_deployment",
                              "list_deployments", "list_hpas", "get_resource"})

# Limits the strict JSON schema cannot express; enforced here.
MAX_TEXT = 300
# The decisive check names what was compared with what, on both sides: at 300
# characters it was cut off mid-sentence in pull requests
MAX_DECISIVE_TEXT = 800
MAX_ITEMS = 5

TOOL_NAME = "submit_assessment"

CONCLUSIONS = ("cause_found", "no_problem", "inconclusive")
SUPPORTS = ("observed", "inferred")
TIMINGS = ("matches", "does_not_match", "not_checked")
RESULTS = ("ruled_out", "not_ruled_out")

_CALLS = {"type": "array", "items": {"type": "integer"},
          "description": "Numbers of your own tool calls (1 = your first call) whose results show this"}

# strict: true tool input - every object closes with additionalProperties: false
# and lists every property as required (empty string / empty list when unused).
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["conclusion", "cause", "cause_support", "cause_evidence", "decisive_check",
                 "alternatives", "timing", "timing_evidence", "unverified"],
    "properties": {
        "conclusion": {"type": "string", "enum": list(CONCLUSIONS),
                       "description": "cause_found: you identified the cause. no_problem: the evidence "
                                      "shows no problem (or that it has stopped). inconclusive: neither."},
        "cause": {"type": "string",
                  "description": "One sentence: the cause, or for no_problem what shows there is none. "
                                 "Empty for inconclusive."},
        "cause_support": {"type": "string", "enum": list(SUPPORTS),
                          "description": "observed: your results show both the failure in the cluster "
                                         "(pods, events, logs, metrics) and the setting or change that "
                                         "explains it (the repository, a commit, the Deployment, the HPA). "
                                         "inferred: you deduced it, or saw only one of the two."},
        "cause_evidence": _CALLS,
        "decisive_check": {
            "type": "object", "additionalProperties": False,
            "required": ["what", "evidence"],
            "description": "The one check that separates your cause from the most likely alternative. "
                           "Empty text and an empty list if you made no such check.",
            "properties": {
                "what": {"type": "string", "description": "What you checked and what it showed, in one "
                                                          "or two sentences"},
                "evidence": _CALLS,
            },
        },
        "alternatives": {
            "type": "array",
            "description": "Other causes you considered (at most 5)",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["cause", "result", "evidence"],
                "properties": {
                    "cause": {"type": "string"},
                    "result": {"type": "string", "enum": list(RESULTS)},
                    "evidence": _CALLS,
                },
            },
        },
        "timing": {"type": "string", "enum": list(TIMINGS),
                   "description": "Whether the cause began before the alert, close enough to explain it"},
        "timing_evidence": _CALLS,
        "unverified": {
            "type": "array",
            "description": "What you could not check (at most 5)",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["what", "could_change_diagnosis"],
                "properties": {
                    "what": {"type": "string"},
                    "could_change_diagnosis": {"type": "boolean"},
                },
            },
        },
    },
}


@dataclass
class Reason:
    points: int
    text: str


@dataclass
class Score:
    value: int
    reasons: list[Reason]
    assessment: dict          # the assessment as scored (cleaned)
    # True only for cause_found that meets the observed rule: the hard
    # requirement for proposing a change
    observed: bool = False
    # How the conclusion was credited: "observed" or "inferred" ("" if inconclusive)
    support: str = ""
    # Why a cause claimed as observed was not counted as one ("" otherwise)
    not_observed: str = ""
    # Not scored: what the model could not rule out or verify, and the calls that failed
    open_questions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"score": self.value, "observed": self.observed,
                "reasons": [{"points": r.points, "text": r.text} for r in self.reasons],
                "open_questions": self.open_questions,
                "assessment": self.assessment}


# --- Cleaning -----------------------------------------------------------------------

def _text(value, limit: int = MAX_TEXT) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _calls(value) -> list:
    if not isinstance(value, list):
        return []
    seen, out = set(), []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int) or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _items(value) -> list:
    return [v for v in value if isinstance(v, dict)][:MAX_ITEMS] if isinstance(value, list) else []


def clean(raw) -> tuple[dict, list[Reason]]:
    """Coerce anything into a well-formed assessment, noting what was fixed.

    The strict tool schema already guarantees types and enums from the
    Anthropic API; this also covers other providers, tests and replays.
    """
    notes = []
    raw = raw if isinstance(raw, dict) else {}
    decisive = raw.get("decisive_check") if isinstance(raw.get("decisive_check"), dict) else {}

    def pick(key, allowed, default):
        v = raw.get(key)
        if v in allowed:
            return v
        notes.append(Reason(0, f"'{key}' was {v!r}; treated as '{default}'"))
        return default

    a = {
        "conclusion": pick("conclusion", CONCLUSIONS, "inconclusive"),
        "cause": _text(raw.get("cause")),
        "cause_support": pick("cause_support", SUPPORTS, "inferred"),
        "cause_evidence": _calls(raw.get("cause_evidence")),
        # absent from assessments recorded before the field existed
        "decisive_check": {"what": _text(decisive.get("what"), MAX_DECISIVE_TEXT),
                           "evidence": _calls(decisive.get("evidence"))},
        "alternatives": [{"cause": _text(x.get("cause")),
                          "result": x.get("result") if x.get("result") in RESULTS else "not_ruled_out",
                          "evidence": _calls(x.get("evidence"))}
                         for x in _items(raw.get("alternatives"))],
        "timing": pick("timing", TIMINGS, "not_checked"),
        "timing_evidence": _calls(raw.get("timing_evidence")),
        "unverified": [{"what": _text(x.get("what")),
                        "could_change_diagnosis": x.get("could_change_diagnosis") is not False}
                       for x in _items(raw.get("unverified"))],
    }
    for key in ("alternatives", "unverified"):
        if isinstance(raw.get(key), list) and len(raw[key]) > MAX_ITEMS:
            notes.append(Reason(0, f"only the first {MAX_ITEMS} '{key}' were counted"))
    if a["conclusion"] == "cause_found" and not a["cause"]:
        notes.append(Reason(0, "conclusion 'cause_found' with no cause stated; treated as inconclusive"))
        a["conclusion"] = "inconclusive"
    return a, notes


# --- Scoring -------------------------------------------------------------------------

def _valid(calls: list, evidence: list, dropped: list) -> list:
    """Citations that point at a real, successful evidence-gathering call."""
    ok = []
    for n in calls:
        if not 1 <= n <= len(evidence):
            dropped.append(f"call {n} does not exist")
        elif evidence[n - 1].tool == TOOL_NAME:
            dropped.append(f"call {n} is an assessment, not evidence")
        elif evidence[n - 1].is_error:
            dropped.append(f"call {n} ({evidence[n - 1].tool}) failed")
        else:
            ok.append(n)
    return ok


def _cite(calls: list, evidence: list) -> str:
    return ", ".join(f"{evidence[n - 1].tool} (call {n})" for n in calls)


def _why_not_observed(cause_calls: list, decisive_calls: list, evidence: list) -> str:
    """Why these citations do not make an observed cause ("" if they do)."""
    if not cause_calls:
        return "no valid citation"
    if not decisive_calls:
        return "no decisive check with a valid citation"
    if not any(evidence[n - 1].tool in EXPLAINING_TOOLS for n in decisive_calls):
        return (f"the decisive check cites only {_cite(decisive_calls, evidence)}, which shows what "
                "happened to the pods, not the setting or change that explains it")
    if not any(evidence[n - 1].tool in WORKLOAD_TOOLS for n in cause_calls + decisive_calls):
        return "nothing cited shows the failure in the cluster (pods, events, logs or metrics)"
    return ""


def score(raw_assessment, evidence: list) -> Score:
    """The evidence score for an assessment, given the run's tool calls (agent.Evidence)."""
    a, reasons = clean(raw_assessment)
    dropped: list[str] = []
    failed = [f"{e.tool} (call {n})" for n, e in enumerate(evidence, 1)
              if e.is_error and e.tool != TOOL_NAME]
    questions = ["Calls that failed or returned nothing: " + ", ".join(failed)] if failed else []

    if a["conclusion"] == "inconclusive":
        reasons.append(Reason(INCONCLUSIVE_SCORE, "Inconclusive: no cause identified"))
        return Score(INCONCLUSIVE_SCORE, reasons, a, open_questions=questions)

    subject = "Cause" if a["conclusion"] == "cause_found" else "No problem"
    total = 0

    cause_calls = _valid(a["cause_evidence"], evidence, dropped)
    decisive = a["decisive_check"]
    decisive_calls = _valid(decisive["evidence"], evidence, dropped) if decisive["what"] else []
    not_observed = (_why_not_observed(cause_calls, decisive_calls, evidence)
                    if a["cause_support"] == "observed" else "")
    observed = a["cause_support"] == "observed" and not not_observed
    if observed:
        total += POINTS["cause_observed"]
        reasons.append(Reason(POINTS["cause_observed"],
                              f"{subject} directly observed: {_cite(cause_calls, evidence)}"))
    else:
        why = (f" (claimed observed, but {not_observed})" if not_observed
               else (f": {_cite(cause_calls, evidence)}" if cause_calls else ", with no citation"))
        total += POINTS["cause_inferred"]
        reasons.append(Reason(POINTS["cause_inferred"], f"{subject} inferred{why}"))

    tools = {evidence[n - 1].tool for n in cause_calls}
    if len(tools) >= CORROBORATION_TOOLS:
        total += POINTS["corroborated"]
        reasons.append(Reason(POINTS["corroborated"], f"Corroborated by {len(tools)} different tools"))

    if decisive_calls:
        total += POINTS["decisive_check"]
        reasons.append(Reason(POINTS["decisive_check"],
                              f"Decisive check: {decisive['what']} ({_cite(decisive_calls, evidence)})"))
    elif decisive["what"]:
        reasons.append(Reason(0, f"Decisive check claimed without a valid citation: {decisive['what']}"))
    else:
        reasons.append(Reason(0, "No decisive check named"))

    ruled_out = 0
    for alt in a["alternatives"]:
        calls = _valid(alt["evidence"], evidence, dropped)
        name = alt["cause"] or "an unnamed alternative"
        if alt["result"] == "ruled_out" and calls:
            if ruled_out < MAX_RULED_OUT:
                total += POINTS["alternative_ruled_out"]
                reasons.append(Reason(POINTS["alternative_ruled_out"],
                                      f"Ruled out: {name} ({_cite(calls, evidence)})"))
            else:
                reasons.append(Reason(0, f"Ruled out: {name} (beyond the {MAX_RULED_OUT} that count)"))
            ruled_out += 1
        elif alt["result"] == "ruled_out":
            reasons.append(Reason(0, f"Claimed ruled out without a valid citation: {name}"))
        else:
            questions.append(f"Not ruled out: {name}")

    timing_calls = _valid(a["timing_evidence"], evidence, dropped)
    if a["timing"] == "matches" and timing_calls:
        total += POINTS["timing_matches"]
        reasons.append(Reason(POINTS["timing_matches"],
                              f"Timing matches the alert ({_cite(timing_calls, evidence)})"))
    elif a["timing"] == "matches":
        reasons.append(Reason(0, "Timing said to match, but with no valid citation"))
    elif a["timing"] == "does_not_match":
        total += POINTS["timing_mismatch"]
        reasons.append(Reason(POINTS["timing_mismatch"], "Timing does not match the alert"))
    else:
        reasons.append(Reason(0, "Timing not checked"))

    for item in a["unverified"]:
        what = item["what"] or "an unnamed item"
        questions.append(f"Not verified, could change the diagnosis: {what}"
                         if item["could_change_diagnosis"] else f"Not verified (detail only): {what}")

    if dropped:
        reasons.append(Reason(0, "Citations ignored: " + "; ".join(dropped)))

    if ruled_out == 0 and total > CAP_NO_ALTERNATIVE_RULED_OUT:
        reasons.append(Reason(CAP_NO_ALTERNATIVE_RULED_OUT - total,
                              f"Capped at {CAP_NO_ALTERNATIVE_RULED_OUT}: no alternative explanation "
                              "was ruled out with evidence"))
        total = CAP_NO_ALTERNATIVE_RULED_OUT
    if not observed and total > CAP_INFERRED:
        reasons.append(Reason(CAP_INFERRED - total,
                              f"Capped at {CAP_INFERRED}: the {subject.lower()} is inferred, not observed"))
        total = CAP_INFERRED

    if total > 100:
        reasons.append(Reason(100 - total, "The score stops at 100"))

    return Score(max(0, min(100, total)), reasons, a,
                 observed=observed and a["conclusion"] == "cause_found",
                 support="observed" if observed else "inferred",
                 not_observed=not_observed, open_questions=questions)


def format_reasons(s: Score) -> list[str]:
    lines = [f"{r.points:+4d}  {r.text}" if r.points else f"   .  {r.text}" for r in s.reasons]
    if s.open_questions:
        lines += ["Open questions (not scored):"] + [f"   ?  {q}" for q in s.open_questions]
    return lines


# --- The tool the model calls ----------------------------------------------------------

MAX_SUBMISSIONS = 2   # the first assessment and at most one revision


@dataclass
class Submission:
    after_calls: int      # evidence-gathering calls made before it
    score: Score

    def as_dict(self) -> dict:
        return {"after_calls": self.after_calls, **self.score.as_dict()}


class AssessmentTool:
    """submit_assessment: records each assessment and returns its score to the model.

    A revision is allowed once, and only after new evidence-gathering calls,
    so the model cannot resubmit the same evidence until the score suits it.
    attach() gives it the run's tool calls (agent.Agent.evidence).
    """

    def __init__(self, min_for_pr: int, min_for_rollback: int):
        self.min_for_pr = min_for_pr
        self.min_for_rollback = min_for_rollback
        self.submissions: list[Submission] = []
        self._evidence = lambda: []

    def attach(self, get_evidence) -> None:
        self._evidence = get_evidence

    def attach_to(self, agent) -> None:
        """Wire into an agent.Agent: citations check its tool calls; if it finishes
        without an assessment it is reminded once; assessing never uses up the
        tool-call budget."""
        self.attach(lambda: agent.evidence)
        agent.before_finish = self.reminder
        agent.unbudgeted = {TOOL_NAME}

    def reminder(self):
        if self.submissions:
            return None
        return ("You have not called submit_assessment. Call it now for the report you just "
                "wrote, citing your tool calls by number. Do not rewrite the report: after the "
                "tool call, reply with one word, done.")

    @property
    def latest(self):
        return self.submissions[-1].score if self.submissions else None

    def summary(self) -> dict:
        latest = self.latest
        return {"score": latest.value if latest else None,
                "display": display(latest),
                "observed": latest.observed if latest else False,
                "min_for_pr": self.min_for_pr,
                "min_for_rollback": self.min_for_rollback,
                "reasons": [{"points": r.points, "text": r.text} for r in latest.reasons] if latest else [],
                "open_questions": latest.open_questions if latest else [],
                "submissions": [s.as_dict() for s in self.submissions]}

    def handle(self, args: dict) -> str:
        from tools import ToolError
        evidence = self._evidence()
        gathered = sum(1 for e in evidence if e.tool != TOOL_NAME)
        if len(self.submissions) >= MAX_SUBMISSIONS:
            raise ToolError("Not recorded: the assessment was already revised once. Write your report.")
        if self.submissions and gathered == self.submissions[-1].after_calls:
            raise ToolError("Not recorded: an assessment can only be revised after new tool calls. "
                            "Gather more evidence first, or write your report.")
        s = score(args, evidence)
        self.submissions.append(Submission(gathered, s))
        blocked = change_blocked(s, self.min_for_pr)
        verdict = (f"A configuration change may be proposed (it needs {self.min_for_pr}, "
                   f"or {self.min_for_rollback} for an image.tag rollback)."
                   if not blocked else
                   f"Do not propose a configuration change: {blocked}. Report the diagnosis "
                   "and say what evidence is missing.")
        revision = "" if len(self.submissions) == 1 else " (revision)"
        return (f"Recorded{revision}. Evidence score: {s.value}/100.\n"
                + "\n".join(format_reasons(s)) + "\n" + verdict)

    def tool(self):
        from tools import Tool
        return Tool(
            TOOL_NAME,
            "Record your evidence assessment before writing the final report, citing your tool calls "
            "by number (1 = your first call). The code turns it into an evidence score: an observed "
            "cause counts for more than an inferred one; a cited decisive check, alternatives ruled "
            "out with evidence, and timing that fits the alert add to it. Alternatives you could not "
            "rule out and things you could not verify do not lower the score: they are shown to the "
            "reviewer as open questions, so list them all. Be honest: an inflated assessment only "
            "delays a human. You may revise it once, after gathering more evidence.",
            SCHEMA, self.handle, strict=True)


def display(score) -> str:
    """How a score is shown to people: "96/100", or "not assessed"."""
    return f"{score.value}/100" if score is not None else "not assessed"


def change_blocked(score, needed: int) -> str:
    """Why this assessment may not lead to a proposed change ("" if it may).

    The one rule shared by the proposal tool, the final check before a PR is
    opened, and the note under the report.
    """
    if score is None:
        return "no evidence assessment was submitted, so the evidence was not scored"
    if not score.observed:
        if score.assessment.get("conclusion") != "cause_found":
            return "the conclusion is not a cause that a change could fix"
        if score.not_observed:
            return (f"the cause does not count as observed ({score.not_observed}) - "
                    "a change needs an observed cause")
        return ("the cause is inferred, not directly observed in a cited tool result - "
                "a change needs an observed cause")
    if score.value < needed:
        why = ("; ".join(r.text for r in score.reasons if r.points < 0)
               or "too little of the conclusion is backed by cited evidence")
        return f"the evidence score is {score.value}/100, below the {needed} needed ({why})"
    return ""


def note_no_change(assessment, proposed: bool = False) -> str:
    """Written by code under a report when the evidence does not allow a change."""
    if assessment is None or proposed:
        return ""
    blocked = change_blocked(assessment.latest, assessment.min_for_pr)
    return f"No change proposed: {blocked}." if blocked else ""
