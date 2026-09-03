"""Tests for pinning the token estimate to a real tokenizer.

The budget is the product. A map fitted to two thousand tokens that costs
twenty-six hundred on the model reading it is not a budget, and that is
exactly where the default constant stood after the tokenizer change at
Opus 4.7. So these tests check three things: that the default now carries
the documented correction, that a calibration recovers a tokenizer's true
constant from a handful of samples, and that every tool over a calibrated
store actually uses it.
"""

from __future__ import annotations

import io
import json
import math
import urllib.error
from pathlib import Path

import pytest

from repoatlas.rank import calibrate as calibration
from repoatlas.rank.tokens import (
    CHARS_PER_TOKEN,
    CHARS_PER_TOKEN_BEFORE_OPUS_47,
    TOKENIZER_GROWTH_FROM_OPUS_47,
    calibrate_constant,
    estimate_with,
    make_estimator,
)
from repoatlas.server import tools
from repoatlas.store import IndexStore, update_store

pytest.importorskip("tree_sitter_language_pack", reason="needs the parse extra")

FIXTURE = Path(__file__).parent / "fixtures" / "tsdemo"


def fake_tokenizer(chars_per_token: float, envelope: int = 7):
    """A counter that behaves like a real one: an envelope plus the text."""

    def count(text: str) -> int:
        lines = text.count("\n")
        body = len(text) - lines
        return envelope + math.ceil(body / chars_per_token) + lines + 1

    return count


@pytest.fixture
def store(tmp_path: Path) -> IndexStore:
    with IndexStore(tmp_path / "index.db") as opened:
        update_store(FIXTURE, opened, use_git=False)
        yield opened


class TestDefaultConstant:
    def test_the_default_carries_the_documented_correction(self) -> None:
        # Anthropic: models from Opus 4.7 on count about 30 percent higher.
        # The default divides the older figure by that growth.
        assert round(
            CHARS_PER_TOKEN_BEFORE_OPUS_47 / TOKENIZER_GROWTH_FROM_OPUS_47, 2
        ) == CHARS_PER_TOKEN
        assert CHARS_PER_TOKEN < CHARS_PER_TOKEN_BEFORE_OPUS_47

    def test_the_old_constant_would_undercount_a_current_model_by_a_third(self) -> None:
        text = "\n".join(f"  {i:>5}  def something_{i}(self, argument) -> Result:" for i in range(80))
        counted = fake_tokenizer(CHARS_PER_TOKEN)(text)
        old = estimate_with(text, CHARS_PER_TOKEN_BEFORE_OPUS_47)
        assert old < counted * 0.85


class TestCalibrateConstant:
    def test_recovers_the_constant_a_tokenizer_used(self) -> None:
        true = 2.9
        counter = fake_tokenizer(true, envelope=0)
        texts = ["x" * 400 + "\n" * 10, "y" * 1300 + "\n" * 30, "z" * 90 + "\n"]
        found = calibrate_constant((text, counter(text)) for text in texts)
        assert abs(found - true) / true < 0.02

    def test_long_samples_weigh_more_than_short_ones(self) -> None:
        # Pooled rather than averaged: a long sample's ratio dominates,
        # because budget errors on long texts are the ones that matter.
        long_text, short_text = "a" * 10_000 + "\n", "b" * 10 + "\n"
        found = calibrate_constant([(long_text, 5000 + 2), (short_text, 2 + 2)])
        assert abs(found - 2.0) < 0.02

    def test_needs_at_least_one_usable_sample(self) -> None:
        with pytest.raises(ValueError):
            calibrate_constant([("", 0), ("abc", 0)])


class TestCalibrateStore:
    def test_records_the_constant_and_the_model(self, store: IndexStore) -> None:
        assert store.chars_per_token() is None
        result = calibration.calibrate_store(store, "claude-test", fake_tokenizer(2.8))
        assert store.calibrated_model() == "claude-test"
        assert store.chars_per_token() == pytest.approx(result.chars_per_token)
        assert 0 < abs(result.chars_per_token - 2.8) / 2.8 < 0.1

    def test_the_envelope_is_subtracted(self, store: IndexStore) -> None:
        # A real count includes the request wrapper. Calibrating on the raw
        # count would fold that fixed cost into the constant.
        with_wrapper = calibration.calibrate_store(store, "m", fake_tokenizer(2.8, envelope=40))
        bare = calibration.calibrate_store(store, "m", fake_tokenizer(2.8, envelope=0))
        assert abs(with_wrapper.chars_per_token - bare.chars_per_token) < 0.05

    def test_the_estimate_after_calibration_is_close(self, store: IndexStore) -> None:
        counter = fake_tokenizer(3.1)
        calibration.calibrate_store(store, "m", counter)
        text = tools.repo_map(store, budget=900)
        estimated = store.estimator()(text)
        real = counter(text) - (counter("x") - 1)
        assert abs(estimated - real) / real < 0.05

    def test_reports_what_the_default_would_have_said(self, store: IndexStore) -> None:
        result = calibration.calibrate_store(store, "m", fake_tokenizer(2.0))
        assert result.samples >= 4
        assert result.counted_tokens > 0
        # The default assumes more characters per token than this tokenizer
        # gives, so it undercounts, and the signed error says so.
        assert result.error_before < 0
        assert result.as_dict()["model"] == "m"

    def test_samples_are_the_texts_the_tools_produce(self, store: IndexStore) -> None:
        samples = calibration.sample_texts(store)
        assert any("src/user.ts:" in text for text in samples)
        assert any("use(s) of" in text for text in samples)

    def test_an_empty_index_cannot_be_calibrated(self, tmp_path: Path) -> None:
        with (
            IndexStore(tmp_path / "empty.db") as empty,
            pytest.raises(calibration.CalibrationError, match="nothing to calibrate"),
        ):
            calibration.calibrate_store(empty, "m", fake_tokenizer(3.0), samples=[])


