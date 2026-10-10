"""Evidence score: how well the agent's conclusion is supported by the evidence it cited.

The model never states a percentage. It fills in a structured assessment
(submit_assessment): what it concluded, whether the cause was observed in a
tool result or inferred, which alternatives it ruled out, whether the timing
fits, what it could not verify - citing its own tool calls by number. This
module turns that into a score with a fixed rule.

What the score is and is not:
- It measures how well the conclusion is supported by the evidence the agent
  says it has. It is NOT a calibrated probability that the diagnosis is right.
- The code checks that every cited tool call exists and succeeded. It cannot
  check that the call actually proves the claim; that judgement is the
  model's. Stage 6 evaluation is what checks whether scores track
  correctness.
- The same assessment and the same tool calls always give the same score.

The whole rule is the table below. Tune it here.
"""

from dataclasses import dataclass, field

# --- The scoring rule -------------------------------------------------------------

POINTS = {
    "cause_observed": 50,         # a cited tool result shows the cause itself
    "cause_inferred": 30,         # deduced from what was seen (or "observed" without a valid citation)
    "corroborated": 10,           # the cause is cited from >= CORROBORATION_TOOLS different tools
    "alternative_ruled_out": 10,  # each, at most MAX_RULED_OUT, needs a valid citation
    "alternative_open": -15,      # each alternative left not ruled out
    "timing_matches": 20,         # needs a valid citation
    "timing_mismatch": -20,
    "unverified_major": -15,      # each unverified item that could change the diagnosis
    "unverified_minor": -2,       # each detail-only item, at most MAX_MINOR_PENALTY in total
}
CORROBORATION_TOOLS = 2
MAX_RULED_OUT = 2
MAX_MINOR_PENALTY = 6
# A single explanation that was never tested against another is the pattern of
# every wrong answer in regressions/: without at least one alternative ruled
# out, the score cannot reach the default PR threshold.
CAP_NO_ALTERNATIVE_RULED_OUT = 55
INCONCLUSIVE_SCORE = 20

# Limits the strict JSON schema cannot express; enforced here.
MAX_TEXT = 300
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
    "required": ["conclusion", "cause", "cause_support", "cause_evidence", "alternatives",
                 "timing", "timing_evidence", "unverified"],
    "properties": {
        "conclusion": {"type": "string", "enum": list(CONCLUSIONS),
                       "description": "cause_found: you identified the cause. no_problem: the evidence "
                                      "shows no problem (or that it has stopped). inconclusive: neither."},
        "cause": {"type": "string",
                  "description": "One sentence: the cause, or for no_problem what shows there is none. "
                                 "Empty for inconclusive."},
        "cause_support": {"type": "string", "enum": list(SUPPORTS),
                          "description": "observed: a tool result shows the cause itself (an OOMKilled exit, "
                                         "a commit diff, a metric above a limit). inferred: you deduced it."},
        "cause_evidence": _CALLS,
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
    # True only for cause_found with "observed" credited (valid citations):
    # the hard requirement for proposing a change
    observed: bool = False

    def as_dict(self) -> dict:
        return {"score": self.value, "observed": self.observed,
                "reasons": [{"points": r.points, "text": r.text} for r in self.reasons],
                "assessment": self.assessment}


# --- Cleaning -----------------------------------------------------------------------

def _text(value) -> str:
    return value.strip()[:MAX_TEXT] if isinstance(value, str) else ""


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
            dropped.append(f"#{n} does not exist")
        elif evidence[n - 1].tool == TOOL_NAME:
            dropped.append(f"#{n} is an assessment, not evidence")
        elif evidence[n - 1].is_error:
            dropped.append(f"#{n} ({evidence[n - 1].tool}) failed")
        else:
            ok.append(n)
    return ok


def _cite(calls: list, evidence: list) -> str:
    return ", ".join(f"{evidence[n - 1].tool} (#{n})" for n in calls)


def score(raw_assessment, evidence: list) -> Score:
    """The evidence score for an assessment, given the run's tool calls (agent.Evidence)."""
    a, reasons = clean(raw_assessment)
    dropped: list[str] = []

    if a["conclusion"] == "inconclusive":
        reasons.append(Reason(INCONCLUSIVE_SCORE, "Inconclusive: no cause identified"))
        return Score(INCONCLUSIVE_SCORE, reasons, a)

    subject = "Cause" if a["conclusion"] == "cause_found" else "No problem"
    total = 0

    cause_calls = _valid(a["cause_evidence"], evidence, dropped)
    observed = a["cause_support"] == "observed" and bool(cause_calls)
    if observed:
        total += POINTS["cause_observed"]
        reasons.append(Reason(POINTS["cause_observed"],
                              f"{subject} directly observed: {_cite(cause_calls, evidence)}"))
    else:
        why = (" (claimed observed, but no valid citation)" if a["cause_support"] == "observed"
               else (f": {_cite(cause_calls, evidence)}" if cause_calls else ", with no citation"))
        total += POINTS["cause_inferred"]
        reasons.append(Reason(POINTS["cause_inferred"], f"{subject} inferred{why}"))

    tools = {evidence[n - 1].tool for n in cause_calls}
    if len(tools) >= CORROBORATION_TOOLS:
        total += POINTS["corroborated"]
        reasons.append(Reason(POINTS["corroborated"], f"Corroborated by {len(tools)} different tools"))

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
            total += POINTS["alternative_open"]
            reasons.append(Reason(POINTS["alternative_open"], f"Not ruled out: {name}"))

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

    minor = 0
    for item in a["unverified"]:
        what = item["what"] or "an unnamed item"
        if item["could_change_diagnosis"]:
            total += POINTS["unverified_major"]
            reasons.append(Reason(POINTS["unverified_major"],
                                  f"Not verified, could change the diagnosis: {what}"))
        else:
            penalty = max(POINTS["unverified_minor"], -MAX_MINOR_PENALTY - minor)
            minor += penalty
            total += penalty
            reasons.append(Reason(penalty, f"Not verified (detail only): {what}"))

    if dropped:
        reasons.append(Reason(0, "Citations ignored: " + "; ".join(dropped)))

    if ruled_out == 0 and total > CAP_NO_ALTERNATIVE_RULED_OUT:
        reasons.append(Reason(CAP_NO_ALTERNATIVE_RULED_OUT - total,
                              f"Capped at {CAP_NO_ALTERNATIVE_RULED_OUT}: no alternative explanation "
                              "was ruled out with evidence"))
        total = CAP_NO_ALTERNATIVE_RULED_OUT

    return Score(max(0, min(100, total)), reasons, a,
                 observed=observed and a["conclusion"] == "cause_found")


def format_reasons(s: Score) -> list[str]:
    return [f"{r.points:+4d}  {r.text}" if r.points else f"   .  {r.text}" for r in s.reasons]


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
            "by number (1 = your first call). The code turns it into an evidence score: a cause seen "
            "directly in a tool result counts for more than one inferred; alternatives ruled out with "
            "evidence, and timing that fits the alert, add to it; open alternatives and things you "
            "could not check take away. Be honest: an inflated assessment only delays a human. You "
            "may revise it once, after gathering more evidence.",
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
