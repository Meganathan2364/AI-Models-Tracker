import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from backend import jobs


def test_nested_tracked_jobs_preserve_each_source_status():
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(jobs, "DATA", jobs.Path(temp_dir)):
            jobs.tracked("prices", lambda: (jobs.tracked("qwen_model_studio", lambda: 5), 10)[1])
            status = json.loads(jobs.Path(temp_dir, "status.json").read_text(encoding="utf-8"))

    assert status["prices"]["rows"] == 10
    assert status["qwen_model_studio"]["rows"] == 5


def test_qwen_catalog_parser_uses_the_source_page_and_ignores_other_providers():
    html = """
    <a class="adoc-help-center-card" href="/model/qwen4-max-preview">
      <img src="/assets/qwen.svg">
      <strong class="adoc-help-center-card-title">qwen4-max-preview</strong>
      <div class="adoc-help-center-card-body"><p>Complex reasoning and coding (Token Plan)</p></div>
    </a>
    <a class="adoc-help-center-card" href="/model/qwen4-max-preview">
      <strong class="adoc-help-center-card-title">qwen4-max-preview</strong>
      <div class="adoc-help-center-card-body"><p>Visual understanding (Token Plan)</p></div>
    </a>
    <a class="adoc-help-center-card" href="/model/other">
      <strong class="adoc-help-center-card-title">deepseek-v4</strong>
    </a>
    """

    models = jobs.parse_qwen_catalog(html, "https://example.com/catalog")

    assert list(models) == ["qwen4maxpreview"]
    model = models["qwen4maxpreview"]
    assert model["official_description"] == "Complex reasoning and coding (Token Plan); Visual understanding (Token Plan)"
    assert model["official_url"] == "https://example.com/model/qwen4-max-preview"
    assert model["image_url"] == "https://example.com/assets/qwen.svg"


def test_provider_icon_prefers_explicit_official_icon_url():
    preferred = "https://www.gstatic.com/google.svg"
    with patch("backend.jobs.get", side_effect=RuntimeError("offline")):
        candidates = jobs.icon_candidates("https://deepmind.google", preferred)

    assert candidates == [preferred, "https://deepmind.google/favicon.ico"]


def test_qwen_capability_parser_reads_model_table_without_inventing_entries():
    html = """
    <table><thead><tr><th>Model ID</th><th>Capability</th></tr></thead>
    <tbody>
      <tr><td>qwen4-max-preview</td><td>Reasoning, visual understanding, text generation</td></tr>
      <tr><td>deepseek-v4</td><td>Reasoning, text generation</td></tr>
    </tbody></table>
    <p>The alias of qwen4-max-preview is qwen4.0-max-preview. Both IDs can be used.</p>
    """

    models = jobs.parse_qwen_capabilities(
        html,
        "https://example.com/details",
        "https://example.com/catalog",
    )

    assert list(models) == ["qwen4maxpreview"]
    model = models["qwen4maxpreview"]
    assert model["official_capabilities"] == ["Reasoning", "visual understanding", "text generation"]
    assert model["aliases"] == ["qwen4.0-max-preview"]
    assert model["official_url"] == "https://example.com/details"


def test_qwen_retirement_parser_converts_end_of_day_to_next_date():
    html = """
    <ul><li>qwen4-max-preview preview benefits: The preview period runs from
    10:00 on November 11, 2026 to 24:00 on November 25, 2026 (UTC+8);
    the model will be retired after the preview ends.</li></ul>
    """

    rows = jobs.parse_qwen_retirements(html, "https://example.com/retirements")

    assert len(rows) == 1
    assert rows[0]["model"] == "qwen4-max-preview"
    assert rows[0]["retire_guess"] == "2026-11-26"


def test_openrouter_description_is_not_truncated():
    description = ("Detailed model description. " * 30).strip()
    response = SimpleNamespace(
        json=lambda: {
            "data": [
                {
                    "id": "openai/example-model",
                    "name": "Example Model",
                    "description": description,
                }
            ]
        }
    )

    with patch("backend.jobs.get", return_value=response):
        models = jobs.fetch_openrouter()

    assert models["examplemodel"]["description"] == description


def test_news_job_uses_feed_descriptions_and_images_and_backfills_existing_items():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = jobs.Path(temp_dir)
        old_item = {
            "source": "OpenAI news",
            "title": "Source headline",
            "link": "https://example.com/story",
            "published": None,
        }
        data_dir.mkdir(exist_ok=True)
        (data_dir / "news.json").write_text(json.dumps({"items": [old_item]}), encoding="utf-8")
        entry = {
            "title": "Source headline",
            "link": "https://example.com/story",
            "summary": "<p>A description from the feed.</p>",
            "media_content": [{"url": "/images/story.jpg", "type": "image/jpeg"}],
        }

        with (
            patch.object(jobs, "DATA", data_dir),
            patch.object(jobs.feedparser, "parse", return_value=SimpleNamespace(entries=[entry])),
        ):
            jobs.news_job()
            payload = json.loads((data_dir / "news.json").read_text(encoding="utf-8"))

    assert len(payload["items"]) == 1
    assert payload["items"][0]["description"] == "A description from the feed."
    assert payload["items"][0]["image"] == "https://example.com/images/story.jpg"
    assert payload["items"][0]["published"] is None
