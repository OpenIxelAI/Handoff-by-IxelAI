"""The rankings and what each model costs (rankings.py): the new models are ranked and tiered, the built-in
rankings.json is the module's, and no fallback is ever dearer than the model it replaces (COST)."""
import json
from pathlib import Path

from handoff import rankings

NEW_COST = {"gpt-6-astra": 100, "grok-4.7": 20, "gemini-3.1-pro-preview": 20, "gpt-6.1-sol": 20, "gpt-6-luna": 1}


def tiers_of(entry):
    return [tier for tier, models in rankings.TIERS.items() if any(m["model"] == entry for m in models)]


def fallbacks_of(agent, model):
    return rankings.fallbacks("coding", agent, model, None, rankings.RANKS)


def test_the_new_models_are_ranked_priced_and_tiered():
    for job in ("coding", "review", "writing"):
        assert "grok:grok-4.7" in rankings.RANKS[job]
    for job in ("review", "writing"):
        assert "antigravity:gemini-3.1-pro-preview" in rankings.RANKS[job]
    for entry in ("codex:gpt-6-astra", "codex:gpt-6.1-sol", "codex:gpt-6-luna"):
        assert entry in rankings.RANKS["coding"]
    assert {model: rankings.COST[model] for model in NEW_COST} == NEW_COST
    assert tiers_of("codex:gpt-6-astra") == ["top"] and tiers_of("antigravity:gemini-3.1-pro-preview") == ["top"]
    assert tiers_of("grok:grok-4.7") == ["mid"] and tiers_of("codex:gpt-6.1-sol") == ["mid"]
    assert tiers_of("codex:gpt-6-luna") == ["low"]


def test_rankings_json_matches_the_module():
    path = Path(rankings.__file__).with_name("rankings.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"updated": rankings.UPDATED, "ranks": rankings.RANKS, "tiers": rankings.TIERS}


def test_no_fallback_is_ever_dearer_than_the_model_it_replaces():
    for job in ("coding", "planning", "review", "writing"):
        for entry in rankings.RANKS[job]:
            agent, model = rankings.split(entry)
            model = rankings.canonical(agent, model)
            if model not in rankings.COST:
                continue
            for fallback in rankings.fallbacks(job, agent, model, None, rankings.RANKS):
                if rankings._free(fallback):
                    continue
                fb_agent, fb_model = rankings.split(fallback)
                cost = rankings.COST.get(rankings.canonical(fb_agent, fb_model), 0)
                assert cost <= rankings.COST[model], f"{job}: {entry} falls back to {fallback}"


def test_the_prices_keep_the_claude_fallbacks_where_they_were():
    assert "codex:gpt-6-astra" not in fallbacks_of("claude", "claude-fable-5-1")
    sonnet = fallbacks_of("claude", "claude-sonnet-5-5")
    assert "codex:gpt-6.1-sol" not in sonnet and "grok:grok-4.7" not in sonnet
    assert "grok:grok-4.7" not in fallbacks_of("claude", "claude-haiku-5-5")
