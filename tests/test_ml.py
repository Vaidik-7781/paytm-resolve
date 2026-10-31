from resolve.ml.corpus import generate
from resolve.ml.ensemble import combine
from resolve.ml.intent_model import get_model


def test_model_beats_rules_on_unseen_noisy_phrases():
    from resolve.llm.offline import classify
    rows = generate(60, seed=4321, part=2, noise=0.3)
    ml = sum(get_model().predict(t)["intent"] == i for t, i, _ in rows) / len(rows)
    rules = sum(classify(t)["intent"] == i for t, i, _ in rows) / len(rows)
    assert ml > 0.85 and ml > rules + 0.1


def test_ensemble_never_misroutes_confidently():
    rows = generate(80, seed=777, part=2, noise=0.4)
    bad = [1 for t, i, _ in rows if combine(t)["intent"] not in (i, "unknown")]
    assert len(bad) == 0


def test_cross_lingual():
    assert combine("मेरा सेटलमेंट रुका हुआ है")["intent"] == "settlement_held"
    assert combine("mere paise do baar kat gaye")["intent"] == "duplicate_debit"
    assert combine("my soundbox is silent")["intent"] == "soundbox"