class TestToolsUseTheCalibration:
    def test_every_tool_budget_follows_the_store(self, store: IndexStore) -> None:
        generous = len(tools.repo_map(store, budget=300))
        calibration.calibrate_store(store, "m", fake_tokenizer(1.2))
        tight = len(tools.repo_map(store, budget=300))
        assert tight < generous

    def test_an_explicit_estimator_wins_over_the_store(self, store: IndexStore) -> None:
        calibration.calibrate_store(store, "m", fake_tokenizer(1.2))
        stored = len(tools.file_outline(store, "src/user.ts", budget=60))
        generous = len(tools.file_outline(store, "src/user.ts", budget=60, estimator=make_estimator(20.0)))
        assert generous > stored

    def test_the_budget_tracker_never_exceeds_its_limit(self) -> None:
        estimate = make_estimator(CHARS_PER_TOKEN)
        budgeted = tools._Budget(50, estimate)
        added = 0
        while budgeted.add(f"  {added:>5}  def something_{added}(self, argument) -> Result:"):
            added += 1
        assert added > 0
        assert budgeted.spent <= 50
        assert estimate("\n".join(budgeted.lines)) <= 50

    def test_status_reports_the_calibration_and_keeps_size_last(self, store: IndexStore) -> None:
        before = tools.index_status(store)
        assert "not calibrated" in before
        calibration.calibrate_store(store, "claude-test", fake_tokenizer(2.8))
        after = tools.index_status(store)
        assert "calibrated for claude-test" in after
        # The size changes on every re-index; everything before it should
        # stay byte-identical so the agent's prompt cache survives.
        assert after.rstrip().splitlines()[-1].startswith("size:")


class TestAnthropicCounter:
    def test_reads_the_count_from_the_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["model"] = json.loads(request.data)["model"]
            seen["key"] = request.get_header("X-api-key")
            return Reply(b'{"input_tokens": 123}')

        monkeypatch.setattr(calibration.urllib.request, "urlopen", fake_urlopen)
        count = calibration.anthropic_counter("claude-test", "sk-test")
        assert count("hello") == 123
        assert seen == {"url": calibration.COUNT_ENDPOINT, "model": "claude-test", "key": "sk-test"}

    def test_an_http_error_becomes_a_calibration_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "nope", {}, io.BytesIO(b"bad key"))

        monkeypatch.setattr(calibration.urllib.request, "urlopen", failing)
        with pytest.raises(calibration.CalibrationError, match="401"):
            calibration.anthropic_counter("m", "k")("text")

    def test_refuses_to_start_without_a_key(self) -> None:
        with pytest.raises(calibration.CalibrationError, match="API key"):
            calibration.anthropic_counter("m", "")


class TestCalibrateCli:
    def test_records_and_reports(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        from repoatlas.cli import main

        path = tmp_path / "index.db"
        with IndexStore(path) as opened:
            update_store(FIXTURE, opened, use_git=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(
            calibration, "anthropic_counter", lambda model, key: fake_tokenizer(2.5)
        )
        assert main(["calibrate", str(path), "--model", "claude-test"]) == 0
        out = capsys.readouterr().out
        assert "claude-test" in out and "calibrated:" in out
        with IndexStore(path) as opened:
            assert opened.calibrated_model() == "claude-test"

    def test_json_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        from repoatlas.cli import main

        path = tmp_path / "index.db"
        with IndexStore(path) as opened:
            update_store(FIXTURE, opened, use_git=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(
            calibration, "anthropic_counter", lambda model, key: fake_tokenizer(2.5)
        )
        assert main(["calibrate", str(path), "--model", "m", "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["model"] == "m" and payload["samples"] >= 4

    def test_a_missing_key_is_explained(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from repoatlas.cli import main

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(SystemExit, match="API key"):
            main(["calibrate", str(tmp_path / "index.db"), "--model", "m"])

    def test_the_map_command_uses_a_stored_calibration(
        self, tmp_path: Path, capsys
    ) -> None:
        from repoatlas.cli import main

        path = tmp_path / "index.db"
        with IndexStore(path) as opened:
            update_store(FIXTURE, opened, use_git=False)
        assert main(["map", str(path), "--budget", "300"]) == 0
        before = capsys.readouterr().out
        with IndexStore(path) as opened:
            calibration.calibrate_store(opened, "m", fake_tokenizer(1.2))
        assert main(["map", str(path), "--budget", "300"]) == 0
        after = capsys.readouterr().out
        assert len(after) < len(before)
