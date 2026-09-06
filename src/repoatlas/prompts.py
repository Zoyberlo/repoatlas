"""Task wordings, and how much of the answer each one gives away.

Every agent-level comparison in this project so far has posed a commit
subject. Those are written by the developer who made the change, moments
after making it, in the vocabulary of the code — *"render note-row
badges in a summary block"*. Half those nouns are identifiers.

That is a real task, but it is one particular task, and it is the one
`grep` is best at: the request already contains the string to search for.
Nine null results were therefore all measured in the baseline's best case,
which is worth saying out loud before quoting them again.

Real requests are not distributed like that. *"Fix the button on the
report page that clears the report, it doesn't work"* names nothing in the
code — the button might be `resetReport`, `ClearReportAction`, or a label
in a translation file. A search has nothing to anchor on, which is
precisely the condition a resolved index exists for.

So a single number cannot answer "is this worth it". What can is a
profile across how much the request gives away, and this module is the
part that decides which stratum a wording belongs to — mechanically,
because a benchmark whose difficulty is assigned by the person hoping for
a result is not a benchmark.

The rule is overlap with the answer:

- **precise** — the request names an identifier or a path from the ground
  truth. `grep` needs one call. The question here is not whether an index
  finds it but whether it adds anything once found.
- **domain** — the request shares no identifier with the answer but does
  use words that exist in the codebase. Roughly where commit subjects sit.
- **unanchored** — the request shares *nothing* with the answer's
  identifiers. Text search has no purchase; if an index never wins here it
  will not win anywhere.
- **diffuse** — the change is spread across layers with no single site.
  Assigned by hand, since "no one place to look" is not a property of the
  wording.

One limitation is worth knowing before reading any table built on this.
The rule is binary overlap, so *"the button on the report page that wipes
everything"* comes out **precise**, because `report` is in the answer's
path. That is the rule being strict rather than broken — but it means
`unanchored` will be a small stratum, since a request about a report page
almost always says "report".

What actually separates those cases is not whether a word is shared but
how many things it selects: `report` matches two hundred symbols and
`clearReport` matches one, which is the lexical collision rate the
published ablation found to be the deciding variable. Refining the middle
strata that way needs an index to count against, and is worth doing only
once there is data showing the binary split is too coarse. The guarantee
that matters is already exact: an `unanchored` wording shares nothing with
its answer, so nothing about it can be solved lexically.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "STRATA",
    "PromptSet",
    "TaskPrompt",
    "classify",
    "identifiers_of",
    "load_prompts",
    "words_of",
]

STRATA = ("precise", "domain", "unanchored", "diffuse")

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Words that appear in every request and in every codebase, and mean
# nothing about either. Counting `report` as domain vocabulary is fair;
# counting `the` is how a filter stops filtering.
_STOP = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from", "get", "has", "have", "how", "i", "if", "in", "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "our", "so", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to", "too", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your", "please", "fix", "make", "add", "remove", "change", "update", "should", "could"]
)


@dataclass(frozen=True, slots=True)
class TaskPrompt:
    """One wording of one task, and the stratum it was placed in."""

    sha: str
    text: str
    stratum: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"sha": self.sha, "text": self.text, "stratum": self.stratum, "note": self.note}


@dataclass(slots=True)
class PromptSet:
    """Wordings keyed by the commit whose change they describe."""

    prompts: dict[str, TaskPrompt] = field(default_factory=dict)
    source: str = ""

    def for_sha(self, sha: str) -> TaskPrompt | None:
        """The wording for a commit, matched on the prefix the runs record."""
        if sha in self.prompts:
            return self.prompts[sha]
        for key, prompt in self.prompts.items():
            if sha.startswith(key) or key.startswith(sha):
                return prompt
        return None

    def in_stratum(self, stratum: str) -> PromptSet:
        return PromptSet(
            {k: v for k, v in self.prompts.items() if v.stratum == stratum}, self.source
        )


def words_of(text: str) -> set[str]:
    """The meaningful words of a request, lowercased.

    Split on camel and snake boundaries too, so that a request saying
    "report field" and an identifier called `reportField` are seen to
    share something. Not splitting them would let a wording claim to be
    unanchored while naming the answer in another case.
    """
    found: set[str] = set()
    for token in _WORD.findall(text):
        pieces = re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", token)
        for piece in (token, *pieces):
            lowered = piece.lower()
            if len(lowered) > 2 and lowered not in _STOP:
                found.add(lowered)
    return found


def identifiers_of(names: Iterable[str]) -> set[str]:
    """The words an answer is made of: symbol names and path segments."""
    found: set[str] = set()
    for name in names:
        for part in re.split(r"[/\\.]", name):
            found |= words_of(part)
    return found


def classify(
    text: str, answer_names: Iterable[str], *, vocabulary: Iterable[str] = ()
) -> str:
    """Which stratum this wording falls in, by what it gives away.

    ``answer_names`` are the ground truth's symbol names and paths;
    ``vocabulary`` is the rest of the repository's identifiers, which is
    what separates a request using the project's words from one using only
    the user's. `diffuse` is never returned: whether a change has one site
    is a property of the change, not of the sentence describing it.
    """
    asked = words_of(text)
    answer = identifiers_of(answer_names)
    if asked & answer:
        return "precise"
    if asked & identifiers_of(vocabulary):
        return "domain"
    return "unanchored"


def load_prompts(path: Path) -> PromptSet:
    """Read a prompt set, refusing anything that would confuse a report.

    Strict on purpose. A wording with no stratum, or one whose stratum is
    not a known name, would land in a results table as a column nobody can
    interpret, and the failure would look like a finding.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        entries = raw
        source = str(path)
    elif isinstance(raw, dict):
        entries = raw.get("prompts") or []
        source = str(raw.get("source") or path)
    else:
        raise ValueError(f"{path}: expected a list or an object with 'prompts'")

    prompts: dict[str, TaskPrompt] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: every prompt must be an object")
        sha = str(entry.get("sha") or "").strip()
        text = str(entry.get("text") or "").strip()
        stratum = str(entry.get("stratum") or "").strip()
        if not sha or not text:
            raise ValueError(f"{path}: a prompt needs both 'sha' and 'text'")
        if stratum and stratum not in STRATA:
            raise ValueError(f"{path}: unknown stratum {stratum!r}; one of {', '.join(STRATA)}")
        prompts[sha] = TaskPrompt(sha, text, stratum, str(entry.get("note") or ""))
    return PromptSet(prompts, source)
