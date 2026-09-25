import pytest

from app import app, r, generate_game_hash, all_letters


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def clean_redis():
    r.flushdb()


def test_hash_changes_when_letters_are_removed():
    game_code = "HASH42"
    word = "casa"
    r.set(f"game:{game_code}", "admin", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.lpush(f"game:{game_code}:players", "A", "B")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    old_hash = generate_game_hash(game_code)
    r.lrem(f"game:{game_code}:{word}", 0, "a")

    assert old_hash != generate_game_hash(game_code)


def test_all_players_skip_does_not_award_points_and_advances_turn(client):
    game_code = "SKIP01"
    word = "casa"
    r.set(f"game:{game_code}", "A", ex=3600)
    r.set(f"game:{game_code}:status", "playing", ex=3600)
    r.set(f"game:{game_code}:word", word, ex=3600)
    r.set(f"game:{game_code}:playerplay", 0, ex=3600)
    r.set(f"game:{game_code}:money", 100, ex=3600)
    r.set(f"game:{game_code}:nb_words", 2, ex=3600)
    r.lpush(f"game:{game_code}:players", "B", "A")
    r.expire(f"game:{game_code}:players", 3600)
    r.rpush(f"game:{game_code}:{word}", *all_letters)
    r.expire(f"game:{game_code}:{word}", 3600)

    client.set_cookie("username", "A", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})
    assert resp.status_code == 302

    client.set_cookie("username", "B", domain="localhost")
    resp = client.post("/guess", data={"game_code": game_code, "action": "skip"})
    assert resp.status_code == 302

    assert r.get(f"game:{game_code}:score:A") is None
    assert r.get(f"game:{game_code}:score:B") is None
    assert r.get(f"game:{game_code}:playerplay") == "0"
