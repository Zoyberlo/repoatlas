"""Deciding how much a request gives away, mechanically.

The point of the stratification is that nobody hoping for a result gets to
assign the difficulty. So these tests are mostly about the rule refusing to
be generous: a wording that names the answer in another case, or through a
path, or in camel case, is *precise* however user-facing it reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoatlas.prompts import classify, identifiers_of, load_prompts, words_of

ANSWER = ["app/Http/Controllers/ReportController.php", "ReportController.clearReport"]
VOCABULARY = ["app/Models/Estimate.php", "Estimate.section", "app/Support/Layout.php"]


class TestWhatCountsAsGivingTheAnswerAway:
    def test_naming_the_class_is_precise(self) -> None:
        assert classify("ReportController is broken", ANSWER) == "precise"

    def test_naming_the_method_in_prose_is_still_precise(self) -> None:
        # "clear report" and `clearReport` are the same words with a space
        # in them; a rule that missed that would let the easiest wordings
        # be scored as the hardest.
        assert classify("the clear report action fails", ANSWER) == "precise"

    def test_naming_a_path_segment_is_precise(self) -> None:
        assert classify("something in Http Controllers is wrong", ANSWER) == "precise"

    def test_the_user_facing_wording_is_unanchored(self) -> None:
        asked = "the button on the report page that wipes everything does nothing"
        # `report` is in the answer's path, so this must NOT come out
        # unanchored — and that is the rule working, not failing.
        assert classify(asked, ANSWER) == "precise"

    def test_a_wording_sharing_nothing_is_unanchored(self) -> None:
        asked = "the button at the top right does nothing when I press it"
        assert classify(asked, ANSWER, vocabulary=VOCABULARY) == "unanchored"

    def test_project_words_that_are_not_the_answer_are_domain(self) -> None:
        asked = "the estimate section is not saved with the layout"
        assert classify(asked, ANSWER, vocabulary=VOCABULARY) == "domain"

    def test_common_words_alone_do_not_anchor_anything(self) -> None:
        # Without a stop list every request is "precise", because every
        # request says "the" and some file is called `the_something`.
        assert classify("please fix this, it should not do that", ANSWER) == "unanchored"


class TestSplittingWords:
    def test_camel_case_is_split_and_kept_whole(self) -> None:
        assert {"clearreport", "clear", "report"} <= words_of("clearReport")

    def test_snake_case_is_split(self) -> None:
        assert {"clear", "report"} <= words_of("clear_report")

    def test_two_letter_words_are_ignored(self) -> None:
        assert "id" not in words_of("id of it")

    def test_identifiers_come_from_paths_and_names_alike(self) -> None:
        found = identifiers_of(["app/Models/Estimate.php"])
        assert {"app", "models", "estimate"} <= found
        assert "php" in found


class TestLoadingASet:
    def write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "prompts.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_list_of_prompts_loads(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path, [{"sha": "abc123", "text": "the button", "stratum": "unanchored"}]
        )
        loaded = load_prompts(path)
        assert loaded.for_sha("abc123def456").text == "the button"

    def test_a_short_sha_finds_a_long_one(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, [{"sha": "abc123def456", "text": "x", "stratum": "domain"}])
        assert load_prompts(path).for_sha("abc123") is not None

    def test_filtering_by_stratum(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            [
                {"sha": "a1", "text": "one", "stratum": "precise"},
                {"sha": "b2", "text": "two", "stratum": "unanchored"},
            ],
        )
        only = load_prompts(path).in_stratum("unanchored")
        assert list(only.prompts) == ["b2"]

    def test_an_unknown_stratum_is_refused(self, tmp_path: Path) -> None:
        # It would arrive in a results table as a column nobody can read,
        # and the mistake would look like a finding.
        path = self.write(tmp_path, [{"sha": "a1", "text": "x", "stratum": "easyish"}])
        with pytest.raises(ValueError, match="unknown stratum"):
            load_prompts(path)

    def test_a_prompt_without_text_is_refused(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, [{"sha": "a1", "stratum": "domain"}])
        with pytest.raises(ValueError, match="needs both"):
            load_prompts(path)
